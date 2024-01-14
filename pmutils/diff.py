#!/usr/bin/env python
# Copyright (c) 2024, zhiayang
# SPDX-License-Identifier: Apache-2.0

import os
import io
import re
import shlex
import fnmatch
import tempfile
import contextlib
import subprocess

from typing import *
from pmutils import msg

import requests as req
import urllib.parse as urlparse

UPSTREAM_URL_BASE = f"https://gitlab.archlinux.org"
PACKAGE_NAMESPACE = f"archlinux/packaging/packages"

def get_pkgbase(pkgbuild: str) -> Optional[str]:
	# extract the package name from the pkgbuild with makepkg.
	# the pkgbuild must be in the current working dir...
	abs_path = os.path.realpath(pkgbuild)
	with contextlib.chdir(os.path.dirname(abs_path)) as _:
		srcinfo = subprocess.check_output(["makepkg",
			"--printsrcinfo", "-p", os.path.basename(abs_path)
		]).decode("utf-8").splitlines()

		for key in srcinfo:
			if key.startswith("pkgbase = "):
				return key.split('=', maxsplit=1)[1].strip()

	return None


def generate_diff(pkg_url: str, upstream_sha: str, local_path: str) -> Optional[str]:
	resp = req.get(f"{UPSTREAM_URL_BASE}/api/v4/projects/{pkg_url}/repository/blobs/{upstream_sha}/raw")
	if resp.status_code != 200:
		msg.error(f"Could not fetch upstream PKGBUILD content: {resp.text}")
		return None

	# ignore the exit code
	output = subprocess.run(["diff", "-du", "-", os.path.normpath(local_path)],
		text=True, input=resp.text, check=False, capture_output=True)

	if output.returncode == 2:
		msg.warn(f"Diff produced an error: {output.stderr}")
		return None
	elif output.returncode == 0:
		return ""

	return output.stdout


def patch_offensive_lines_in_pkgbuild(lines: list[str]) -> list[str]:
	i: int = 0

	ret: list[str] = []
	while i < len(lines):
		line = lines[i].strip()

		if line.startswith("pkgrel="):
			ret.append(f"pkgrel=69")
		elif line.startswith(f"pkgver="):
			ret.append(f"pkgver=69.420")
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

def generate_diff_for_pkgbuild(pkg_url: str, upstream_sha: str, local_path: str) -> Optional[str]:
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

	upstream = patch_offensive_lines_in_pkgbuild(resp.text.splitlines())
	local = patch_offensive_lines_in_pkgbuild(open(local_path, "r").read().splitlines())

	with tempfile.NamedTemporaryFile("w") as f1, tempfile.NamedTemporaryFile("w") as f2:
		f1.write('\n'.join(upstream) + "\n")
		f2.write('\n'.join(local) + "\n")
		f1.flush()
		f2.flush()

		# ignore the exit code
		output = subprocess.run(["diff", "-du", f1.name, f2.name],
			text=True, check=False, capture_output=True)

		if output.returncode == 2:
			msg.warn(f"Diff produced an error: {output.stderr}")
			return None
		elif output.returncode == 0:
			return ""
		else:
			return output.stdout




def diff_package(pkg_path: str) -> bool:
	pkgbuild_path = f"{pkg_path}/PKGBUILD"
	if not os.path.exists(pkgbuild_path):
		return False

	pkgbase = get_pkgbase(pkgbuild_path)
	if pkgbase is None:
		msg.warn(f"Could not extract `pkgbase` from PKGBUILD; skipping")
		return False

	msg.log(f"Processing {pkgbase}")

	ignored: list[str] = [".SRCINFO", "*.desktop"]
	if os.path.exists(f"{pkg_path}/.pmdiffignore"):
		with open(f"{pkg_path}/.pmdiffignore", "r") as f:
			ignored.extend(f.readlines())

			# weirdge
			if "PKGBUILD" in ignored:
				msg.log2(f"{msg.yellow('Ignoring upstream PKGBUILD')}")

	# first get the list of files.
	pkg_url = urlparse.quote(f"{PACKAGE_NAMESPACE}/{pkgbase}", safe='')
	request_url = f"{UPSTREAM_URL_BASE}/api/v4/projects/{pkg_url}/repository/tree"

	resp = req.get(request_url)
	if resp.status_code != 200:
		msg.error(f"Could not fetch upstream package info: {resp.text}")
		return False

	resp_json: list[Any] = resp.json()
	files: list[tuple[str, str]] = []   # (name, id)

	found_pkgbuild = False
	for obj in resp_json:
		obj: dict[str, Any]
		# skip folders... haven't seen a PKGBUILD put important stuff in a folder before.
		if obj["type"] == "tree":
			continue

		if obj["name"] == "PKGBUILD":
			found_pkgbuild = True

		if not any(fnmatch.fnmatch(obj["name"], x) for x in ignored):
			files.append((obj["name"], obj["id"]))

	if not found_pkgbuild:
		msg.warn(f"Could not find PKGBUILD in upstream package; skipping")
		return False

	for (name, sha) in files:
		msg.log2(f"{name}")

		if name == "PKGBUILD":
			patch = generate_diff_for_pkgbuild(pkg_url, sha, f"{pkg_path}/{name}")
		else:
			patch = generate_diff(pkg_url, sha, f"{pkg_path}/{name}")

		if (patch is None) or len(patch) == 0:
			continue
		with open(f"{pkg_path}/{name}.pmdiff", "w") as f:
			f.write(patch)

	return True

