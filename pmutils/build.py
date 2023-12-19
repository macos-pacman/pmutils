#!/usr/bin/env python
# Copyright (c) 2023, zhiayang
# SPDX-License-Identifier: Apache-2.0

import os
import tempfile
import subprocess as sp

from typing import *
from pmutils import msg
from pmutils.registry import Registry

def makepkg(registry: Registry, *, verify_pgp: bool, check: bool, keep: bool, database: Optional[str],
			skip_upload: bool, install: bool):
	args = ["makepkg", "-f"]
	if not check:
		args += ["--nocheck"]
	if not verify_pgp:
		args += ["--skippgpcheck"]

	with tempfile.TemporaryDirectory() as tmp:
		env = os.environ
		env["PKGDEST"] = tmp
		args += [f"PKGDEST={tmp}"]
		try:
			sp.check_call(args, env=env)
		except:
			msg.error_and_exit("Failed to build package!")

		packages: list[str] = []
		for pkg in os.listdir(tmp):
			if pkg.endswith(".pkg.tar.zst"):
				packages.append(pkg)

		if database is not None:
			repo = registry.get_repository(database)
			if repo is None:
				msg.error(f"Repository {repo} does not exist")
			else:
				with msg.Indent():
					for pkg in packages:
						repo.database.add(f"{tmp}/{pkg}", verbose=True)

				if not skip_upload:
					repo.sync()

		if install:
			msg.log("Installing package(s)")
			try:
				sp.check_call(["sudo", "pacman", "-U", *[ f"{tmp}/{x}" for x in packages ]])
			except:
				msg.error_and_exit("Failed to install package!")

		# if we're keeping, move them somewhere that's not the temp dir
		if keep:
			msg.log("Moving package(s) to /pm/pkgs")
			for pkg in packages:
				os.rename(f"{tmp}/{pkg}", f"/pm/pkgs/{os.path.basename(pkg)}")
