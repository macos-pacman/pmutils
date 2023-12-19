#!/usr/bin/env python
# Copyright (c) 2023, zhiayang
# SPDX-License-Identifier: Apache-2.0

import gzip
import tarfile
import asyncio
import aiohttp
import datetime

from typing import *
from pmutils import msg
from dataclasses import dataclass

from pmutils.registry import Repository
from pmutils.package import Version, Package
from pmutils.config import CheckerConfig

from io import BytesIO
from urllib.request import urlopen

CONCURRENT_CONNECTIONS = 3

@dataclass
class UpstreamDatabase:
	packages: dict[str, Package]

	@staticmethod
	def from_url(url: str) -> "UpstreamDatabase":
		with tarfile.open(name=None, fileobj=BytesIO(urlopen(url).read())) as tar:
			pkgs = [ Package.from_tar_file(tar, x) for x in tar.getmembers() if x.isdir() ]
			return UpstreamDatabase({ pkg.name: pkg for pkg in pkgs })

def _print_progress(idx: int, total: int):
	txt_len = len(f" {1 + idx}/{total}")
	print(f" {msg.PINK}{1 + idx}{msg.ALL_OFF}/{msg.PINK}{total}{msg.ALL_OFF}", end='', flush=True)
	print(f"\x1b[{txt_len}D", end='', flush=True)


def check_packages(base_url: str, repo: Repository, check_config: CheckerConfig):
	pkg_names = set(x.name for x in repo.database.packages())

	upstream_versions: dict[str, Version] = {}
	upstream_flagged_ood: dict[str, datetime.date] = {}

	msg.log(f"Retrieving packages from upstream")

	# get 'core' and 'extra' from upstream
	# the assumption here is that core, extra, and AUR are disjoint sets -- which should be the case.
	for ur in ["core", "extra"]:
		msg.log2(f"{ur}{msg.ALL_OFF}: ", end='')
		r = UpstreamDatabase.from_url(f"{base_url}/{ur}/os/x86_64/{ur}.db.tar.gz")

		l = len(r.packages)
		print(f"{msg.GREEN}{l}{msg.ALL_OFF} {msg.BOLD}package{'' if l == 1 else 's'}{msg.ALL_OFF}", end='', flush=True)

		tmp = { x.name: x.version for x in r.packages.values() if x.name in pkg_names }

		if check_config.check_out_of_date:
			# check if the package is flagged is out of date
			print(f", checking:", end='', flush=True)

			dd = [0]
			async def check_one(session: aiohttp.ClientSession, name: str, done: list[int]):
				while True:
					s = await session.get(f"https://archlinux.org/packages/{ur}/{r.packages[name].arch}/{name}/json/")
					if s.status != 200:
						print(f"status: {s.status}:\n{await s.text()}")
						await asyncio.sleep(2.5)
						continue

					j = await s.json()
					break

				_print_progress(done[0], len(tmp))
				done[0] += 1

				if (flag_date := j["flag_date"]) is not None:
					dt = datetime.datetime.fromisoformat(flag_date)
					upstream_flagged_ood[name] = datetime.date(dt.year, dt.month, dt.day)

			async def top():
				session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=CONCURRENT_CONNECTIONS))
				await asyncio.gather(*[check_one(session, name, dd) for name in tmp.keys()])
				await session.close()

			asyncio.run(top())

		print("")

		# only add to `upstream_versions` if we have the same package
		upstream_versions.update(tmp)

	msg.log2(f"aur: ", end='')
	aur_names = set(gzip.open(urlopen(f"https://aur.archlinux.org/packages.gz")).readlines())

	# definitely > 1 package on AUR, don't need that plural check
	print(f"{msg.GREEN}{len(aur_names)}{msg.ALL_OFF} {msg.BOLD}packages{msg.ALL_OFF}", end='', flush=True)
	aur_to_check = pkg_names.intersection(aur_names)


	if len(aur_to_check) > 0:
		print(f", checking:", end='', flush=True)

		dd = [0]
		async def check_one_aur(session: aiohttp.ClientSession, name: str, done: list[int]):
			while True:
				s = await session.get(f"https://aur.archlinux.org/rpc/v5/info/{name}")
				if s.status != 200:
					print(f"status: {s.status}:\n{await s.text()}")
					await asyncio.sleep(2.5)
					continue

				info = await s.json()
				break

			if info["Name"] != name:
				return

			_print_progress(done[0], len(aur_to_check))
			done[0] += 1

			upstream_versions[name] = Version.parse(info["Version"])
			if (check_config.check_out_of_date) and (ood := info["OutOfDate"]) is not None:
				upstream_flagged_ood[name] = datetime.date.fromtimestamp(int(ood))

		async def top():
			session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=CONCURRENT_CONNECTIONS))
			await asyncio.gather(*[check_one_aur(session, name, dd) for name in tmp.keys()])
			await session.close()

		asyncio.run(top())

	print("")

	def filt(pkg: str) -> bool:
		if pkg in check_config.ignore_packages:
			return False

		v1 = repo.database.get(pkg).version
		v2 = upstream_versions[pkg]

		# ignore pkgrel changes for 'haskell-*' packages
		if check_config.ignore_haskell_pkgrel and pkg.startswith("haskell-"):
			v1 = Version(v1.epoch, v1.pkgver, 1)
			v2 = Version(v2.epoch, v2.pkgver, 1)

		if pkg in check_config.ignore_epochs:
			v1 = Version(0, v1.pkgver, v1.pkgrel)
			v2 = Version(0, v2.pkgver, v2.pkgrel)

		return v1 < v2

	updated_packages = list(sorted(filter(filt, upstream_versions.keys())))

	if (num_upd := len(updated_packages)) > 0:
		msg.log(f"{num_upd} package{' was' if num_upd == 1 else 's were'} updated upstream:")
		for u in updated_packages:
			msg.log3(f"{u}{msg.ALL_OFF}: {msg.GREY}{repo.database.get(u).version}{msg.ALL_OFF} " +
					 f"-> {msg.GREEN}{upstream_versions[u]}{msg.ALL_OFF}")

	if (num_ood := len(upstream_flagged_ood)) > 0:
		msg.log(f"{num_ood} package{' was' if num_ood == 1 else 's were'} flagged out-of-date:")
		for name, date in upstream_flagged_ood.items():
			msg.log3(f"{name}{msg.ALL_OFF}: {msg.GREY}since{msg.ALL_OFF} {msg.YELLOW}{date.strftime('%Y-%m-%d')}{msg.ALL_OFF}")
