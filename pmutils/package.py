#!/usr/bin/env python
# Copyright (c) 2023, zhiayang
# SPDX-License-Identifier: Apache-2.0

import subprocess
import os.path as path

from typing import *
from dataclasses import dataclass

@dataclass(eq=True, frozen=True)
class Version:
	epoch: int
	pkgver: str
	pkgrel: int

	def __str__(self) -> str:
		estr = f"{self.epoch}:" if self.epoch > 0 else ""
		return f"{estr}{self.pkgver}-{self.pkgrel}"

	# call out to pacman because it's the final authority
	def __lt__(self, other: "Version") -> bool:
		return int(subprocess.check_output(["vercmp", str(self), str(other)])) < 0

	def __gt__(self, other: "Version") -> bool:
		return int(subprocess.check_output(["vercmp", str(self), str(other)])) > 0

	def __le__(self, other: "Version") -> bool:
		return self == other or self < other

	def __ge__(self, other: "Version") -> bool:
		return self == other or self > other




@dataclass(eq=True, frozen=True)
class Package:
	name: str
	version: Version
	arch: Optional[str]
	sha256: Optional[str]

	def __str__(self) -> str:
		astr = f"-{self.arch}" if self.arch else ""
		return f"{self.name}-{self.version}{astr}"

	@staticmethod
	def parse(name: str, sha256: Optional[str] = None) -> "Package":
		arch: Optional[str] = None
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

		return Package(name, Version(epoch, pkgver, int(pkgrel)), arch, sha256)

