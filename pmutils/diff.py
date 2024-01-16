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
from collections import OrderedDict

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

@dataclass(frozen=True)
class SourceEntry:
	value: str
	comment: str
	expanded_count: int

@dataclass(frozen=True)
class ChecksumEntry:
	value: str
	comment: str

@dataclass(frozen=True)
class SourceChecksumList:
	# second string is for the comment at the end of the line
	sources: list[SourceEntry]
	checksums: OrderedDict[str, list[ChecksumEntry]]


def _add_array(line: str, first: bool, adder: Callable[[tuple[str, str, int]], None]) -> bool:
	if first:
		line = line.split('=', maxsplit=1)[1]

	ss = io.StringIO(line)
	shlexer = shlex.shlex(ss, posix=True, punctuation_chars=True)
	shlexer.wordchars += "{$:/,}"
	shlexer.commenters = ""

	# get tokens till the shlexer returns None
	stop = False

	current_n = 0
	current = ""
	comment = ""

	while (t := shlexer.get_token()) is not None:
		if t == '(':
			continue
		elif t == ')':
			stop = True
		elif t == '#':
			# consume till the end of line.
			comment += '#' + ss.read()
			break
		elif len(comment) > 0:
			comment += t
		else:
			# flush the current if any
			if current_n > 0:
				adder((current, comment, current_n))

			current = t
			current_n = len(list(braceexpand.braceexpand(t)))

	if current_n > 0:
		adder((current, comment, current_n))

	return stop


def _get_sources_and_checksums_from_lines(lines: list[str]) -> SourceChecksumList:
	sources: list[SourceEntry] = []
	checksums: OrderedDict[str, list[ChecksumEntry]] = OrderedDict()

	i: int = 0
	while i < len(lines):
		line = lines[i]
		if line.startswith("source="):
			first = True
			while i < len(lines):
				l = lines[i]
				i += 1
				if _add_array(l, first, lambda x: sources.append(SourceEntry(*x))):
					break
				first = False

			# don't i += 1 again
			continue

		# known_hash_algos=({ck,md5,sha{1,224,256,384,512},b2})
		elif (m := re.match(r"(ck|md5|(?:sha(?:1|224|256|384|512))|b2)sums=", line)) is not None:
			algo = m.groups()[0]
			first = True
			while i < len(lines):
				l = lines[i]
				i += 1
				if _add_array(l, first, lambda x: checksums.setdefault(algo, []).append(ChecksumEntry(x[0], x[1]))):
					break
				first = False

			# don't i += 1 again
			continue

		i += 1

	return SourceChecksumList(sources, checksums)


def _get_src_filename(src: str) -> str:
	if (i := src.find("::")) != -1:
		return src[:i]
	elif re.match(r"(http(s?)|ftp)://", src):
		return os.path.basename(urlparse.urlparse(src).path)
	else:
		return src

def _patch_pkgbuild_sources(upstream_lines: list[str], local_lines: list[str], ignored_srcs: list[str]) -> tuple[str, str]:
	# get the list of sources for each.
	uu = _get_sources_and_checksums_from_lines(upstream_lines)
	ll = _get_sources_and_checksums_from_lines(local_lines)

	final_sources: list[SourceEntry] = []
	final_checksums: OrderedDict[str, list[ChecksumEntry]] = OrderedDict()

	# for each local source, see if there is an upstream source that matches
	# if so, then *replace* the local source string (in the pkgbuild file) with the upstream string
	# (and replace the checksums as well)
	processed_local_srcs: set[int] = set()
	for l in range(len(ll.sources)):
		ucofs: int = 0
		for u in range(len(uu.sources)):
			if _get_src_filename(uu.sources[u].value) == _get_src_filename(ll.sources[l].value):
				if uu.sources[u].expanded_count != ll.sources[l].expanded_count:
					msg.warn2(f"Mismatched number of checksum entries; patching may be unsuccessful")

				nc: int = min(uu.sources[u].expanded_count, ll.sources[l].expanded_count)
				final_sources.append(uu.sources[u])
				processed_local_srcs.add(l)

				for ck in uu.checksums.keys():
					for c in range(nc):
						final_checksums.setdefault(ck, []).append(uu.checksums[ck][c+ucofs])
				break

			ucofs += uu.sources[u].expanded_count


	# now the other way -- look for new sources in upstream
	ucofs: int = 0
	for u in range(len(uu.sources)):
		found: bool = False
		for l in range(len(ll.sources)):
			found = found or (_get_src_filename(uu.sources[u].value) == _get_src_filename(ll.sources[l].value))
			if found:
				break

		if not found:
			nc = uu.sources[u].expanded_count
			if not any(fnmatch.fnmatch(uu.sources[u].value, x) for x in ignored_srcs):
				final_sources.append(uu.sources[u])
				for ck in uu.checksums.keys():
					for c in range(nc):
						final_checksums.setdefault(ck, []).append(uu.checksums[ck][c+ucofs])

		ucofs += uu.sources[u].expanded_count

	# finally, any local sources that are not found upstream (ie. any custom ones)
	lcofs: int = 0
	for l in range(len(ll.sources)):
		nc: int = ll.sources[l].expanded_count

		if l not in processed_local_srcs:
			final_sources.append(ll.sources[l])
			for ck in ll.checksums.keys():
				for c in range(nc):
					final_checksums.setdefault(ck, []).append(ll.checksums[ck][c+lcofs])

		lcofs += nc

	# upstream is unmodified
	upstream = '\n'.join(upstream_lines) + "\n"

	# local is without the source array or any checksum array,
	# then manually appended with our patched versions
	fixed_local_lines: list[str] = []


	i: int = 0
	while i < len(local_lines):
		line = local_lines[i]
		if line.startswith("source="):
			first = True
			while i < len(local_lines):
				l = local_lines[i]
				i += 1
				if _add_array(l, first, lambda _: None):
					break
				first = False

			# append our fixed sources array
			fixed_local_lines.append("source=(")
			for src in final_sources:
				c = ('  ' + src.comment) if len(src.comment) > 0 else ''
				fixed_local_lines.append(f"  {src.value}{c}")   # it should be safe to not quote these...
			fixed_local_lines.append(')')

			# don't i += 1 again
			continue

		# known_hash_algos=({ck,md5,sha{1,224,256,384,512},b2})
		elif (m := re.match(r"(ck|md5|(?:sha(?:1|224|256|384|512))|b2)sums=", line)) is not None:
			algo = m.groups()[0]
			first = True
			while i < len(local_lines):
				l = local_lines[i]
				i += 1
				if _add_array(l, first, lambda _: None):
					break
				first = False

			fixed_local_lines.append(f"{algo}sums=(")
			for cks in final_checksums[algo]:
				c = ('  ' + cks.comment) if len(cks.comment) > 0 else ''
				fixed_local_lines.append(f"  \'{cks.value}\'{c}")   # single quote these because they usually always are

			fixed_local_lines.append(')')

			# don't i += 1 again
			continue

		else:
			fixed_local_lines.append(line)

		i += 1


	local = '\n'.join(fixed_local_lines) + "\n"

	return (upstream, local)




def _generate_diff_for_pkgbuild(pkg_url: str, upstream_sha: str, local_path: str, ignored_srcs: list[str]) -> Optional[FileDiff]:
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

	upstream, local = _patch_pkgbuild_sources(upstream_lines, local_lines, ignored_srcs)

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




def _generate_diff(pkg_url: str, upstream_sha: str, local_path: str) -> Optional[FileDiff]:
	resp = req.get(f"{UPSTREAM_URL_BASE}/api/v4/projects/{pkg_url}/repository/blobs/{upstream_sha}/raw")
	if resp.status_code != 200:
		msg.error(f"Could not fetch upstream file content: {resp.text}")
		return None

	files = ["-", os.path.normpath(local_path)]
	if not os.path.exists(local_path):
		files.reverse()

	# ignore the exit code
	filename = os.path.basename(local_path)
	output = subprocess.run(["diff", "-Nd", "--unified=1", f"--label={filename}", f"--label={filename}", *files],
		text=True, input=resp.text, check=False, capture_output=True)

	if output.returncode == 2:
		msg.warn2(f"Diff produced an error: {output.stderr}")
		return None
	elif output.returncode == 0:
		return FileDiff(name=filename, diff="", upstream=resp.text)

	return FileDiff(name=filename, diff=output.stdout, upstream=resp.text)


def diff_package(pkg_path: str, quiet: bool = False) -> Optional[PackageDiff]:
	pkgbuild_path = f"{pkg_path}/PKGBUILD"
	if not os.path.exists(pkgbuild_path):
		return None

	pkgbase = util.get_srcinfo(pkgbuild_path).pkgbase
	if not quiet:
		msg.log(f"Processing {pkgbase}")

	ignored_srcs: list[str] = [".SRCINFO", "*.desktop"]
	if os.path.exists(f"{pkg_path}/.pmdiffignore"):
		with open(f"{pkg_path}/.pmdiffignore", "r") as f:
			ignored_srcs.extend(map(lambda s: s.strip(), f.readlines()))

			# weirdge
			if "PKGBUILD" in ignored_srcs:
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

		if not any(fnmatch.fnmatch(obj["name"], x) for x in ignored_srcs):
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
				patch = _generate_diff_for_pkgbuild(pkg_url, sha, f"{pkg_path}/{name}", ignored_srcs)
			else:
				patch = _generate_diff(pkg_url, sha, f"{pkg_path}/{name}")

			if patch is None:
				continue

			yield patch

	return PackageDiff(generator(pkg_path, files))

