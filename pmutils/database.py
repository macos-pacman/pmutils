#!/usr/bin/env python
# Copyright (c) 2023, zhiayang
# SPDX-License-Identifier: Apache-2.0

import io
import os
import time
import pyzstd
import tarfile
import hashlib
import subprocess

from os import path
from typing import *
from pmutils import msg
from pmutils.package import Package

from pmutils.msg import GREEN, PINK, GREY, WHITE, BOLD, UNCOLOUR, ALL_OFF

class Database:
	def __init__(self, db_path: str, packages: list[Package]):
		self._db_path = path.realpath(db_path)
		self._packages: list[Package] = packages
		self._names: dict[str, Package] = dict()
		for pkg in self._packages:
			self._names[pkg.name] = pkg

		self._additions: list[str] = []     # filenames
		self._add_names: dict[str, Package] = dict()
		self._new_files: list[tuple[Package, str]] = []

		self._removals: list[Package] = []

	def path(self) -> str:
		return self._db_path

	def packages(self) -> list[Package]:
		return self._packages

	def get(self, name: str) -> Package:
		if name not in self._names:
			raise NameError(f"unknown package '{name}'")
		return self._names[name]

	def contains(self, name: str) -> bool:
		return name in self._names

	def remove(self, package: Package):
		if package.name not in self._names:
			msg.warn(f"Ignoring removal of '{package.name}' that was not in the database")
			return

		self._removals.append(package)

	def add(self, file: str, verbose: bool = True, allow_downgrade: bool = False) -> bool:
		if not path.exists(file):
			msg.error(f"Ignoring addition of non-existent package file '{file}'")
			return False

		# remove an old package if we need to.
		new_pkg = Package.parse(file, size=os.stat(file).st_size,
			sha256=hashlib.file_digest(open(file, "rb"), "sha256").hexdigest())

		self._new_files.append((new_pkg, file))

		def should_add(new_pkg: Package, old_pkg: Package):
			if new_pkg.version > old_pkg.version:
				return True

			elif old_pkg.version == new_pkg.version:
				# the old package better contain a hash!
				assert old_pkg.sha256 is not None

				if new_pkg.sha256 == old_pkg.sha256:
					if verbose:
						msg.warn2(f"Ignoring {new_pkg} (identical copy in database)")
					return False
				else:
					# stop doing this m8
					msg.warn2(f"Package {new_pkg} has identical version but different hash in database")
					return True

			elif old_pkg.version > new_pkg.version:
				if verbose:
					msg.warn2(f"{'Ignoring ' if not allow_downgrade else 'Downgrading '}{new_pkg}" + \
						f" ({GREY}{new_pkg.version}{UNCOLOUR} " + \
						f"older than {GREEN}{old_pkg.version}{UNCOLOUR})")

				return allow_downgrade

		did_remove = False
		old_pkg: Optional[Package] = None
		if new_pkg.name in self._names:
			old_pkg = self._names[new_pkg.name]
			if not should_add(new_pkg, old_pkg):
				return False

			self._removals.append(old_pkg)
			did_remove = True

		# else, it was added before
		elif new_pkg.name in self._add_names:
			old_pkg = self._add_names[new_pkg.name]
			if not should_add(new_pkg, old_pkg):
				return False

		self._add_names[new_pkg.name] = new_pkg
		self._additions.append(file)

		if old_pkg is not None and did_remove:
			msg.p(f"{WHITE}*{ALL_OFF} {BOLD}{new_pkg.name}{ALL_OFF}" + \
				f" ({GREY}{old_pkg.version}{ALL_OFF} -> {GREEN}{new_pkg.version}{ALL_OFF})", end='')
		else:
			msg.p(msg.white("+ ") + f"{BOLD}{new_pkg.name}{ALL_OFF}: {msg.green(str(new_pkg.version))}", end='')

		# check if we need to generate a signature
		sig_file = f"{file}.sig"
		if not path.exists(sig_file):
			if verbose:
				print(f", {PINK}signing{ALL_OFF}", end='', flush=True)

			try:
				subprocess.check_call(["gpg", "--use-agent", "--output", sig_file, "--detach-sig", file])
			except:
				msg.error_and_exit("Failed to sign package!")

			if verbose:
				print(f", {GREEN}done{ALL_OFF}")
			else:
				print("")

		return True



	# write the database to disk by applying pending remove and add operations (in that order)
	# return a new database that has the updates.
	def save(self) -> list[tuple[Package, str]]:
		# if the lock file exists, spin until it no longer exists
		while True:
			if not os.path.exists(f"{self._db_path}.lck"):
				break
			msg.log("Pacman database is locked, waiting...")
			time.sleep(0.5)

		if (a := len(self._removals)) > 0:
			msg.log2(f"Removing {a} package{'' if a == 1 else 's'}")
			try:
				subprocess.check_call(["repo-remove", "--quiet", self._db_path, *[x.name for x in self._removals]])
			except:
				msg.error_and_exit("Failed to remove package!")

		if (b := len(self._additions)) > 0:
			msg.log2(f"Adding {b} package{'' if b == 1 else 's'}")
			try:
				subprocess.check_call(["repo-add", "--quiet", "--prevent-downgrade", "--sign",
					self._db_path, *self._additions])
			except:
				msg.error_and_exit("Failed to add package!")

		# need to save it before `reload_from_file()`, since that resets it
		new_files = self._new_files
		if len(self._removals) + len(self._additions) > 0:
			self.reload_from_file()
			msg.log("Updated database on disk")

		return new_files


	@staticmethod
	def load(db_path: str) -> "Database":
		if not path.exists(db_path):
			if not path.exists(path.dirname(db_path)):
				os.makedirs(path.dirname(db_path), exist_ok=True)

			msg.log(f"Creating new database {db_path}")

			# just in case there's actual errors
			# note: we add '.tar.zst' explictily here.
			try:
				out = subprocess.check_output(["repo-add", "--sign", "--quiet", f"{db_path}.tar.zst"],
					stderr=subprocess.PIPE).splitlines()
			except:
				msg.error_and_exit("Failed to create database!")

			for line in out:
				if line != b"==> WARNING: No packages remain, creating empty database.":
					print(line)

			return Database(db_path, [])

		ret = Database(db_path, []).reload_from_file()
		msg.log(f"Loaded {len(ret._packages)} package{'' if len(ret._packages) == 1 else 's'} from {db_path}")

		return ret


	def reload_from_file(self) -> "Database":
		db_tar: tarfile.TarFile
		if path.splitext(self._db_path)[1] == ".zst":
			with open(self._db_path, "rb") as f:
				db_tar = tarfile.open(fileobj=io.BytesIO(pyzstd.decompress(f.read())))
		else:
			db_tar = tarfile.open(self._db_path)

		self._packages = [ Package.from_tar_file(db_tar, x) for x in db_tar.getmembers() if x.isdir() ]
		for pkg in self._packages:
			self._names[pkg.name] = pkg

		self._add_names = dict()
		self._new_files = []
		self._additions = []
		self._removals = []
		return self

