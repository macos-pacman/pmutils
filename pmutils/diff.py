#!/usr/bin/env python
# Copyright (c) 2024, zhiayang
# SPDX-License-Identifier: Apache-2.0

import os
import io
import re
import shlex
import fnmatch
import tempfile
import subprocess
import braceexpand

from typing import *
from pmutils import msg, util
from dataclasses import dataclass

import requests as req
import urllib.parse as urlparse

UPSTREAM_URL_BASE = f"https://gitlab.archlinux.org"
PACKAGE_NAMESPACE = f"archlinux/packaging/packages"


@dataclass(frozen=True)
class FileDiff:
	name: str
	diff: str
	upstream: str

@dataclass(frozen=True)
class PackageDiff:
	files: Iterator[FileDiff]


def _generate_diff(pkg_url: str, upstream_sha: str, local_path: str) -> Optional[FileDiff]:
	resp = req.get(f"{UPSTREAM_URL_BASE}/api/v4/projects/{pkg_url}/repository/blobs/{upstream_sha}/raw")
	if resp.status_code != 200:
		msg.error(f"Could not fetch upstream PKGBUILD content: {resp.text}")
		return None

	if not os.path.exists(local_path):
		return None

	# ignore the exit code
	filename = os.path.basename(local_path)
	output = subprocess.run(["diff", "-d", "--unified=1", f"--label={filename}", f"--label={filename}",
		"-", os.path.normpath(local_path)], text=True, input=resp.text, check=False, capture_output=True)

	if output.returncode == 2:
		msg.warn2(f"Diff produced an error: {output.stderr}")
		return None
	elif output.returncode == 0:
		return FileDiff(name=filename, diff="", upstream=resp.text)

	return FileDiff(name=filename, diff=output.stdout, upstream=resp.text)


def _patch_offensive_lines_in_pkgbuild(lines: list[str]) -> list[str]:
	i: int = 0

	ret: list[str] = []
	while i < len(lines):
		line = lines[i]

		if line.startswith("pkgrel="):
			ret.append("pkgrel=69")
		elif line.startswith(f"pkgver="):
			ret.append("pkgver=69.420")
		elif line.startswith("url="):
			ret.append("url=AAAAAAAA")
		elif (m := re.match(r"(_\w*ver)=", line)):
			ret.append(f"{m.groups()[0]}=AAAAAAAA")
		elif (m := re.match(r"(_\w*(?:rev|commit))=", line)):
			ret.append(f"{m.groups()[0]}=AAAAAAAA")
		elif line.startswith("_build_date="):
			ret.append(f"_build_date='January 1, 1970'")
		elif line.startswith("source="):
			while i < len(lines):
				s = re.sub(r"#commit=([A-Fa-f0-9]+)", "#commit=AAAAAAAA", lines[i])
				s = re.sub(r"#revision=([A-Fa-f0-9]+)", "#revision=AAAAAAAA", s)
				i += 1

				# ideally, one line should correspond to one source; i've never seen a PKGBUILD
				# that violates this, but it might happen. do not shlex comments, handle them manually!
				# there's often a `# tag: v2.3.1` thing after a commit; we also need to exclude this from the diff.
				ss = io.StringIO(s)
				shlexer = shlex.shlex(ss, posix=True, punctuation_chars=True)
				shlexer.commenters = ""


				# get tokens till the shlexer returns None
				last = False
				while (t := shlexer.get_token()) is not None:
					if t == '#' and ss.tell() > 0:
						s = s[:ss.tell() - 1]
					elif t == ')':
						last = True

				ret.append(s)
				if last:
					break
			# don't i += 1 again
			continue
		else:
			ret.append(line)

		i += 1
	return ret

def _add_array(line: str, first: bool, arr: list[tuple[str, int]]) -> bool:
	if first:
		line = line.split('=', maxsplit=1)[1]
	ss = io.StringIO(line)
	shlexer = shlex.shlex(ss, posix=True, punctuation_chars=True)
	shlexer.wordchars += "{$:/,}"

	# get tokens till the shlexer returns None
	last = False
	while (t := shlexer.get_token()) is not None:
		if t == '(':
			continue
		elif t == ')':
			last = True
			break
		else:
			n = len(list(braceexpand.braceexpand(t)))
			arr.append((t, n))
	return last

@dataclass(frozen=True)
class SourceChecksumList:
	sources: list[tuple[str, int]]
	checksums: list[str]

	sources_start_line: int
	checksums_start_line: int


def _get_sources_and_checksums_from_lines(lines: list[str]) -> SourceChecksumList:
	sources: list[tuple[str, int]] = []
	checksums: list[tuple[str, int]] = []
	sources_start_line = 0
	checksums_start_line = 0

	i: int = 0
	while i < len(lines):
		line = lines[i]
		if line.startswith("source="):
			sources_start_line = i
			first = True
			while i < len(lines):
				l = lines[i]
				i += 1
				if _add_array(l, first, sources):
					break
				first = False

			# don't i += 1 again
			continue

		# known_hash_algos=({ck,md5,sha{1,224,256,384,512},b2})
		elif re.match(r"(ck|md5|(?:sha(?:1|224|256|384|512))|b2)sums=", line) is not None:
			checksums_start_line = i
			first = True
			while i < len(lines):
				l = lines[i]
				i += 1
				if _add_array(l, first, checksums):
					break
				first = False

			# don't i += 1 again
			continue

		i += 1

	return SourceChecksumList(sources, [c for c, _ in checksums], sources_start_line, checksums_start_line)


def _get_src_filename(src: str) -> str:
	if (i := src.find("::")) != -1:
		return src[:i]
	elif re.match(r"(http(s?)|ftp)://", src):
		return os.path.basename(urlparse.urlparse(src).path)
	else:
		return src

def _patch_pkgbuild_sources(upstream_lines: list[str], local_lines: list[str]) -> tuple[str, str]:
	# get the list of sources for each.
	uu = _get_sources_and_checksums_from_lines(upstream_lines)
	ll = _get_sources_and_checksums_from_lines(local_lines)
	usrcs = uu.sources
	uchks = uu.checksums
	lsrcs = ll.sources
	lchks = ll.checksums

	# for each local source, see if there is an upstream source that matches
	# if so, then *replace* the local source string (in the pkgbuild file) with the upstream string
	# (and replace the checksums as well)
	replacements: dict[str, str] = {}

	lcofs: int = 0
	for l in range(len(lsrcs)):
		ucofs: int = 0
		for u in range(len(usrcs)):
			if _get_src_filename(usrcs[u][0]) == _get_src_filename(lsrcs[l][0]):
				if usrcs[u][1] != lsrcs[l][1]:
					msg.warn2(f"Mismatched number of checksum entries; patching may be unsuccessful")
				nc = min(usrcs[u][1], lsrcs[l][1])

				if lsrcs[l][0] != usrcs[u][0]:
					replacements[lsrcs[l][0]] = usrcs[u][0]

				for c in range(nc):
					if lchks[c+lcofs] != uchks[c+ucofs]:
						replacements[lchks[c+lcofs]] = uchks[c+ucofs]
			ucofs += usrcs[u][1]
		lcofs += lsrcs[l][1]

	# now the other way -- look for new sources in upstream
	ucofs: int = 0
	splice_offset1: int = min(ll.sources_start_line, ll.checksums_start_line)
	splice_offset2: int = max(ll.sources_start_line, ll.checksums_start_line)

	for u in range(len(usrcs)):
		lcofs: int = 0
		found: bool = False
		for l in range(len(lsrcs)):
			found = found or (_get_src_filename(usrcs[u][0]) == _get_src_filename(lsrcs[l][0]))
			lcofs += lsrcs[l][1]
			if found:
				break

		if not found:
			nc = usrcs[u][1]
			new_chks = upstream_lines[uu.checksums_start_line+ucofs:][:nc]
			new_srcs = [upstream_lines[uu.sources_start_line + u]]

			# use the line info to "splice" the new source and checksum into the local file lines.
			if ll.sources_start_line < ll.checksums_start_line:
				# sources array is before checksums array; do the latter first.
				local_lines = local_lines[:ucofs+splice_offset2] + new_chks + local_lines[ucofs+splice_offset2:]
				local_lines = local_lines[:u+splice_offset1] + new_srcs + local_lines[u+splice_offset1:]

				splice_offset1 += len(new_srcs) - 1
				splice_offset2 += len(new_srcs) + len(new_chks) - 1
			else:
				# checksums array is before sources array; do the latter first.
				local_lines = local_lines[:u+splice_offset2] + new_srcs + local_lines[u+splice_offset2:]
				local_lines = local_lines[:ucofs+splice_offset1] + new_chks + local_lines[ucofs+splice_offset1:]

				splice_offset1 += len(new_srcs) + len(new_chks) - 1
				splice_offset2 += len(new_srcs) - 1

		ucofs += usrcs[u][1]


	# now we have the replacements; feed the replacements through all our local lines, then run
	# the diff; this should "automagically" give us the new source files from upstream, without
	# screwing up any changes that they might have made.

	upstream = '\n'.join(upstream_lines) + "\n"
	local = '\n'.join(local_lines) + "\n"
	for s, r in replacements.items():
		local = local.replace(s, r)

	return (upstream, local)




def _generate_diff_for_pkgbuild(pkg_url: str, upstream_sha: str, local_path: str) -> Optional[FileDiff]:
	# pkgbuilds are more annoying. the main reason is that upstream will update
	# the pkgver, so obviously we don't want to "patch" it back to the old version
	# (that defeats the entire purpose).

	# unfortunately, it's a pain in the ass to "perform surgery" on patch files.
	# so we instead remove pkgver and pkgrel lines from BOTH pkgbuilds when passing it to diff
	# (replacing them with identical lines -- otherwise the line numbers screw up).

	# there's a few more complications:
	# 1. commit-based packages (not `-git` ones, but those that use commit hashes)
	#   the plan is to ignore lines matching `^_(\w)*commit=` with the same mechanism as pkgver/pkgrel
	#   we also need to ignore lines containing `#commit=`, but with a slightly different mechanism; replace
	#   the text starting with `#commit=...` all the way till we see either: (a) a closing `"`, (b) a `)`, or (c) a newline

	# 2. SVN shit -- same, but with `_rev` instead of `_commit` and `#revision=`

	# 3. checksums. obviously we don't want to patch the checksums if possible, since upstream will have
	#   updated them when updating the package. the trouble is that sometimes our patched PKGBUILDs have additional
	#   source files -- ignoring the entire checksum block means we will get mismatched array lengths
	#
	#   one potentially very-involved solution is to ask makepkg to generate the source list (parsing it ourselves
	#   would be quite fragile, since it's arbitrary bash...) -- after replacing that git/svn stuff, then finding
	#   differences, and *keeping* the shasum entries corresponding to those differences?

	resp = req.get(f"{UPSTREAM_URL_BASE}/api/v4/projects/{pkg_url}/repository/blobs/{upstream_sha}/raw")
	if resp.status_code != 200:
		msg.error(f"Could not fetch upstream PKGBUILD content: {resp.text}")
		return None

	upstream_lines = _patch_offensive_lines_in_pkgbuild(resp.text.splitlines())
	local_lines = _patch_offensive_lines_in_pkgbuild(open(local_path, "r").read().splitlines())

	upstream, local = _patch_pkgbuild_sources(upstream_lines, local_lines)

	with tempfile.NamedTemporaryFile("w") as f1, tempfile.NamedTemporaryFile("w") as f2:
		f1.write(upstream)
		f2.write(local)
		f1.flush()
		f2.flush()

		# ignore the exit code
		output = subprocess.run(["diff", "-d", "--unified=0", f"--label=PKGBUILD", f"--label=PKGBUILD",
			f1.name, f2.name], text=True, check=False, capture_output=True)

		if output.returncode == 2:
			msg.warn(f"Diff produced an error: {output.stderr}")
			return None
		elif output.returncode == 0:
			return FileDiff(name="PKGBUILD", diff="", upstream=resp.text)
		else:
			return FileDiff(name="PKGBUILD", diff=output.stdout, upstream=resp.text)




def diff_package(pkg_path: str, quiet: bool = False) -> Optional[PackageDiff]:
	pkgbuild_path = f"{pkg_path}/PKGBUILD"
	if not os.path.exists(pkgbuild_path):
		return None

	pkgbase = util.get_srcinfo(pkgbuild_path).pkgbase
	if not quiet:
		msg.log(f"Processing {pkgbase}")

	ignored: list[str] = [".SRCINFO", "*.desktop"]
	if os.path.exists(f"{pkg_path}/.pmdiffignore"):
		with open(f"{pkg_path}/.pmdiffignore", "r") as f:
			ignored.extend(map(lambda s: s.strip(), f.readlines()))

			# weirdge
			if "PKGBUILD" in ignored:
				msg.log2(f"{msg.yellow('Ignoring upstream PKGBUILD')}")

	# first get the list of files.
	pkg_url = urlparse.quote(f"{PACKAGE_NAMESPACE}/{pkgbase}", safe='')
	request_url = f"{UPSTREAM_URL_BASE}/api/v4/projects/{pkg_url}/repository/tree"

	resp = req.get(request_url)
	if resp.status_code != 200:
		msg.error(f"Could not fetch upstream package info: {resp.text}")
		return None

	resp_json: list[Any] = resp.json()
	files: list[tuple[str, str]] = []   # (name, id)

	pkgbuild_idx: Optional[int] = None
	for obj in resp_json:
		obj: dict[str, Any]
		# skip folders... haven't seen a PKGBUILD put important stuff in a folder before.
		if obj["type"] == "tree":
			continue

		if obj["name"] == "PKGBUILD":
			pkgbuild_idx = len(files)

		if not any(fnmatch.fnmatch(obj["name"], x) for x in ignored):
			files.append((obj["name"], obj["id"]))

	if pkgbuild_idx is None:
		msg.warn(f"Could not find PKGBUILD in upstream package; skipping")
		return None

	# rearrange files so PKGBUILD is first
	tmp = files.pop(pkgbuild_idx)
	files = [tmp, *files]

	def generator(pkg_path: str, fs: list[tuple[str, str]]):
		for (name, sha) in fs:
			if not quiet:
				msg.log2(f"{name}")

			if name == "PKGBUILD":
				patch = _generate_diff_for_pkgbuild(pkg_url, sha, f"{pkg_path}/{name}")
			else:
				patch = _generate_diff(pkg_url, sha, f"{pkg_path}/{name}")

			if patch is None:
				continue

			yield patch

	return PackageDiff(generator(pkg_path, files))

