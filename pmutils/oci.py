#!/usr/bin/env python3
# Copyright (c) 2024, zhiayang
# SPDX-License-Identifier: Apache-2.0

import sys
import json
import hashlib
import requests as req
import urllib.parse as urlparse

from typing import *
from enum import Enum
from pmutils import msg, mimes


class Existence(Enum):
	NONE = 1
	EXISTS = 2
	CONFLICTS = 3


class OciWrapper:
	registry_url: str
	remote: str
	token: str

	def __init__(self):
		pass

	def upload(
	    self,
	    sanitised_name: str,
	    sanitised_version: str,
	    platform: Optional[str],
	    sha256: str,
	    blob: bytes | bytearray,
	    manifest: dict[str, Any],
	    other_manifests: list[dict[str, Any]],
	    *,
	    full_name: Optional[str] = None,
	    full_version: Optional[str] = None,
	    blob_upload_callback: Callable[[], None] = (lambda: None),
	    index_upload_callback: Callable[[], None] = (lambda: None),
	    manifest_upload_callback: Callable[[], None] = (lambda: None),
	    done_callback: Callable[[], None] = (lambda: None),
	):

		pkg_url = f"{self.remote}/{sanitised_name}"

		# if the blob already exists, don't upload it again (we might have changed other stuff)
		if self.http_head(f"/v2/{pkg_url}/blobs/sha256:{sha256}", failable=True).status_code != 200:
			r = self.http_post(f"/v2/{pkg_url}/blobs/uploads/")
			if not (200 <= r.status_code <= 299):
				msg.error(f"Failed to get blob-upload-endpoint: {r.text}")
				return

			upload_url = r.headers["location"]

			blob_upload_callback()
			r = self.http_put(upload_url, blob, params={ "digest": f"sha256:{sha256}"})

		manifest["annotations"] = {
		    "org.opencontainers.image.title": full_name or sanitised_name,
		    "org.opencontainers.image.version": full_version or sanitised_version,
		}

		# this makes github link the repo with the package
		if "ghcr.io" in self.registry_url:
			manifest["annotations"]["org.opencontainers.image.source"] = f"https://github.com/{self.remote}"

		manifest_str = json.dumps(manifest).encode("utf-8")
		manifest_digest = hashlib.sha256(manifest_str).hexdigest()

		index_upload_callback()
		manifest_desc: dict[str, Any] = {
		    "mediaType": mimes.MANIFEST, "digest": f"sha256:{manifest_digest}", "size": len(manifest_str)
		}

		if platform is not None:
			manifest_desc["platform"] = {
			    "os": "darwin",
			    "architecture": platform,
			}

		index = {
		    "schemaVersion": 2,
		    "mediaType": mimes.INDEX,
		    "manifests": other_manifests + [manifest_desc],
		    "annotations": {
		        "org.opencontainers.image.title": f"{full_name or sanitised_name}",
		        "org.opencontainers.image.version": f"{full_version or sanitised_version}",
		        "org.opencontainers.image.source": f"https://github.com/{self.remote}"
		    }
		}

		self.http_put(f"/v2/{pkg_url}/manifests/{sanitised_version}", data=json.dumps(index), content_type=mimes.INDEX)
		manifest_upload_callback()

		self.http_put(
		    f"/v2/{pkg_url}/manifests/sha256:{manifest_digest}", data=manifest_str, content_type=mimes.MANIFEST
		)

		done_callback()

	def check_existence(
	    self,
	    sanitised_name: str,
	    sanitised_version: str,
	    platform: Optional[str],
	    sha256: str,
	) -> tuple[Existence, list[dict[str, Any]]]:

		pkg_url = f"{self.remote}/{sanitised_name}"

		r = self.http_get(f"/v2/{pkg_url}/manifests/{sanitised_version}", failable=True)
		if r.status_code == 404:
			return (Existence.NONE, [])

		# ok, this version exists; means either it exists (ok) or it conflicts (sad but still ok)
		index = r.json()

		if index["schemaVersion"] != 2 or index["mediaType"] != mimes.INDEX:
			msg.error(f"Registry returned weird response:\n{index}")
			return (Existence.NONE, [])

		# these are the "non-conflicting" manifests
		other_manifests: list[dict[str, Any]] = []
		this_manifest_digest: Optional[str] = None

		for manifest in index["manifests"]:
			if manifest["mediaType"] != mimes.MANIFEST:
				continue

			if (platform is None) or ("platform" not in manifest) or (platform == manifest["platform"]):
				this_manifest_digest = manifest["digest"]
			else:
				other_manifests.append(manifest)

		if this_manifest_digest is not None:
			mm = self.http_get(f"/v2/{pkg_url}/manifests/{this_manifest_digest}", failable=True)
			if mm.status_code == 404:
				return (Existence.NONE, other_manifests)

			mmj = mm.json()
			if (mmj["schemaVersion"] != 2) or (mmj["mediaType"]
			                                   != mimes.MANIFEST) or (mmj["config"]["mediaType"] != mimes.CONFIG):
				msg.error(f"Registry returned weird response:\n{mmj}")
				return (Existence.CONFLICTS, other_manifests)

			if mmj["config"]["digest"].split(':')[1] == sha256:
				return (Existence.EXISTS, other_manifests)

			else:
				return (Existence.CONFLICTS, other_manifests)

		else:
			return (Existence.NONE, other_manifests)

	def http_request(
	    self,
	    method: str,
	    url: str,
	    *,
	    content_type: Optional[str] = None,
	    failable: bool = False,
	    **kwargs: Any
	) -> req.Response:

		resp = req.request(
		    method,
		    urlparse.urljoin(self.registry_url, url),
		    headers={
		        "Content-Type": content_type or mimes.BYTES,
		        "Authorization": f"Bearer {self.token}",
		        "Accept": ','.join([mimes.INDEX, mimes.CONFIG, mimes.MANIFEST])
		    },
		    **kwargs
		)

		if not failable and not (200 <= resp.status_code <= 299):
			print("", file=sys.stderr)
			msg.error(f"{method.upper()} response failed ({resp.status_code}):\n{resp.text}")

		return resp

	def http_get(self, url: str, *args: Any, **kwargs: Any) -> req.Response:
		return self.http_request("get", url, *args, **kwargs)

	def http_put(self, url: str, *args: Any, **kwargs: Any) -> req.Response:
		return self.http_request("put", url, *args, **kwargs)

	def http_head(self, url: str, *args: Any, **kwargs: Any) -> req.Response:
		return self.http_request("head", url, *args, **kwargs)

	def http_post(self, url: str, *args: Any, **kwargs: Any) -> req.Response:
		return self.http_request("post", url, *args, **kwargs)
