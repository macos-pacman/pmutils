# util.py
# Copyright (c) 2024, yuki
# SPDX-License-Identifier: Apache-2.0

import os
import pyalpm
import shutil
import fnmatch
import hashlib
import tempfile
import contextlib
import subprocess

import tqdm.auto as tqdm
import tqdm.utils as tqdm_utils

from io import BytesIO

from typing import *
from pmutils import msg
from pmutils.package import Version
from dataclasses import dataclass


@dataclass(frozen=True)
class SrcInfo:
	pkgbase: str
	fields: dict[str, list[str]]
	subpkgs: list[tuple[str, dict[str, list[str]]]]

	def version(self) -> Version:
		return Version(int(self.fields.get("epoch", [0])[0]), self.fields["pkgver"][0], self.fields["pkgrel"][0])


pacman_root_dir: Optional[str] = None


def _read_kv(l: str) -> tuple[str, str]:
	a = l.split('=', maxsplit=1)
	return (a[0].strip(), a[1].strip())


def _parse_srcinfo(srcinfo: str) -> SrcInfo:
	fields: dict[str, list[str]] = {}
	subpkgs: list[tuple[str, dict[str, list[str]]]] = []
	pkgbase = ""

	for line in map(lambda s: s.strip(), srcinfo.splitlines()):
		if line.startswith('#') or len(line) == 0:
			continue

		k, v = _read_kv(line)
		if k == "pkgbase":
			pkgbase = v
		elif k == "pkgname":
			subpkgs.append((v, {}))
		else:
			if len(subpkgs) > 0:
				subpkgs[-1][1].setdefault(k, []).append(v)
			else:
				fields.setdefault(k, []).append(v)

	return SrcInfo(pkgbase, fields, subpkgs)


def _get_pacman_root_dir():
	global pacman_root_dir
	if pacman_root_dir is None:
		if (a := shutil.which("pacman")) is None:
			msg.error_and_exit(f"Could not find `pacman` binary!")

		pacman_root_dir = os.path.normpath(f"{os.path.dirname(a)}/../../")


def get_srcinfo(pkgbuild: str) -> SrcInfo:
	global pacman_root_dir
	_get_pacman_root_dir()

	# manually do the things; whatever makepkg does is too slow for some reason.
	cmdline = f"source {pkgbuild} && source {pacman_root_dir}/usr/share/makepkg/srcinfo.sh && write_srcinfo"

	proc = subprocess.run(["bash", "-c", cmdline], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

	if proc.returncode != 0:
		msg.error_and_exit(f"Failed to source PKGBUILD: {proc.stdout}")
	else:
		return _parse_srcinfo(proc.stdout)


def get_srcinfo_from_string(pkgbuild: str) -> SrcInfo:
	global pacman_root_dir
	_get_pacman_root_dir()

	with tempfile.NamedTemporaryFile("w") as f:
		f.write(pkgbuild)
		f.flush()

		cmdline = f"source {f.name} && source {pacman_root_dir}/usr/share/makepkg/srcinfo.sh && write_srcinfo"

		srcinfo = subprocess.check_output(["bash", "-c", cmdline], text=True)
		return _parse_srcinfo(srcinfo)


def check_tree_dirty(path: str, check_patterns: list[str] = []) -> bool:
	with contextlib.chdir(path) as _:
		# run a git diff to see if dirty (if not force)
		git = subprocess.run(["git", "diff-index", "--name-only", "--relative", "HEAD"],
		                     check=False,
		                     capture_output=True,
		                     text=True)

		if git.returncode != 0:
			msg.error(f"Error running git: {git.stderr}")
			return True

		if len(git.stdout) > 0:
			if len(check_patterns) == 0:
				return True

			# if there are patterns, then only consider files matching the pattern
			for dirt in git.stdout.splitlines():
				if any(fnmatch.fnmatch(dirt, pat) for pat in check_patterns):
					return True
			return False

	return False


# get the pacman installation prefix
_pacman_prefix: Optional[str] = None


def get_pacman_prefix() -> str:
	global _pacman_prefix
	if _pacman_prefix is not None:
		return _pacman_prefix

	if (which_pacman := shutil.which("pacman")) is None:
		msg.error_and_exit(f"Could not find pacman!")

	_pacman_prefix = os.path.normpath(os.path.join(os.path.dirname(which_pacman), "..", ".."))
	return _pacman_prefix


@dataclass
class PackageDeps:
	depends: set[str]
	optdepends: set[str]
	makedepends: set[str]
	checkdepends: set[str]


# TODO: support non-default DBPath (ie. not /var/lib/pacman)
DB_PATH = f"/var/lib/pacman"


def get_alpm_handle() -> Any:
	PREFIX = get_pacman_prefix()

	if not os.path.exists(f"{PREFIX}/{DB_PATH}"):
		msg.error_and_exit(f"Could not find Pacman database path")

	return pyalpm.Handle("/", f"{PREFIX}/{DB_PATH}")


def get_package_dependencies(handle: Any, package_name: str) -> Optional[PackageDeps]:
	if (pkg := handle.get_localdb().get_pkg(package_name)) is not None:
		# there is no type information in pyalpm...
		return PackageDeps(
		    depends=set(cast(list[str], pkg.depends)),
		    optdepends=set(cast(list[str], pkg.optdepends)),
		    makedepends=set(cast(list[str], pkg.makedepends)),
		    checkdepends=set(cast(list[str], pkg.checkdepends))
		)

	return None


def resolve_transitive_deps(handle: Any, packages: str | Iterable[str], depkind: str) -> set[str]:
	visited: set[str] = set()
	stack: list[str] = []

	if isinstance(packages, str):
		stack.append(packages)
	else:
		stack.extend(packages)

	while len(stack) > 0:
		pkg = stack.pop()
		if pkg in visited:
			continue

		if (d := get_package_dependencies(handle, pkg)) is not None:
			stack.extend(getattr(d, depkind))
			visited.add(pkg)

	return visited


def read_file_chunks_with_progress_bar(
    file: str,
    progress_bar_threshold: int,
    bar_desc: str,
    chunk_callback: Callable[[Any, str, int], None],   # (data, digest, size) -> None
    max_chunk_size: int
):
	with open(file, "rb") as pkg_fd:
		pkg_size = os.path.getsize(file)

		if pkg_size >= progress_bar_threshold:
			bar = tqdm.tqdm(
			    desc=bar_desc,
			    total=pkg_size,
			    unit=f"B",
			    unit_scale=True,
			    unit_divisor=1024,
			    dynamic_ncols=True,
			    miniters=1,
			    maxinterval=0.3,
			    ascii=" ▬",
			    leave=False,
			    bar_format=f"{{desc:<18}}: {msg.blue('[')}{{bar}}{msg.blue(']')} ({{n_fmt:<5}}/{{total_fmt:>5}}"
			    + " [{percentage:>3.0f}%], {rate_fmt:>8}{postfix}) "
			)

			# multi blob: show progress bar
			while True:
				data = pkg_fd.read(max_chunk_size)
				if len(data) == 0:
					break

				blob_io = tqdm_utils.CallbackIOWrapper(bar.update, BytesIO(data))
				chunk_callback(blob_io, hashlib.sha256(data).hexdigest(), len(data))

			bar.close()

		else:
			# just do it without a bar
			data = pkg_fd.read()
			chunk_callback(data, hashlib.sha256(data).hexdigest(), len(data))


def write_file_chunks_with_progress_bar(
    file: str,
    file_size: int,
    progress_bar_threshold: int,
    bar_desc: str,
    data_iterator: Iterable[Any],
):
	with open(file, "wb") as pkg_fd:
		if file_size >= progress_bar_threshold:
			bar = tqdm.tqdm(
			    desc=bar_desc,
			    total=file_size,
			    unit=f"B",
			    unit_scale=True,
			    unit_divisor=1024,
			    dynamic_ncols=True,
			    miniters=1,
			    maxinterval=0.3,
			    ascii=" ▬",
			    leave=False,
			    bar_format=f"{{desc:<18}}: {msg.blue('[')}{{bar}}{msg.blue(']')} ({{n_fmt:<5}}/{{total_fmt:>5}}"
			    + " [{percentage:>3.0f}%], {rate_fmt:>8}{postfix}) "
			)

			# multi blob: show progress bar
			for data in data_iterator:
				bar.update(len(data))
				pkg_fd.write(data)

			bar.close()

		else:
			for data in data_iterator:
				pkg_fd.write(data)
