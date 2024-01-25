# rebase.py
# Copyright (c) 2024, zhiayang
# SPDX-License-Identifier: Apache-2.0

import os
import subprocess
import contextlib

from typing import *
from pmutils import msg, remote, build, util
from pmutils.registry import Registry, Repository


def _patch_file(name: str, upstream_content: str, diff_content: str) -> bool:
	new_name = f"{name}.new"

	msg.log2(f"{name}", end='')
	with open(new_name, "w") as dst:
		dst.write(upstream_content)

	# check whether the thing can apply first
	rc = subprocess.run(["patch", "--check", "-Ns", "-Vnone", new_name],
	                    input=diff_content,
	                    text=True,
	                    stdout=subprocess.DEVNULL,
	                    stderr=subprocess.DEVNULL).returncode

	if rc != 0:
		# for failed patches, write out the pmdiff.
		print(f": {msg.red('failed')}")
		return False

	# ok, it can apply. now apply it for real.
	subprocess.run(["patch", "-ENs", "-Vnone", new_name], input=diff_content, text=True, check=True)

	os.rename(new_name, name)
	print(f": {msg.green('ok')}")

	return True


def rebase_package(
    pkg_dir: str,
    force: bool,
    *,
    registry: Optional[Registry] = None,
    repository: Optional[Repository] = None,
    build_pkg: bool,
    install_pkg: bool,
    check_pkg: bool,
    upload: bool,
    commit: bool,
    allow_downgrade: bool,
    update_buildnum: bool
) -> bool:

	if not os.path.exists(pkg_dir) or not os.path.isdir(pkg_dir):
		msg.warn(f"Skipping nonexistent folder '{pkg_dir}'")
		return True
	elif not os.path.exists(f"{pkg_dir}/PKGBUILD"):
		msg.warn(f"Skipping folder '{pkg_dir}' without PKGBUILD")
		return True

	have_fails = False
	pkgname = os.path.basename(os.path.realpath(pkg_dir))
	msg.log(f"Updating {pkgname}")

	with contextlib.chdir(pkg_dir) as _:
		if (pmdiff := remote.PmDiffFile.load()) is None:
			msg.warn2(f"Changes file missing! Run `pm diff` first")
			return False

		# make the diffs (note: diff_package already checks for a dirty working dir)
		if (gen := remote.diff_package_lazy(pkg_path=pkg_dir, force=force, fetch_latest=True)) is None:
			return False

		local_srcinfo = util.get_srcinfo("./PKGBUILD")
		for d, _ in gen:
			if d is None:
				break

			# if there's no diff:
			diff_name = f"{d.name}.pmdiff"
			if not os.path.exists(diff_name):
				if (not d.new) and (d.name not in pmdiff.clean_files):
					msg.warn2(f"Diff file `{diff_name}` is missing!")
					continue
				else:
					# just write the file out.
					with open(d.name, "w") as f:
						f.write(d.upstream)
						continue

			with open(f"{d.name}.pmdiff", "r") as df:
				have_fails |= (not _patch_file(name=d.name, upstream_content=d.upstream, diff_content=df.read()))

		# get the srcinfo again, after patching
		new_srcinfo = util.get_srcinfo("./PKGBUILD")

	new_ver = new_srcinfo.version()
	old_ver = local_srcinfo.version()
	if new_ver < old_ver:
		msg.warn2(f"Upstream version '{new_ver}' is " + \
         f"older than local '{old_ver}'{' (not installing)' if install_pkg else ''}")

		install_pkg = False
	elif old_ver == new_ver:
		msg.log2(f"Version: {msg.GREEN}{old_ver}{msg.ALL_OFF}")
	else:
		msg.log2(
		    f"Version: {msg.GREY}{old_ver}{msg.ALL_OFF} {msg.BOLD}->{msg.ALL_OFF} {msg.GREEN}{new_ver}{msg.ALL_OFF}"
		)

	with contextlib.chdir(pkg_dir) as _:
		if commit and (not have_fails):
			try:
				# see if there are changes at all
				if subprocess.check_output(["git", "status", "--porcelain", "."], text=True).strip() != "":
					# note: we are still in the pkg directory.
					msg.log2(f"Commiting changes")
					subprocess.check_call(["git", "add", "-A", "."])
					subprocess.check_call(["git", "commit", "-qam", f"{pkgname}: update to {new_ver}"])
				else:
					msg.log2(f"No changes to commit")
			except:
				msg.warn2(f"Commit failed!")

		if (build_pkg or install_pkg) and (not have_fails):

			assert registry is not None
			assert repository is not None

			build.makepkg(
			    registry=registry,
			    verify_pgp=False,
			    check=check_pkg,
			    keep=(not upload),
			    database=repository.name,
			    upload=upload,
			    install=install_pkg,
			    confirm=True,
			    allow_downgrade=allow_downgrade,
			    update_buildnum=update_buildnum
			)

	if have_fails:
		msg.warn2(f"Some patches failed to apply; use `.pmdiff` files to update manually")

	return not have_fails
