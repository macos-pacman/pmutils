#!/usr/bin/env python3
# Copyright (c) 2023, yuki
# SPDX-License-Identifier: Apache-2.0

import os
import re
import sys
import enum
import json
import tempfile
import subprocess as sp

from typing import *
from pmutils import msg
from pmutils.registry import Registry
from pmutils.makepkg import PackageBuilder
from pmutils.diff import PMDIFF_JSON_FILE


def exit_virtual_environment(args: dict[str, str]) -> dict[str, str]:
	if sys.base_prefix == sys.prefix:
		return args.copy()

	msg.log(f"Editing Python venv out of environment")
	env = args.copy()

	venv = env["VIRTUAL_ENV"]

	env.pop("VIRTUAL_ENV")
	env.pop("VIRTUAL_ENV_PROMPT")

	if "_OLD_VIRTUAL_PATH" in env:
		env.pop("_OLD_VIRTUAL_PATH")

	# edit the path
	new_path = ':'.join(filter(lambda x: x != f"{venv}/bin", env["PATH"].split(':')))
	env["PATH"] = new_path
	return env


class BuildNumMode(enum.Enum):
	INCREMENT = 1
	DECREMENT = 2
	RESET = 3


def edit_build_number(mode: BuildNumMode, use_major: bool) -> tuple[Optional[int], Optional[int]]:
	new_lines: list[str] = []
	old_buildnum: Optional[int] = None
	new_buildnum: Optional[int] = None
	pkgrel_line_idx: Optional[int] = None

	found_buildnum = False

	with open("PKGBUILD", "r") as pkgbuild:
		for line_idx, line in enumerate(pkgbuild.read().splitlines()):
			if use_major:
				pat = r"pkgrel=(\d+)"
			else:
				pat = r"pkgrel\+=\"(?:\.(\d+))?\""

			if (m := re.fullmatch(pat, line)) is not None:
				if (buildnum := m.groups()[0]) is None:
					# it was empty (pkgrel+=""), so make it .1
					# (this should not happen for major pkgrel -- we should always find it)
					assert not use_major

					if mode in [BuildNumMode.INCREMENT, BuildNumMode.RESET]:
						new_lines.append('pkgrel+=".1"')
						new_buildnum = 1
				else:
					old_buildnum = int(buildnum)
					if mode == BuildNumMode.INCREMENT:
						new_buildnum = old_buildnum + 1
					elif mode == BuildNumMode.DECREMENT:
						new_buildnum = old_buildnum - 1
					elif mode == BuildNumMode.RESET:
						new_buildnum = 1
					else:
						msg.error_and_exit("?!")

					if use_major:
						new_lines.append(f'pkgrel={new_buildnum}')
					else:
						new_lines.append(f'pkgrel+=".{new_buildnum}"')

				found_buildnum = True

			else:
				new_lines.append(line)
				if line.startswith("pkgrel="):
					pkgrel_line_idx = line_idx

	if use_major and not found_buildnum:
		msg.error_and_exit(f"Malformed PKGBUILD: missing `pkgrel` specification")

	if (mode in [BuildNumMode.INCREMENT, BuildNumMode.RESET]) and not found_buildnum:
		assert not use_major
		assert pkgrel_line_idx is not None
		new_lines = new_lines[:1 + pkgrel_line_idx] + ['pkgrel+=".1"'] + new_lines[1 + pkgrel_line_idx:]

	new_name = ".PKGBUILD.new"
	with open(new_name, "w") as new:
		new.write('\n'.join(new_lines))
		new.write("\n")

	os.rename(new_name, "PKGBUILD")

	return (old_buildnum, new_buildnum)


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
    sync: bool,
    use_sandbox: bool,
    sandbox_folder: Optional[str],
    sandbox_keep: bool,
    confirm: bool = True,
):
	conn = PackageBuilder(use_sandbox)
	untracked_package: bool = False

	args: list[str] = []
	if not check:
		args += ["--nocheck"]
	if not verify_pgp:
		args += ["--skippgpcheck"]

	# TODO: update the buildnum after the build succeeds, instead of before then rollback if fail
	if update_buildnum:
		if not os.path.exists("PKGBUILD"):
			msg.error_and_exit(f"Could not find PKGBUILD in the current directory")

		if os.path.exists(PMDIFF_JSON_FILE):
			with open(PMDIFF_JSON_FILE, "r") as x:
				pmdiff = json.load(x)

			# if the package is untracked (ie. not from upstream), then we
			# should just update the pkgrel integer directly rather than do the +=".1" thing.
			if pmdiff.get("untracked", False):
				untracked_package = True

		msg.log2(f"Updating {'pkgrel' if untracked_package else 'build number'}: ", end='')
		(old_buildnum, new_buildnum) = edit_build_number(BuildNumMode.INCREMENT, use_major=untracked_package)

		if old_buildnum:
			print(f"{msg.GREY}{old_buildnum}{msg.ALL_OFF} -> {msg.GREEN}{new_buildnum or 1}{msg.ALL_OFF}")
		else:
			print(f"{msg.GREEN}{new_buildnum or 1}{msg.ALL_OFF}")

	def rollback_buildnum():
		if update_buildnum:
			msg.log2(f"Rolling back {'pkgrel' if untracked_package else 'build number'}: ", end='')
			(old_buildnum, new_buildnum) = edit_build_number(BuildNumMode.DECREMENT, use_major=untracked_package)
			if old_buildnum:
				print(f"{msg.RED}{new_buildnum}{msg.ALL_OFF} <- {msg.GREY}{old_buildnum or 1}{msg.ALL_OFF}")
			else:
				print(f"{msg.RED}{new_buildnum or 1}{msg.ALL_OFF}")

	# now that the buildnum has been updated, wrap the whole thing in a try-except
	# so we rollback in case of *any* problem.
	try:
		env = exit_virtual_environment(dict(os.environ))
		with tempfile.TemporaryDirectory() as tmp:
			packages = conn.makepkg(
			    args,
			    env=env,
			    pkgdest=tmp,
			    check=check,
			    sync=sync,
			    sandbox_folder=sandbox_folder,
			    sandbox_keep=sandbox_keep,
			)

			if packages is None:
				msg.error("Failed to build package!")
				rollback_buildnum()
				sys.exit(1)

			msg.log(f"Successfully built {len(packages)} package{'' if len(packages) == 1 else 's'}")

			if database is not None:
				repo = registry.get_repository(database)
				if repo is None:
					msg.error(f"Repository {repo} does not exist")
				else:
					with msg.Indent():
						for pkg in packages:
							repo.add_package(f"{tmp}/{pkg}", verbose=True, allow_downgrade=allow_downgrade)

					repo.sync(upload)

			if install:
				msg.log("Installing package(s)")
				try:
					sp.check_call([
					    "sudo",
					    "pacman",
					    *([] if confirm else ["--noconfirm"]),
					    "-U",
					    *[f"{tmp}/{x}" for x in packages],
					])
				except:
					msg.error_and_exit("Failed to install package!")

			# if we're keeping, move them somewhere that's not the temp dir
			if keep:
				msg.log("Moving package(s) to /pm/pkgs")
				for pkg in packages:
					os.rename(f"{tmp}/{pkg}", f"/pm/pkgs/{os.path.basename(pkg)}")

	except KeyboardInterrupt:
		rollback_buildnum()
		msg.error_and_exit("Aborted!")
