#!/usr/bin/env python3
# Copyright (c) 2024, yuki
# SPDX-License-Identifier: Apache-2.0

import sys
import json
import hashlib
import requests as req
import urllib.parse as urlparse

from typing import *
from enum import Enum
from dataclasses import dataclass

from pmutils import msg, mimes
from pmutils.version import Version, IVersion

# god damn requests sucks
from http.client import HTTPConnection

LARGE_BLOCK_SIZE = 256 * 1024

assert HTTPConnection.__init__.__defaults__ is not None
HTTPConnection.__init__.__defaults__ = tuple(
    x if x != 8192 else LARGE_BLOCK_SIZE for x in HTTPConnection.__init__.__defaults__
)

IMG_TITLE = "org.opencontainers.image.title"
IMG_VERSION = "org.opencontainers.image.version"
IMG_REMOTE_URL = "org.opencontainers.image.source"
IMG_DESCRIPTION = "org.opencontainers.image.description"


class Existence(Enum):
	NONE = 1
	EXISTS = 2
	CONFLICTS = 3


class UploadResult(Enum):
	UPLOADED = 1
	EXISTS = 2
	FAILED = 3


@dataclass
class OciObject:
	sha256: str
	mime: str
	size: int

	def json(self) -> str:
		return json.dumps(self.obj())

	def obj(self) -> dict[str, Any]:
		return {
		    "digest": f"sha256:{self.sha256}",
		    "mediaType": self.mime,
		    "size": self.size,
		}

	@classmethod
	def from_json(cls, json: dict[str, Any]) -> "OciObject":
		try:
			return OciObject(
			    sha256=json["digest"].replace("sha256:", ""),
			    mime=json["mediaType"],
			    size=json["size"],
			)
		except KeyError as e:
			msg.error_and_exit(f"Malformed OciObject json: {json}: {e}")


@dataclass
class OciManifest:
	name: str
	version: IVersion
	remote_url: str
	layers: list[OciObject]
	description: Optional[str] = None
	config: Optional[OciObject] = None

	def __post_init__(self):
		if '+' in self.name:
			msg.error_and_exit(f"Cannot create OCI manifest with name '{self.name}' containing '+'")

	def obj(self) -> dict[str, Any]:
		# the config can just be the first layer.
		cfg = self.config or self.layers[0]

		ret: dict[str, Any] = {
		    "schemaVersion": 2,
		    "mediaType": mimes.MANIFEST,
		    "annotations": {
		        IMG_TITLE: self.name,
		        IMG_VERSION: str(self.version),
		        IMG_REMOTE_URL: f"https://github.com/{self.remote_url}",
		        IMG_DESCRIPTION: (self.description or f"{self.name} {self.version}"),
		    },
		    "config": {
		        "digest": f"sha256:{cfg.sha256}",
		        "mediaType": mimes.CONFIG,
		        "size": cfg.size,
		    },
		    "layers": [layer.obj() for layer in self.layers],
		}

		return ret

	def json(self) -> str:
		return json.dumps(self.obj())

	@classmethod
	def from_json(cls, json: dict[str, Any]) -> "OciManifest":
		try:
			config = OciObject.from_json(json["config"])
			layers = [OciObject.from_json(l) for l in json["layers"]]

			annot = json["annotations"]
			return OciManifest(
			    name=annot[IMG_TITLE],
			    version=annot[IMG_VERSION],
			    remote_url=annot[IMG_REMOTE_URL].replace("https://github.com/", ""),
			    description=annot.get(IMG_DESCRIPTION) or "",
			    layers=layers,
			    config=config
			)

		except KeyError as e:
			msg.error_and_exit(f"Malformed OciManifest json: {json}: {e}")


@dataclass
class OciIndex:
	@dataclass
	class ManifestShim:
		size: int
		digest: str
		platform_os: Optional[str]
		platform_arch: Optional[str]

	name: str
	version: IVersion
	remote_url: str
	manifests: list[ManifestShim]

	def __post_init__(self):
		if '+' in self.name:
			msg.error_and_exit(f"Cannot create OCI index with name '{self.name}' containing '+'")

	def obj(self) -> dict[str, Any]:
		def make_platform_dict(m: OciIndex.ManifestShim) -> dict[str, Any]:
			if not (m.platform_os or m.platform_arch):
				return {}
			return {
			    "platform": {
			        "os": m.platform_os,
			        "architecture": m.platform_arch,
			    }
			}

		mm = [{
		    "mediaType": mimes.MANIFEST,
		    "digest": m.digest,
		    **make_platform_dict(m),
		} for m in self.manifests]

		return {
		    "schemaVersion": 2,
		    "mediaType": mimes.INDEX,
		    "annotations": {
		        IMG_TITLE: self.name,
		        IMG_VERSION: str(self.version),
		        IMG_REMOTE_URL: f"https://github.com/{self.remote_url}",
		    },
		    "manifests": mm,
		}

	def json(self) -> str:
		return json.dumps(self.obj())

	@classmethod
	def from_json(cls, json: dict[str, Any]) -> "OciIndex":
		try:
			manifests: list[OciIndex.ManifestShim] = []
			seen_digests: set[str] = set()
			for m in json["manifests"]:
				if m["digest"] in seen_digests:
					continue

				seen_digests.add(m["digest"])
				manifests.append(
				    OciIndex.ManifestShim(
				        size=m["size"],
				        digest=m["digest"],
				        platform_os=m.get("platform", {}).get("os"),
				        platform_arch=m.get("platform", {}).get("architecture")
				    )
				)

			return OciIndex(
			    name=json["annotations"][IMG_TITLE],
			    version=Version.parse(json["annotations"][IMG_VERSION]),
			    remote_url=json["annotations"][IMG_REMOTE_URL].replace("https://github.com/", ""),
			    manifests=manifests
			)

		except KeyError as e:
			msg.error_and_exit(f"Malformed OciIndex json: {json}: {e}")


@dataclass
class OciWrapper:
	registry_url: str
	remote: str
	token: str

	def upload_blob(
	    self,
	    namespace: str,
	    sha256: str,
	    blob: bytes | bytearray | Any,
	    *,
	    callback: Callable[[], None] = (lambda: None),
	) -> bool:
		# if the blob already exists, don't upload it again (we might have changed other stuff)
		if self.http_head(f"/v2/{namespace}/blobs/sha256:{sha256}", failable=True).status_code == 200:
			return False

		r = self.http_post(f"/v2/{namespace}/blobs/uploads/")
		if not (200 <= r.status_code <= 299):
			msg.error(f"Failed to get blob-upload-endpoint for namespace '{namespace}': {r.text}")
			return False

		upload_url = r.headers["location"]

		callback()
		r = self.http_put(upload_url, data=blob, params={ "digest": f"sha256:{sha256}"})
		if not (200 <= r.status_code <= 299):
			msg.error(f"Failed to upload blob: {r.text}")

		return True

	def upload_manifest(
	    self,
	    namespace: str,
	    manifest: OciManifest,
	    *,
	    callback: Callable[[], None] = (lambda: None),
	) -> tuple[str, int]:
		manifest_str = manifest.json().encode("utf-8")
		manifest_digest = hashlib.sha256(manifest_str).hexdigest()

		callback()
		self.http_put(
		    f"/v2/{namespace}/manifests/sha256:{manifest_digest}",
		    data=manifest_str,
		    content_type=mimes.MANIFEST,
		)

		return (manifest_digest, len(manifest_str))

	def upload(
	    self,
	    manifest: OciManifest,
	    platform: Optional[str],
	    *,
	    index_upload_callback: Callable[[], None] = (lambda: None),
	    manifest_upload_callback: Callable[[], None] = (lambda: None),
	    done_callback: Callable[[], None] = (lambda: None),
	) -> UploadResult:
		namespace = self.make_namespace(for_package=manifest.name)
		exists, other_manifests = self.check_existence(namespace, manifest, platform)

		if exists == Existence.EXISTS:
			return UploadResult.EXISTS

		(manifest_digest, manifest_len) = self.upload_manifest(namespace, manifest, callback=manifest_upload_callback)

		manifest_desc: dict[str, Any] = {
		    "mediaType": mimes.MANIFEST,
		    "digest": f"sha256:{manifest_digest}",
		    "size": manifest_len,
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
		        IMG_TITLE: f"{manifest.name}",
		        IMG_VERSION: str(manifest.version),
		        IMG_REMOTE_URL: f"https://github.com/{self.remote}",
		        IMG_DESCRIPTION: (manifest.description or f"{manifest.name} {manifest.version}"),
		    }
		}

		index_upload_callback()
		self.http_put(
		    f"/v2/{namespace}/manifests/{manifest.version.sanitise()}",
		    data=json.dumps(index),
		    content_type=mimes.INDEX,
		)

		done_callback()
		return UploadResult.UPLOADED

	def make_namespace(self, *, for_package: str) -> str:
		return f"{self.remote}/{for_package}"

	def get_tags(self, namespace: str) -> list[str]:
		r = self.http_get(f"/v2/{namespace}/tags/list", failable=True)
		if r.status_code == 404:
			return []

		return r.json()["tags"]

	def get_index(self, namespace: str, tag: str) -> Optional[OciIndex]:
		r = self.http_get(f"/v2/{namespace}/manifests/{tag}", failable=True)
		if r.status_code == 404:
			return None

		return OciIndex.from_json(r.json())

	def get_manifest(self, ns: str, digest: str) -> Optional[OciManifest]:
		r = self.http_get(f"/v2/{ns}/manifests/{digest}", failable=True)
		if r.status_code == 404:
			return None

		return OciManifest.from_json(r.json())

	def check_existence(
	    self,
	    namespace: str,
	    manifest: OciManifest,
	    platform: Optional[str],
	) -> tuple[Existence, list[dict[str, Any]]]:

		r = self.http_get(f"/v2/{namespace}/manifests/{manifest.version.sanitise()}", failable=True)
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

		for mmm in index["manifests"]:
			if mmm["mediaType"] != mimes.MANIFEST:
				continue

			if (platform is None) or ("platform" not in mmm) or (platform == mmm["platform"]):
				this_manifest_digest = mmm["digest"]
			else:
				other_manifests.append(mmm)

		if this_manifest_digest is not None:
			mm = self.http_get(f"/v2/{namespace}/manifests/{this_manifest_digest}", failable=True)
			if mm.status_code == 404:
				return (Existence.NONE, other_manifests)

			mmj = mm.json()
			if (mmj["schemaVersion"] != 2) or (mmj["mediaType"]
			                                   != mimes.MANIFEST) or (mmj["config"]["mediaType"] != mimes.CONFIG):
				msg.error(f"Registry returned weird response:\n{mmj}")
				return (Existence.CONFLICTS, other_manifests)

			cur_manifest_config_digest = manifest.config.sha256 if manifest.config else manifest.layers[0].sha256
			if mmj["config"]["digest"].split(':')[1] == cur_manifest_config_digest:
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
	    accept: list[str] = [],
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
