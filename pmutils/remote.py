#!/usr/bin/env python
# Copyright (c) 2024, zhiayang
# SPDX-License-Identifier: Apache-2.0

import os
import json
import fnmatch
import contextlib
import subprocess

from typing import *
from pmutils import msg, util
from dataclasses import dataclass

import requests as req
import urllib.parse as urlparse

UPSTREAM_URL_BASE = f"https://gitlab.archlinux.org"
PACKAGE_NAMESPACE = f"archlinux/packaging/packages"

PMDIFF_JSON_FILE = ".changes.json"
DEFAULT_IGNORE_FILES = [".SRCINFO"]

@dataclass(frozen=True)
class FileDiff:
	name: str
	diff: str
	upstream: str
	new: bool = False


def get_file_list(repo_url: str, ignored_srcs: list[str], commit: Optional[str] = None) -> Optional[list[tuple[str, str]]]:
	resp = req.get(f"{repo_url}/tree", params=({"ref": commit} if commit else {}))
	if resp.status_code != 200:
		msg.error2(f"Could not fetch upstream package info: {resp.text}")
		return None

	resp_json: list[Any] = resp.json()
	files: list[tuple[str, str]] = []   # (name, id)

	for obj in resp_json:
		obj: dict[str, Any]

		# skip folders... haven't seen a PKGBUILD put important stuff in a folder before.
		if obj["type"] == "tree":
			continue

		if not any(fnmatch.fnmatch(obj["name"], x) for x in ignored_srcs):
			files.append((obj["name"], obj["id"]))

	return files


def _diff_file(repo_url: str, upstream_sha: str, local_path: str) -> Optional[FileDiff]:
	resp = req.get(f"{repo_url}/blobs/{upstream_sha}/raw")
	if resp.status_code != 200:
		msg.error(f"Could not fetch upstream file content: {resp.text}")
		return None

	filename = os.path.basename(local_path)
	if not os.path.exists(local_path):
		return FileDiff(name=filename, diff="", upstream=resp.text, new=True)

	# ignore the exit code
	output = subprocess.run(["diff", "-Nd", "--unified=0", f"--label={filename}", f"--label={filename}",
		"-", os.path.normpath(local_path)], text=True, input=resp.text, check=False, capture_output=True)

	if output.returncode == 2:
		msg.warn2(f"Diff produced an error: {output.stderr}")
		return None
	elif output.returncode == 0:
		return FileDiff(name=filename, diff="", upstream=resp.text)

	return FileDiff(name=filename, diff=output.stdout, upstream=resp.text)



@dataclass
class PmDiffFile:
	upstream_commit: str
	diff_files: list[str]
	clean_files: list[str]
	ignore_files: list[str]

	@staticmethod
	def load(path: str = PMDIFF_JSON_FILE) -> Optional["PmDiffFile"]:
		if not os.path.exists(path):
			return None

		with open(path, "r") as f:
			j = json.loads(f.read())
			if ("upstream_commit" in j) and not isinstance(j["upstream_commit"], str):
				msg.warn2(f"Required key `upstream_commit` not a string in `{PMDIFF_JSON_FILE}`, ignoring")
				return None
			elif ("diff_files" in j) and not isinstance(j["diff_files"], list):
				msg.warn2(f"Required key `diff_files` not a list in `{PMDIFF_JSON_FILE}`, ignoring")
				return None
			elif ("clean_files" in j) and not isinstance(j["clean_files"], list):
				msg.warn2(f"Required key `clean_files` not a list in `{PMDIFF_JSON_FILE}`, ignoring")
				return None
			elif ("ignore_files" in j) and not isinstance(j["ignore_files"], list):
				msg.warn2(f"Required key `ignore_files` not a list in `{PMDIFF_JSON_FILE}`, ignoring")
				return None

			return PmDiffFile(
				upstream_commit=j["upstream_commit"],
				diff_files=list(map(str, j.get("diff_files", []))),
				clean_files=list(map(str, j.get("clean_files", []))),
				ignore_files=list(map(str, j.get("ignore_files", []))) + DEFAULT_IGNORE_FILES
			)

	def save(self, path: str = PMDIFF_JSON_FILE):
		with open(path, "w") as f:
			f.write(json.dumps({
				"upstream_commit": self.upstream_commit,
				"diff_files": self.diff_files,
				"clean_files": self.clean_files,
				"ignore_files": self.ignore_files,
			}, indent=2))





def _generator(repo_url: str, ignored_srcs: list[str], upstream_sha: Optional[str]):
	if upstream_sha is None:
		if (r := req.get(f"{repo_url}/commits/main")).status_code != 200:
			msg.error2(f"Failed to get commit hash: {r.text}")
			return None

		upstream_sha = cast(dict[str, str], r.json())["id"]

	if (upstream_files := get_file_list(repo_url, ignored_srcs, upstream_sha)) is None:
		return None

	diff_files: list[str] = []
	clean_files: list[str] = []

	for file in upstream_files:
		# diff all the files accordingly
		if (diff := _diff_file(repo_url, file[1], f"./{file[0]}")) is None:
			continue

		if diff.new or len(diff.diff) == 0:
			clean_files.append(diff.name)
		else:
			diff_files.append(f"{diff.name}.pmdiff")

		yield (diff, None)

	yield (None, PmDiffFile(upstream_sha, diff_files=diff_files, clean_files=clean_files, ignore_files=ignored_srcs))



def diff_package_lazy(pkg_path: str,
	force: bool,
	fetch_latest: bool = False
) -> Optional[Iterator[tuple[Optional[FileDiff], Optional[PmDiffFile]]]]:

	pkgbuild_path = f"{pkg_path}/PKGBUILD"
	if not os.path.exists(pkgbuild_path):
		return None

	pkgbase = util.get_srcinfo(pkgbuild_path).pkgbase

	with contextlib.chdir(pkg_path) as _:
		# run a git diff to see if dirty (if not force)
		if not force and util.check_tree_dirty(pkg_path):
			msg.warn2(f"Package folder '{pkg_path}' is dirty, skipping")
			return None

		# first get the list of files.
		PKG_URL = urlparse.quote(f"{PACKAGE_NAMESPACE}/{pkgbase}", safe='')
		REPO_URL = f"{UPSTREAM_URL_BASE}/api/v4/projects/{PKG_URL}/repository"

		if (pmdiff := PmDiffFile.load(f"./{PMDIFF_JSON_FILE}")) is None:
			msg.log2(f"No {PMDIFF_JSON_FILE}: diffing against latest upstream")
			return _generator(REPO_URL, DEFAULT_IGNORE_FILES, upstream_sha=None)

		else:
			commit = None if fetch_latest else pmdiff.upstream_commit
			return _generator(REPO_URL, pmdiff.ignore_files, upstream_sha=commit)



def save_diff(diff: FileDiff, update_local: bool, keep_old: bool):
	if not diff.new and len(diff.diff) > 0:
		with open(f"{diff.name}.pmdiff", "w") as d:
			d.write(diff.diff)

	if not diff.new and keep_old:
		os.rename(diff.name, f"{diff.name}.old")

	if diff.new or update_local:
		with open(f"{diff.name}", "w") as f:
			f.write(diff.upstream)


def diff_package(pkg_path: str,
	force: bool,
	keep_old: bool = False,
	update_local: bool = False,
	fetch_latest: bool = False) -> bool:

	pkgbuild_path = f"{pkg_path}/PKGBUILD"
	if not os.path.exists(pkgbuild_path):
		return False

	pkgbase = util.get_srcinfo(pkgbuild_path).pkgbase
	msg.log(f"Processing {pkgbase}")

	gen = diff_package_lazy(pkg_path, force=force, fetch_latest=fetch_latest)
	if gen is None:
		return False

	with contextlib.chdir(pkg_path) as _:
		for diff, pmdiff in gen:
			if diff is None:
				assert pmdiff is not None
				pmdiff.save(f"{PMDIFF_JSON_FILE}")
				break

			msg.log2(f"{diff.name}")
			save_diff(diff, update_local=update_local, keep_old=keep_old)

	return True

