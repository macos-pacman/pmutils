#!/usr/bin/env python
# Copyright (c) 2023, yuki
# SPDX-License-Identifier: Apache-2.0

import os
import subprocess

import requests as req

from pmutils import msg, mimes, util
from pmutils.oci import OciWrapper, OciManifest, OciObject
from pmutils.package import Package
from pmutils.database import Database

from typing import *
from dataclasses import dataclass

MANIFEST_OS = "darwin"
MAX_BLOB_SIZE = 500 * 1024 * 1024
MIN_PROGRESSBAR_SIZE = 10 * 1024 * 1024

# MAX_BLOB_SIZE = 2 * 1024 * 1024
# MIN_PROGRESSBAR_SIZE = 10 * 1024


def human_size(num: float, suffix: str = "B"):
	for unit in ["", "k", "M", "G", "T"]:
		if abs(num) < 1024.0:
			return f"{num:3.1f} {unit}{suffix}"
		num /= 1024.0
	return f"{num:.1f} P{suffix}"


@dataclass
class AdHocRepo:
	_name: str
	_remote: str

	def name(self) -> str:
		return self._name

	def remote(self) -> str:
		return self._remote


class Registry:
	def __init__(self, url: str, token: str):
		self._url = url
		self._token = token
		self._repos: dict[str, Repository] = dict()
		self._oauths: dict[str, str] = dict()

	def url(self) -> str:
		return self._url

	def get_default_repository(self) -> Optional[str]:
		if len(self._repos) == 1:
			return list(self._repos.keys())[0]
		return None

	def add_repository(self, name: str, remote: str, db_file: str, release_name: str, root_dir: Optional[str] = None):
		if name in self._repos:
			msg.error_and_exit(f"Duplicate repository '{name}'")

		if not db_file.endswith(".db"):
			msg.error_and_exit(f"db path should end in `.db`, and not have .tar.*")

		self._repos[name] = Repository(name, remote, release_name, Database.load(db_file), self, root_dir=root_dir)

	def get_repository(self, name: str) -> Optional["Repository"]:
		return self._repos.get(name)

	def oauth_token(self, repo: "Repository | AdHocRepo") -> str:
		if repo.name() in self._oauths:
			return self._oauths[repo.name()]

		r = req.get(
		    f"{self._url}/token", {
		        "scope": f"repository:{repo.remote()}:*",
		    }, auth=(repo.remote(), self._token)
		)

		if r.status_code != 200:
			msg.error_and_exit(f"Failed to authenticate!\n{r.text}")

		msg.log(f"Obtained OAuth token for {repo.remote()}")
		return r.json()["token"]

	def user_token(self) -> str:
		return self._token


class Repository:
	_name: str
	_remote: str
	_release_name: str
	_database: Database
	_registry: Registry

	_root_dir: Optional[str]
	_ociw: OciWrapper

	def __init__(
	    self,
	    name: str,
	    remote: str,
	    release_name: str,
	    database: Database,
	    registry: Registry,
	    root_dir: Optional[str] = None
	):
		self._name = name
		self._remote = remote
		self._release_name = release_name
		self._database = database
		self._registry = registry
		self._root_dir = root_dir

		self._ociw = OciWrapper(self._registry.url(), self._remote, self._registry.oauth_token(self))

	def name(self) -> str:
		return self._name

	def root_dir(self) -> Optional[str]:
		return self._root_dir

	def remote(self) -> str:
		return self._remote

	def database(self) -> Database:
		return self._database

	def oci(self) -> OciWrapper:
		return self._ociw

	def sync(self, upload: bool):
		updates = self._database.save()
		if len(updates) == 0 or (not upload):
			return

		msg.log(f"Uploading {len(updates)} package{'' if len(updates) == 1 else 's'}")

		for pkg, file in updates:
			self._upload_package(pkg, file)

		msg.log(f"Uploading database")
		self._upload_database(self._registry.user_token())

	def add_package(self, file: str, verbose: bool = True, allow_downgrade: bool = False) -> bool:
		# what we need to do here is read the package file first to get a list of digests
		# and then we can pass that when doing database.add()

		# it's too annoying to pass this to _upload_package since these are not necessarily
		# called sequentially or together, but we can at least show a progress bar for large
		# packages. the bottleneck when uploading SHOULD be the upload speed anyway.
		digests: list[str] = []

		def cb(_: Any, digest: str, size: int):
			digests.append(digest)

		util.read_file_chunks_with_progress_bar(
		    file, 10 * MAX_BLOB_SIZE, msg.slog3("Calculating package digests"), cb, MAX_BLOB_SIZE
		)
		return self._database.add(file, digests, verbose=verbose, allow_downgrade=allow_downgrade)

	def _upload_package(self, pkg: Package, pkg_file: str):
		msg.log2(f"{pkg.name}{msg.ALL_OFF} ({msg.GREEN}{pkg.version}{msg.ALL_OFF}): ")

		ns = self._ociw.make_namespace(for_package=pkg.sanitised_name())
		layers: list[OciObject] = []

		def cb(data: Any, digest: str, size: int):
			self._ociw.upload_blob(ns, digest, data)
			layers.append(OciObject(
			    sha256=digest,
			    mime=mimes.BYTES,
			    size=size,
			))

		util.read_file_chunks_with_progress_bar(
		    pkg_file, MIN_PROGRESSBAR_SIZE, msg.slog3("Uploading package file"), cb, MAX_BLOB_SIZE
		)

		manifest = OciManifest(
		    name=pkg.sanitised_name(),
		    version=pkg.version,
		    remote_url=self._remote,
		    layers=layers,
		    description=pkg.name,
		)

		assert pkg.arch is not None
		pkg_platform: Optional[str] = None

		if pkg.arch in ["x86_64", "arm64", "arm64e", "aarch64"]:
			if pkg.arch in ["arm64", "arm64e", "aarch64"]:
				pkg_platform = "arm64"
			elif pkg.arch == "x86_64":
				pkg_platform = "amd64"
			else:
				assert False
		elif pkg.arch != "any":
			msg.error(f"Package {pkg} has unsupported arch '{pkg.arch}'")

		self._ociw.upload(
		    manifest,
		    pkg_platform,
		    index_upload_callback=lambda: msg.log3(f"Index uploaded"),
		    manifest_upload_callback=lambda: msg.log3(f"Manifest uploaded"),
		    done_callback=lambda: msg.log3(f"Done"),
		)

	# assets are used for the database file itself
	def _upload_asset(self, token: str, release_id: int, name: str, file: str):
		req.post(
		    f"https://uploads.github.com/repos/{self._remote}/releases/{release_id}/assets",
		    params={ "name": name },
		    headers={
		        "X-GitHub-Api-Version": "2022-11-28",
		        "Authorization": f"Bearer {token}",
		        "Content-Type": "application/octet-stream"
		    },
		    data=open(file, "rb")
		)

	def _delete_asset(self, token: str, asset_id: int, name: str, file: str):
		_ = name
		_ = file
		req.delete(
		    f"https://api.github.com/repos/{self._remote}/releases/assets/{asset_id}",
		    headers={
		        "X-GitHub-Api-Version": "2022-11-28",
		        "Authorization": f"Bearer {token}",
		    }
		)

	def _upload_database(self, token: str):
		# we need to find the release id
		releases = req.get(f"https://api.github.com/repos/{self._remote}/releases").json()
		release_id: Optional[int] = None
		db_asset_id: Optional[int] = None
		sig_asset_id: Optional[int] = None

		for rel in releases:
			if rel["name"] == self._release_name:
				release_id = int(rel["id"])
				for asset in rel["assets"]:
					if asset["name"] == f"{self._name}.db":
						db_asset_id = int(asset["id"])
					elif asset["name"] == f"{self._name}.db.sig":
						sig_asset_id = int(asset["id"])

		if release_id is None:
			msg.error(f"Did not find release named '{self._release_name}'")
			return

		db_file = self._database.path()
		sig_file = f"{db_file}.sig"
		if not os.path.exists(sig_file):
			msg.log2("Signing database")
			try:
				subprocess.check_call(["gpg", "--use-agent", "--output", sig_file, "--detach-sig", db_file])
			except:
				msg.error_and_exit("Failed to sign database!")

		msg.log2(f"Uploading {self._name}.db: ", end='')
		if db_asset_id is not None:
			print(f"{msg.PINK}delete existing{msg.ALL_OFF}, ", end='', flush=True)
			self._delete_asset(token, db_asset_id, f"{self._name}.db", db_file)

		self._upload_asset(token, release_id, f"{self._name}.db", db_file)
		print(f"{msg.GREEN}done{msg.ALL_OFF}")

		msg.log2(f"Uploading {self._name}.db.sig: ", end='')
		if sig_asset_id is not None:
			print(f"{msg.PINK}delete existing{msg.ALL_OFF}, ", end='', flush=True)
			self._delete_asset(token, sig_asset_id, f"{self._name}.db.sig", sig_file)

		self._upload_asset(token, release_id, f"{self._name}.db.sig", sig_file)
		print(f"{msg.GREEN}done{msg.ALL_OFF}")
