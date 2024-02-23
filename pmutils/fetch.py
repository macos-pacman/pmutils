#!/usr/bin/env python3
# Copyright (c) 2024, zhiayang
# SPDX-License-Identifier: Apache-2.0

import os
import contextlib
import requests as req
import urllib.parse as urlparse

from typing import *
from pmutils import msg, diff


def fetch_upstream_package(root_dir: str, pkg_name: str, force: bool) -> bool:
	msg.log(f"Fetching {pkg_name}")

	pkg_dir = f"{root_dir}/{pkg_name}"
	if not force and os.path.exists(f"{pkg_dir}"):
		msg.error2(f"Path '{pkg_dir}' already exists!")
		return False

	os.makedirs(f"{pkg_dir}", exist_ok=force)

	PKG_URL = urlparse.quote(f"{diff.PACKAGE_NAMESPACE}/{pkg_name}", safe='')
	REPO_URL = f"{diff.UPSTREAM_URL_BASE}/api/v4/projects/{PKG_URL}/repository"

	if (r := req.get(f"{REPO_URL}/commits/main")).status_code != 200:
		msg.error2(f"Failed to get commit hash: {r.text}")
		return False

	commit_sha = cast(dict[str, str], r.json())["id"]
	msg.log2(f"Commit: {commit_sha}")

	with contextlib.chdir(pkg_dir) as _:
		if (files := diff.get_file_list(REPO_URL, diff.DEFAULT_IGNORE_FILES, commit_sha)) is None:
			return False

		for file in files:
			msg.log2(f"{file[0]}")
			resp = req.get(f"{REPO_URL}/blobs/{file[1]}/raw")

			if resp.status_code != 200:
				msg.error2(f"Could not fetch file content: {resp.text}")
				return False

			with open(file[0], "w") as f:
				f.write(resp.text)

		diff.PmDiffFile(commit_sha, [], [], diff.DEFAULT_IGNORE_FILES).save()

	return True
