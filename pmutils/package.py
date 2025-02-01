#!/usr/bin/env python
# Copyright (c) 2023, yuki
# SPDX-License-Identifier: Apache-2.0

import tarfile
import os.path as path

from pmutils import msg, mimes
from pmutils.version import Version

from typing import *
from dataclasses import dataclass

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
		if (r := REPLACEMENTS.get(self.name)) is not None:
			return r
		elif "+" in self.name:
			msg.error_and_exit(f"Package '{self.name}' contains invalid character '+'")

		return self.name

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

		if arch not in ["any", "arm64", "x86_64"]:
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
		    name=_key("NAME"),
		    version=Version.parse(_key("VERSION")),
		    arch=_key("ARCH"),
		    sha256=_key("SHA256SUM"),
		    size=int(_key("CSIZE"))
		)
