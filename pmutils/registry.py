#!/usr/bin/env python
# Copyright (c) 2023, zhiayang
# SPDX-License-Identifier: Apache-2.0

import os
import sys
import json
import hashlib
import subprocess
import requests as req
import urllib.parse as urlparse

from enum import Enum
from pmutils import msg, mimes
from pmutils.package import Package
from pmutils.database import Database

from typing import *
from dataclasses import dataclass

MANIFEST_OS = "darwin"

def human_size(num: float, suffix: str = "B"):
	for unit in ["", "k", "M", "G", "T"]:
		if abs(num) < 1024.0:
			return f"{num:3.1f} {unit}{suffix}"
		num /= 1024.0
	return f"{num:.1f} P{suffix}"

class Registry:
	def __init__(self, url: str, token: str):
		self._url = url
		self._token = token
		self._repos: dict[str, Repository] = dict()
		self._oauths: dict[str, str] = dict()

	def url(self) -> str:
		return self._url

	def add_repository(self, name: str, remote: str, db_file: str, release_name: str):
		if name in self._repos:
			msg.error_and_exit(f"duplicate repository '{name}'")

		if not db_file.endswith(".db"):
			msg.error_and_exit(f"db path should end in `.db`, and not have .tar.*")

		self._repos[name] = Repository(name, remote, release_name, Database.load(db_file), self)

	def get_repository(self, name: str) -> Optional["Repository"]:
		return self._repos.get(name)

	def oauth_token(self, repo: "Repository") -> str:
		if repo.name in self._oauths:
			return self._oauths[repo.name]

		r = req.get(f"{self._url}/token", {
			"scope": f"repository:{repo.remote}:*",
		}, auth=(repo.remote, self._token))

		if r.status_code != 200:
			msg.error_and_exit(f"failed to authenticate!\n{r.text}")

		msg.log2(f"Obtained OAuth token for {repo.remote}")
		return r.json()["token"]

	def user_token(self) -> str:
		return self._token


class Existence(Enum):
	NONE = 1
	EXISTS = 2
	CONFLICTS = 3

@dataclass(eq=True, frozen=True)
class Repository:
	name: str
	remote: str
	release_name: str
	database: Database
	registry: Registry

	def sync(self):
		updates = self.database.save()
		if len(updates) == 0:
			return

		msg.log(f"Uploading {len(updates)} package{'' if len(updates) == 1 else 's'}")
		token = self.registry.oauth_token(self)
		for pkg, file in updates:
			self._upload_package(token, pkg, file)

		msg.log(f"Uploading database")
		self._upload_database(self.registry.user_token())


	def _upload_asset(self, token: str, release_id: int, name: str, file: str):
		req.post(f"https://uploads.github.com/repos/{self.remote}/releases/{release_id}/assets", params={
			"name": name
		}, headers={
			"X-GitHub-Api-Version": "2022-11-28",
			"Authorization": f"Bearer {token}",
			"Content-Type": "application/octet-stream"
		}, data=open(file, "rb"))

	def _delete_asset(self, token: str, asset_id: int, name: str, file: str):
		req.delete(f"https://api.github.com/repos/{self.remote}/releases/assets/{asset_id}", headers={
			"X-GitHub-Api-Version": "2022-11-28",
			"Authorization": f"Bearer {token}",
		})


	def _upload_database(self, token: str):
		# we need to find the release id
		releases = req.get(f"https://api.github.com/repos/{self.remote}/releases").json()
		release_id: Optional[int] = None
		db_asset_id: Optional[int] = None
		sig_asset_id: Optional[int] = None

		for rel in releases:
			if rel["name"] == self.release_name:
				release_id = int(rel["id"])
				for asset in rel["assets"]:
					if asset["name"] == f"{self.name}.db":
						db_asset_id = int(asset["id"])
					elif asset["name"] == f"{self.name}.db.sig":
						sig_asset_id = int(asset["id"])

		if release_id is None:
			msg.error(f"Did not find release named '{self.release_name}'")
			return

		db_file = self.database.path()
		sig_file = f"{db_file}.sig"
		if not os.path.exists(sig_file):
			msg.log2("Signing database")
			subprocess.check_call(["gpg", "--use-agent", "--output", sig_file, "--detach-sig", db_file])

		if db_asset_id is not None:
			msg.log2(f"Deleting existing {self.name}.db")
			self._delete_asset(token, db_asset_id, f"{self.name}.db", db_file)

		if sig_asset_id is not None:
			msg.log2(f"Deleting existing {self.name}.db.sig")
			self._delete_asset(token, sig_asset_id, f"{self.name}.db.sig", sig_file)

		msg.log2(f"Uploading {self.name}.db")
		self._upload_asset(token, release_id, f"{self.name}.db", db_file)

		msg.log2(f"Uploading {self.name}.db.sig")
		self._upload_asset(token, release_id, f"{self.name}.db.sig", sig_file)







	def _upload_package(self, token: str, pkg: Package, pkg_file: str):
		exists, other_manifests = self._check_package_existence(pkg, token)
		if exists == Existence.EXISTS:
			msg.log2(f"{pkg.name} up to date, skipping")
			return

		elif exists == Existence.NONE:
			msg.log2(f"{pkg.name}{msg.ALL_OFF} ({msg.GREEN}{pkg.version}{msg.ALL_OFF}): ", end='')

		else:
			msg.log2(f"{pkg.name}{msg.ALL_OFF} ({msg.YELLOW}conflicting hash{msg.ALL_OFF}) " + \
				f"({msg.GREEN}{pkg.version}{msg.ALL_OFF}): ", end='')


		pkg_url = f"{self.remote}/{pkg.sanitised_name()}"

		# if the blob already exists, don't upload it again (we might have changed other stuff)
		if self._head(f"/v2/{pkg_url}/blobs/sha256:{pkg.sha256}", token, failable=True).status_code != 200:
			r = self._post(f"/v2/{pkg_url}/blobs/uploads/", token)
			if not (200 <= r.status_code <= 299):
				return

			upload_url = r.headers["location"]

			print(f"{msg.PINK}blob {msg.ALL_OFF}({msg.BOLD}{human_size(pkg.size)}{msg.ALL_OFF}), ", end='', flush=True)
			r = self._put(upload_url, token, data=open(pkg_file, "rb"), params={"digest": f"sha256:{pkg.sha256}"})


		m = pkg.manifest()
		m["annotations"] = {
			"org.opencontainers.image.title": pkg.name,
			"org.opencontainers.image.version": str(pkg.version)
		}

		# this makes github link the repo with the package
		if "ghcr.io" in self.registry.url():
			m["annotations"]["org.opencontainers.image.source"] = f"https://github.com/{self.remote}"

		manifest_str = json.dumps(m).encode("utf-8")
		manifest_digest = hashlib.sha256(manifest_str).hexdigest()

		print(f"{msg.PINK}index{msg.ALL_OFF}, ", end='', flush=True)
		manifest_desc: dict[str, Any] = {
			"mediaType": mimes.MANIFEST,
			"digest": f"sha256:{manifest_digest}",
			"size": len(manifest_str)
		}

		assert pkg.arch is not None
		if pkg.arch in [ "x86_64", "arm64", "arm64e", "aarch64" ]:
			_arch: str
			if pkg.arch in ["arm64", "arm64e", "aarch64"]:
				_arch = "arm64"
			elif pkg.arch == "x86_64":
				_arch = "amd64"
			else:
				assert False
			manifest_desc["platform"] = {
				"os": "darwin",
				"architecture": _arch,
			}
		elif pkg.arch != "any":
			msg.error(f"Package {pkg} has unsupported arch '{pkg.arch}'")


		index = {
			"schemaVersion": 2,
			"mediaType": mimes.INDEX,
			"manifests": other_manifests + [manifest_desc],
			"annotations": {
				"org.opencontainers.image.title": f"{pkg.name}",
				"org.opencontainers.image.version": f"{pkg.version}",
				"org.opencontainers.image.source": f"https://github.com/{self.remote}"
			}
		}

		# we must replace the colon in the epoch with a '-' because ':' is not valid in a tag
		self._put(f"/v2/{pkg_url}/manifests/{pkg.version.sanitise()}", token,
			data=json.dumps(index), content_type=mimes.INDEX)

		print(f"{msg.PINK}manifest{msg.ALL_OFF}, ", end='', flush=True)
		self._put(f"/v2/{pkg_url}/manifests/sha256:{manifest_digest}", token,
			data=manifest_str, content_type=mimes.MANIFEST)

		print(f"{msg.GREEN}done{msg.ALL_OFF}")



	def _get_package_platform(self, pkg: Package) -> Optional[dict[str, Any]]:
		assert pkg.arch is not None

		if pkg.arch == "any":
			return None
		elif pkg.arch in ["arm64", "arm64e", "aarch64"]:
			return { "os": MANIFEST_OS, "architecture": "arm64" }
		elif pkg.arch == "x86_64":
			return { "os": MANIFEST_OS, "architecture": "amd64" }
		else:
			msg.error(f"Package {pkg} has unsupported arch '{pkg.arch}'")
			return None


	def _check_package_existence(self, pkg: Package, token: str) -> tuple[Existence, list[dict[str, Any]]]:
		pkg_url = f"{self.remote}/{pkg.sanitised_name()}"

		r = self._get(f"/v2/{pkg_url}/manifests/{pkg.version.sanitise()}", token, failable=True)
		if r.status_code == 404:
			return Existence.NONE, []

		# ok, this version exists; means either it exists (ok) or it conflicts (sad but still ok)
		index = r.json()

		if index["schemaVersion"] != 2 or index["mediaType"] != mimes.INDEX:
			msg.error(f"Registry returned weird response:\n{index}")
			return Existence.NONE, []

		# these are the "non-conflicting" manifests
		other_manifests: list[dict[str, Any]] = []
		this_manifest_digest: Optional[str] = None

		for manifest in index["manifests"]:
			if manifest["mediaType"] != mimes.MANIFEST:
				continue

			pkg_platform = self._get_package_platform(pkg)
			if (pkg_platform is None) or ("platform" not in manifest) or (pkg_platform == manifest["platform"]):
				this_manifest_digest = manifest["digest"]
			else:
				other_manifests.append(manifest)


		if this_manifest_digest is not None:
			mm = self._get(f"/v2/{pkg_url}/manifests/{this_manifest_digest}", token, failable=True)
			if mm.status_code == 404:
				return Existence.NONE, other_manifests

			mmj = mm.json()
			if mmj["schemaVersion"] != 2 or mmj["mediaType"] != mimes.MANIFEST or mmj["config"]["mediaType"] != mimes.CONFIG:
				msg.error(f"Registry returned weird response:\n{mmj}")
				return Existence.CONFLICTS, other_manifests

			if mmj["config"]["digest"].split(':')[1] == pkg.sha256:
				return Existence.EXISTS, other_manifests

			else:
				return Existence.CONFLICTS, other_manifests

		else:
			return Existence.NONE, other_manifests




	def _request(self, method: str, url: str, token: str, *,
		         content_type: Optional[str] = None,
		         failable: bool = False, **kwargs: Any) -> req.Response:

		resp = req.request(method, urlparse.urljoin(self.registry.url(), url), headers={
			"Content-Type": content_type or mimes.BYTES,
			"Authorization": f"Bearer {token}",
			"Accept": ','.join([mimes.INDEX, mimes.CONFIG, mimes.MANIFEST])
		}, **kwargs)

		if not failable and not (200 <= resp.status_code <= 299):
			print("", file=sys.stderr)
			msg.error(f"{method.upper()} response failed ({resp.status_code}):\n{resp.text}")

		return resp


	def _get(self, url: str, token: str, *args: Any, **kwargs: Any) -> req.Response:
		return self._request("get", url, token, *args, **kwargs)

	def _put(self, url: str, token: str, *args: Any, **kwargs: Any) -> req.Response:
		return self._request("put", url, token, *args, **kwargs)

	def _head(self, url: str, token: str, *args: Any, **kwargs: Any) -> req.Response:
		return self._request("head", url, token, *args, **kwargs)

	def _post(self, url: str, token: str, *args: Any, **kwargs: Any) -> req.Response:
		return self._request("post", url, token, *args, **kwargs)
