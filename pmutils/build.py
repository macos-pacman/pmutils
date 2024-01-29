#!/usr/bin/env python3
# Copyright (c) 2023, zhiayang
# SPDX-License-Identifier: Apache-2.0

import os
import re
import sys
import tempfile
import subprocess as sp

from typing import *
from pmutils import msg
from pmutils.registry import Registry


def exit_virtual_environment(args: dict[str, str]) -> dict[str, str]:
	if sys.base_prefix == sys.prefix:
		return args.copy()

	msg.log(f"Editing Python venv out of environment")
	env = args.copy()

	venv = env["VIRTUAL_ENV"]

	env.pop("VIRTUAL_ENV")
	env.pop("VIRTUAL_ENV_PROMPT")
	env.pop("_OLD_VIRTUAL_PATH")

	# edit the path
	new_path = ':'.join(filter(lambda x: x != f"{venv}/bin", env["PATH"].split(':')))
	env["PATH"] = new_path
	return env


def makepkg(
    registry: Registry,
    *,
    verify_pgp: bool,
    check: bool,
    keep: bool,
    database: Optional[str],
    upload: bool,
    install: bool,
    allow_downgrade: bool,
    update_buildnum: bool,
    confirm: bool = True
):

	args = ["makepkg", "-f"]
	if not check:
		args += ["--nocheck"]
	if not verify_pgp:
		args += ["--skippgpcheck"]

	if update_buildnum:
		if not os.path.exists("PKGBUILD"):
			msg.error_and_exit(f"Could not find PKGBUILD in the current directory")

		new_name = ".PKGBUILD.new"

		new_lines: list[str] = []
		old_buildnum: Optional[int] = None
		new_buildnum: Optional[int] = None
		pkgrel_line_idx: Optional[int] = None

		found_buildnum = False

		with open("PKGBUILD", "r") as pkgbuild:
			for line_idx, line in enumerate(pkgbuild.read().splitlines()):
				if (m := re.fullmatch(r"pkgrel\+=\"(?:\.(\d+))?\"", line)) is not None:
					if (buildnum := m.groups()[0]) is None:
						# it was empty (pkgrel+=""), so make it .1
						new_lines.append('pkgrel+=".1"')
					else:
						old_buildnum = int(buildnum)
						new_buildnum = old_buildnum + 1
						new_lines.append(f'pkgrel+=".{new_buildnum}"')
					found_buildnum = True
				else:
					new_lines.append(line)
					if line.startswith("pkgrel="):
						pkgrel_line_idx = line_idx

		if not found_buildnum:
			assert pkgrel_line_idx is not None
			new_lines = new_lines[:1 + pkgrel_line_idx] + ['pkgrel+=".1"'] + new_lines[1 + pkgrel_line_idx:]

		with open(new_name, "w") as new:
			new.write('\n'.join(new_lines))
			new.write("\n")

		msg.log2(f"Updating build number: ", end='')
		if old_buildnum:
			print(f"{msg.GREY}{old_buildnum}{msg.ALL_OFF} -> {msg.GREEN}{new_buildnum or 1}{msg.ALL_OFF}")
		else:
			print(f"{msg.GREEN}{new_buildnum or 1}{msg.ALL_OFF}")

		os.rename(new_name, "PKGBUILD")

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
				sp.check_call([
				    "sudo", "pacman", *([] if confirm else ["--noconfirm"]), "-U", *[f"{tmp}/{x}" for x in packages]
				])
			except:
				msg.error_and_exit("Failed to install package!")

		# if we're keeping, move them somewhere that's not the temp dir
		if keep:
			msg.log("Moving package(s) to /pm/pkgs")
			for pkg in packages:
				os.rename(f"{tmp}/{pkg}", f"/pm/pkgs/{os.path.basename(pkg)}")
