#!/usr/bin/env python
# Copyright (c) 2023, zhiayang
# SPDX-License-Identifier: Apache-2.0

import os
import sys
import tempfile
import subprocess as sp

from typing import *
from pmutils import msg
from pmutils.registry import Registry

def exit_virtual_environment(args: dict[str, str]) -> dict[str, str]:
	if sys.base_prefix == sys.prefix:
		return args.copy()

	msg.log(f"Editing Python venv out of virtual environment")
	env = args.copy()

	venv = env["VIRTUAL_ENV"]

	env.pop("VIRTUAL_ENV")
	env.pop("VIRTUAL_ENV_PROMPT")
	env.pop("_OLD_VIRTUAL_PATH")

	# edit the path
	new_path = ':'.join(filter(lambda x: x != f"{venv}/bin", env["PATH"].split(':')))
	env["PATH"] = new_path
	return env


def makepkg(registry: Registry, *, verify_pgp: bool, check: bool, keep: bool, database: Optional[str],
			upload: bool, install: bool, confirm: bool = True, allow_downgrade: bool = False):

	args = ["makepkg", "-f"]
	if not check:
		args += ["--nocheck"]
	if not verify_pgp:
		args += ["--skippgpcheck"]

	with tempfile.TemporaryDirectory() as tmp:
		env = exit_virtual_environment(dict(os.environ))
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
						repo.database.add(f"{tmp}/{pkg}", verbose=True, allow_downgrade=allow_downgrade)

				if upload:
					repo.sync()

		if install:
			msg.log("Installing package(s)")
			try:
				sp.check_call(["sudo", "pacman", *([] if confirm else ["--noconfirm"]),
					"-U", *[f"{tmp}/{x}" for x in packages]])
			except:
				msg.error_and_exit("Failed to install package!")

		# if we're keeping, move them somewhere that's not the temp dir
		if keep:
			msg.log("Moving package(s) to /pm/pkgs")
			for pkg in packages:
				os.rename(f"{tmp}/{pkg}", f"/pm/pkgs/{os.path.basename(pkg)}")
