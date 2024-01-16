# rebase.py
# Copyright (c) 2024, zhiayang
# SPDX-License-Identifier: Apache-2.0

import os
import hashlib
import subprocess
import contextlib

from typing import *
from pmutils import msg, diff, build, util
from pmutils.registry import Registry, Repository

CHECKSUM_ALGOS = ["ck", "md5", "sha1", "sha224", "sha256", "sha384", "sha512", "b2"]

def _calc_checksum(kind: str, path: str) -> str:
	if kind == "ck":
		return subprocess.check_output(["cksum", path], text=True).split(' ')[0]

	with open(path, "rb") as f:
		data = f.read()
		if kind == "md5":
			return hashlib.md5(data).hexdigest()
		elif kind == "sha1":
			return hashlib.sha1(data).hexdigest()
		elif kind == "sha224":
			return hashlib.sha224(data).hexdigest()
		elif kind == "sha256":
			return hashlib.sha256(data).hexdigest()
		elif kind == "sha384":
			return hashlib.sha384(data).hexdigest()
		elif kind == "sha512":
			return hashlib.sha512(data).hexdigest()
		elif kind == "b2":
			return hashlib.blake2b(data, digest_size=512//8).hexdigest()
		else:
			msg.error_and_exit(f"Unsupported checksum algorithm '{kind}'")


def _patch_file(d: diff.FileDiff, keep_diffs: bool, upstream_srcinfo: util.SrcInfo, hash_replacements: dict[str, str]) -> bool:
	new_name = f"{d.name}.new"
	with open(new_name, "w") as f:
		f.write(d.upstream)

	msg.log2(f"{d.name}", end='')
	if len(d.diff) == 0:
		print(f": {msg.pink('no changes')}")
		return True

	# check whether the thing can apply first
	rc = subprocess.run(["patch", "--check", "-Ns", "-Vnone", new_name], input=d.diff,
		text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode

	if keep_diffs or rc != 0:
		with open(f"{d.name}.pmdiff", "w") as f:
			f.write(d.diff)

	if rc != 0:
		# for failed patches, write out the pmdiff.
		print(f": {msg.red('failed')}")
		return False

	# ok, it can apply. now apply it for real.
	subprocess.run(["patch", "-Ns", "-Vnone", new_name], input=d.diff, text=True, check=True)
	os.rename(new_name, d.name)
	print(f": {msg.green('ok')}")

	if d.name != "PKGBUILD":
		for src_idx, src in enumerate(upstream_srcinfo.fields["source"]):
			if src == d.name:
				for ca in CHECKSUM_ALGOS:
					if f"{ca}sums" in upstream_srcinfo.fields:
						hash_replacements[upstream_srcinfo.fields[f"{ca}sums"][src_idx]] = _calc_checksum(ca, d.name)
				break

	return True


def rebase_package(pkg_dir: str, force: bool, keep_diffs: bool, *,
	registry: Optional[Registry] = None, repository: Optional[Repository] = None,
	build_pkg: bool = False, install_pkg: bool = False, check_pkg: bool = False,
	upload: bool = False, commit: bool = False, allow_downgrade: bool = False) -> bool:

	if not os.path.exists(pkg_dir) or not os.path.isdir(pkg_dir):
		msg.warn(f"Skipping nonexistent folder '{pkg_dir}'")
		return True
	elif not os.path.exists(f"{pkg_dir}/PKGBUILD"):
		msg.warn(f"Skipping folder '{pkg_dir}' without PKGBUILD")
		return True

	have_fails = False
	pkgname = os.path.basename(pkg_dir)
	msg.log(f"Updating {pkgname}")

	with contextlib.chdir(pkg_dir) as _:
		# run a git diff to see if dirty (if not force)
		if not force:
			git = subprocess.run(["git", "diff-index", "--name-only", "--relative", "HEAD"],
				check=False, capture_output=True, text=True)
			if git.returncode == 0 and len(git.stdout) > 0:
				msg.warn2(f"Package folder '{pkg_dir}' is dirty, skipping")
				return False

	# make the diffs
	if (pd := diff.diff_package(pkg_path=pkg_dir, quiet=True)) is None:
		return False

	# first get the srcinfo of the current PKGBUILD to get which checksums we need
	with contextlib.chdir(pkg_dir) as _:
		pkgbuild = next(pd.files)
		assert pkgbuild.name == "PKGBUILD"

		hash_replacements: dict[str, str] = {}
		local_srcinfo = util.get_srcinfo("./PKGBUILD")
		upstream_srcinfo = util.get_srcinfo_from_string(pkgbuild.upstream)

		for d in pd.files:
			if not _patch_file(d, keep_diffs, upstream_srcinfo, hash_replacements):
				have_fails = True

		# ok now that we have all the hash replacements, patch the PKGBUILD, then replace the hashes
		_patch_file(pkgbuild, keep_diffs, upstream_srcinfo, hash_replacements)
		with open(f"PKGBUILD", "r") as orig, open(f"PKGBUILD.tmp", "w") as new:
			contents = orig.read()
			for s, r in hash_replacements.items():
				contents = contents.replace(s, r)
			new.write(contents)

		os.rename("PKGBUILD.tmp", "PKGBUILD")

		lv = local_srcinfo.version()
		uv = upstream_srcinfo.version()
		if uv < lv:
			msg.warn2(f"Upstream version '{uv}' is older than local '{lv}'{' (not installing)' if install_pkg else ''}")
			install_pkg = False

		if (build_pkg or install_pkg) and (not have_fails):
			if commit:
				try:
					# see if there are changes at all
					if subprocess.check_output(["git", "status", "--porcelain", "."], text=True).strip() != "":
						# note: we are still in the pkg directory.
						msg.log2(f"Commiting changes:")
						subprocess.check_call(["git", "add", "-A", "."])
						subprocess.check_call(["git", "commit", "-qam", f"{pkgname}: update to {uv}"])
					else:
						msg.log2(f"No changes to commit")
				except:
					msg.warn2(f"Commit failed!")

			assert registry is not None
			assert repository is not None

			build.makepkg(registry=registry, verify_pgp=False, check=check_pkg, keep=(not upload),
				database=repository.name, upload=upload, install=install_pkg,
				confirm=False, allow_downgrade=allow_downgrade)


	if have_fails:
		msg.warn2(f"Some patches failed to apply; use `.pmdiff` files to update manually")

	return not have_fails
