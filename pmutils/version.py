#!/usr/bin/env python3
# Copyright (c) 2024, zhiayang
# SPDX-License-Identifier: Apache-2.0

import pyalpm
import subprocess

from typing import *
from dataclasses import dataclass


class IVersion(Protocol):
	def __str__(self) -> str:
		...

	def sanitise(self) -> str:
		...

	def __lt__(self, other: Self) -> bool:
		...

	def __gt__(self, other: Self) -> bool:
		...

	def __le__(self, other: Self) -> bool:
		...

	def __ge__(self, other: Self) -> bool:
		...


@dataclass(frozen=True)
class Version(IVersion):
	epoch: int
	pkgver: str
	pkgrel: str

	def __str__(self) -> str:
		estr = f"{self.epoch}:" if self.epoch > 0 else ""
		return f"{estr}{self.pkgver}-{self.pkgrel}"

	def sanitise(self) -> str:
		return str(self).replace(':', '-').replace('+', '_')

	def __lt__(self, other: Self) -> bool:
		return pyalpm.vercmp(str(self), str(other)) < 0
		# return int(subprocess.check_output(["vercmp", str(self), str(other)])) < 0

	def __gt__(self, other: Self) -> bool:
		return pyalpm.vercmp(str(self), str(other)) > 0
		# return int(subprocess.check_output(["vercmp", str(self), str(other)])) > 0

	def __eq__(self, other: object) -> bool:
		return isinstance(other, Self) and pyalpm.vercmp(str(self), str(other)) == 0

	def __ne__(self, other: object) -> bool:
		return not (self == other)

	def __le__(self, other: Self) -> bool:
		return self == other or self < other

	def __ge__(self, other: Self) -> bool:
		return self == other or self > other

	@staticmethod
	def parse(s: str) -> "Version":
		epoch: int = 0

		if ':' in s:
			_epoch, s = s.split(':')
			epoch = int(_epoch)

		pkgver, _pkgrel = s.rsplit('-', maxsplit=1)
		return Version(epoch, pkgver, _pkgrel)


@dataclass(eq=True, frozen=True)
class NonPacmanVersion(IVersion):
	version_string: str

	def __lt__(self, other: Self) -> bool:
		return int(subprocess.check_output(["vercmp", str(self), str(other)])) < 0

	def __gt__(self, other: Self) -> bool:
		return int(subprocess.check_output(["vercmp", str(self), str(other)])) > 0

	def __le__(self, other: Self) -> bool:
		return self == other or self < other

	def __ge__(self, other: Self) -> bool:
		return self == other or self > other

	def __str__(self) -> str:
		return self.version_string

	def sanitise(self) -> str:
		return str(self).replace(':', '-').replace('+', '_')
