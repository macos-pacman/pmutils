#!/usr/bin/env python
# Copyright (c) 2023, zhiayang
# SPDX-License-Identifier: Apache-2.0

import tarfile
import subprocess
import os.path as path

from pmutils import msg, mimes

from typing import *
from dataclasses import dataclass

@dataclass(eq=True, frozen=True)
class Version:
	epoch: int
	pkgver: str
	pkgrel: str

	def __str__(self) -> str:
		estr = f"{self.epoch}:" if self.epoch > 0 else ""
		return f"{estr}{self.pkgver}-{self.pkgrel}"

	def sanitise(self) -> str:
		return str(self).replace(':', '-').replace('+', '_')

	# call out to pacman because it's the final authority
	def __lt__(self, other: "Version") -> bool:
		return int(subprocess.check_output(["vercmp", str(self), str(other)])) < 0

	def __gt__(self, other: "Version") -> bool:
		return int(subprocess.check_output(["vercmp", str(self), str(other)])) > 0

	def __le__(self, other: "Version") -> bool:
		return self == other or self < other

	def __ge__(self, other: "Version") -> bool:
		return self == other or self > other

	@staticmethod
	def parse(s: str) -> "Version":
		epoch: int = 0

		if ':' in s:
			_epoch, s = s.split(':')
			epoch = int(_epoch)

		pkgver, _pkgrel = s.rsplit('-', maxsplit=1)
		return Version(epoch, pkgver, _pkgrel)


# HARDCODED LIST OF THINGS
REPLACEMENTS = {
	"crypto++": "cryptopp",
	"libsigc++": "libsigcpp",
	"libsigc++-docs": "libsigcpp-docs",
}


@dataclass(eq=True, frozen=True)
class Package:
	name: str
	version: Version
	arch: Optional[str]
	sha256: str
	size: int

	def __str__(self) -> str:
		astr = f"-{self.arch}" if self.arch else ""
		return f"{self.name}-{self.version}{astr}"

	def sanitised_name(self) -> str:
		return REPLACEMENTS.get(self.name) or self.name


	def manifest(self) -> dict[str, Any]:
		return {
			"schemaVersion": 2,
			"mediaType": mimes.MANIFEST,
			"config": {
				"mediaType": mimes.CONFIG,
				"digest": f"sha256:{self.sha256}",
				"size": self.size,
			},
			"layers": [{
				"mediaType": mimes.BYTES,
				"digest": f"sha256:{self.sha256}",
				"size": self.size,
			}]
		}


	@staticmethod
	def parse(name: str, size: int, sha256: str, arch: Optional[str] = None) -> "Package":
		epoch: int = 0

		if any(name.endswith(x) for x in [f".pkg.tar.{e}" for e in ["gz", "xz", "zst"]]):
			# $name-$pkgver-$pkgrel-$arch.pkg.tar.<ext>
			# we know that the extension has 3 parts (pkg, tar, zst); so splitext 3 times.
			name, arch = path.splitext(path.splitext(path.splitext(name)[0])[0])[0].rsplit('-', maxsplit=1)

		# name is now always $name-($epoch:)?$pkgver-$pkgrel
		# and version cannot contain hyphens. so, we can rsplit twice
		name, pkgver, pkgrel = path.basename(name).rsplit('-', maxsplit=2)

		if ':' in pkgver:
			_epoch, pkgver = pkgver.split(':')
			epoch = int(_epoch)

		# if we don't have an arch by now, complain!
		if arch is None:
			msg.error_and_exit(f"failed to parse package {name} without arch")

		if arch not in [ "any", "arm64", "x86_64" ]:
			msg.error_and_exit(f"Package '{name}' has invalid arch '{arch}'")

		return Package(name, Version(epoch, pkgver, pkgrel), arch, sha256, size)

	@staticmethod
	def from_tar_file(tar: tarfile.TarFile, info: tarfile.TarInfo):
		if info.isdir():
			desc = tar.extractfile(f"{info.name}/desc")
		else:
			desc = tar.extractfile(info)

		assert desc is not None
		lines = desc.read().splitlines()

		def _key(s: str) -> str:
			return lines[lines.index(f"%{s}%".encode()) + 1].decode()

		return Package(
			name    = _key("NAME"),
			version = Version.parse(_key("VERSION")),
			arch    = _key("ARCH"),
			sha256  = _key("SHA256SUM"),
			size    = int(_key("CSIZE"))
		)
