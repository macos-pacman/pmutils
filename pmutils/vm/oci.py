#!/usr/bin/env python3
# Copyright (c) 2024, zhiayang
# SPDX-License-Identifier: Apache-2.0

import os
import sys
import json
import stat
import hashlib
import datetime
import stream_zip
import requests as req
import tqdm.auto as tqdm
import urllib.parse as urlparse

from typing import *
from pmutils import msg, config, mimes
from dataclasses import dataclass


def _make_bar(desc: str) -> Any:
	return tqdm.tqdm(
	    desc=desc,
	    unit=f"iB",
	    total=1,
	    unit_scale=True,
	    unit_divisor=1024,
	    dynamic_ncols=True,
	    miniters=1,
	    ascii=' =',
	    bar_format="{desc:>10}: [{bar}] ({n_fmt:>3}/{total_fmt:>3} [{percentage:>3.0f}%], {rate_fmt}{postfix})",
	)


def yield_file_bytes(path: str, bar: Any) -> Iterable[bytes]:
	CHUNK_SIZE = 128 * 1024
	with open(path, "rb") as f:
		if (bs := f.read(CHUNK_SIZE)):
			bar.update(len(bs))
			yield bs


def yield_files(prefix: str, path: str, bar: Any) -> Iterable[tuple[str, datetime.datetime, int, Any, Any]]:
	# first sum up the total file sizes
	total_bytes = sum(os.stat(f"{path}/{fn}").st_size for fn in os.listdir(path))
	bar.reset(total=total_bytes)

	now = datetime.datetime.now()
	for filename in os.listdir(path):
		st = os.stat(f"{path}/{filename}")
		yield (
		    f"{prefix}/{filename}",
		    now,
		    stat.S_IFREG | 0o644,
		    stream_zip.ZIP_AUTO(st.st_size),
		    yield_file_bytes(f"{path}/{filename}", bar),
		)


@dataclass
class AuthState:
	registry: str
	remote: str
	token: str


BUNDLE_REMOTE_NAME = "pmutils-sandbox-vm-bundle"


def upload_blob(auth: AuthState, blob: bytes | bytearray, digest: str):
	pkg_url = f"{auth.remote}/{BUNDLE_REMOTE_NAME}"

	if _head(f"/v2/{pkg_url}/blobs/sha256:{digest}", auth, failable=True).status_code != 200:
		r = _post(f"/v2/{pkg_url}/blobs/uploads/", auth)
		if not (200 <= r.status_code <= 299):
			msg.error(f"Failed to get blob-upload-endpoint: {r.text}")
			return

		upload_url = r.headers["location"]
		r = _put(upload_url, auth, data=blob, params={ "digest": f"sha256:{digest}"})


def upload_bundle(repo_name: str):
	cfg = config.config()
	if (sandbox_path := cfg.sandbox.path) is None:
		msg.error_and_exit(f"Sandbox path not configured! Set `sandbox.path` in your config.toml")

	# check if there is a `vm.bundle` in there
	bundle_path = os.path.join(sandbox_path, "vm.bundle")
	if not os.path.exists(bundle_path) or not os.path.isdir(bundle_path):
		msg.error_and_exit(f"VM bundle '{bundle_path}' either does not exist, or is not a directory")

	if not os.path.exists(f"{bundle_path}/config.json"):
		msg.error_and_exit(f"VM bundle '{bundle_path}' does not contain config.json")

	os_ver: str
	with open(f"{bundle_path}/config.json", "r") as j:
		jj = json.load(j)
		if "os_ver" not in jj:
			msg.error_and_exit(f"VM bundle's config.json does not contain required `os_ver` key")
		os_ver = str(jj["os_ver"])

	if (repo := cfg.registry.get_repository(repo_name)) is None:
		msg.error_and_exit(f"Repository '{repo_name}' does not exist")

	auth = AuthState(registry=cfg.registry.url(), remote=repo.remote, token=cfg.registry.oauth_token(repo))

	bar = _make_bar(f"{msg.blue('  ->')} {msg.bold('Compressing')}")

	# upload 500mb blobs
	MAX_BLOB_SIZE = 512 * 1024 * 1024
	ZIP_CHUNK_SIZE = 128 * 1024
	zipper = stream_zip.stream_zip(yield_files("vm.bundle", bundle_path, bar), chunk_size=ZIP_CHUNK_SIZE)

	cur_blob = bytearray()
	hasher = hashlib.sha256(usedforsecurity=False)
	for blob in zipper:
		hasher.update(blob)
		cur_blob.extend(blob)

		if len(cur_blob) >= MAX_BLOB_SIZE:
			upload_blob(auth, cur_blob, hasher.hexdigest())


def download_bundle():
	pass


def _request(
    method: str,
    auth: AuthState,
    url: str,
    *,
    content_type: Optional[str] = None,
    failable: bool = False,
    **kwargs: Any
) -> req.Response:
	resp = req.request(
	    method,
	    urlparse.urljoin(auth.registry, url),
	    headers={
	        "Content-Type": content_type or mimes.BYTES,
	        "Authorization": f"Bearer {auth.token}",
	        "Accept": ','.join([mimes.INDEX, mimes.CONFIG, mimes.MANIFEST])
	    },
	    **kwargs
	)

	if not failable and not (200 <= resp.status_code <= 299):
		print("", file=sys.stderr)
		msg.error(f"{method.upper()} response failed ({resp.status_code}):\n{resp.text}")

	return resp


def _get(url: str, auth: AuthState, *args: Any, **kwargs: Any) -> req.Response:
	return _request("get", auth, url, *args, **kwargs)


def _put(url: str, auth: AuthState, *args: Any, **kwargs: Any) -> req.Response:
	return _request("put", auth, url, *args, **kwargs)


def _head(url: str, auth: AuthState, *args: Any, **kwargs: Any) -> req.Response:
	return _request("head", auth, url, *args, **kwargs)


def _post(url: str, auth: AuthState, *args: Any, **kwargs: Any) -> req.Response:
	return _request("post", auth, url, *args, **kwargs)
