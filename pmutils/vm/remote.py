#!/usr/bin/env python3
# Copyright (c) 2024, zhiayang
# SPDX-License-Identifier: Apache-2.0

import os
import json
import tarfile
import hashlib
import zstandard as zstd
import tqdm.auto as tqdm
import tqdm.utils as tqdm_utils

from typing import *
from io import BytesIO
from queue import Queue
from threading import Thread, Lock

from pmutils import msg, config, oci, registry, mimes
from pmutils.version import NonPacmanVersion

UploadQueue = Queue[Optional[tuple[str, BytesIO, int, int]]]

BUNDLE_MANIFEST_NAME = "macos-sandbox-vm"
BLOB_SIZE = 512 * 1024 * 1024
MAX_PENDING_UPLOADS = 20
NUM_UPLOAD_THREADS = 2

TARFILE_CHUNK_SIZE = 8 * 1024 * 1024
ZSTD_COMPRESSION_LEVEL = 18
ZSTD_NUM_THREADS = 4


class AtomicInt:
	value: int
	lock: Lock

	def __init__(self, value: int = 0):
		self.value = value
		self.lock = Lock()

	def fetch_add(self) -> int:
		with self.lock as _:
			ret = self.value
			self.value += 1
			return ret


def _make_bar(desc: str, *, loglevel: int, data: Optional[BytesIO] = None, **kwargs: Any) -> tuple[Any, Any]:
	if loglevel not in [2, 3]:
		msg.error_and_exit(f"loglevel should be 2 or 3")

	if loglevel == 2:
		desc = msg.slog2(desc)
	else:
		desc = msg.slog3(desc)

	bar = tqdm.tqdm(
	    iterable=data,
	    desc=desc,
	    unit=f"B",
	    unit_scale=True,
	    unit_divisor=1024,
	    dynamic_ncols=True,
	    miniters=1,
	    maxinterval=0.3,
	    ascii=" ▬",
	    bar_format=f"{{desc:<18}}: {msg.blue('[')}{{bar}}{msg.blue(']')} ({{n_fmt:<5}}/{{total_fmt:>5}}"
	    + " [{percentage:>3.0f}%], {rate_fmt:>8}{postfix}) ",
	    **kwargs,
	)

	if data is not None:
		return (bar, tqdm_utils.CallbackIOWrapper(bar.update, data))
	else:
		return (bar, None)


class ZstdCompressionStreamer(IO[bytes]):
	_z: zstd.ZstdCompressor
	_c: Any

	blobs: list[tuple[str, int]]

	_bar: Any
	_bar_fmt: str
	_blob_idx: int
	_queue: UploadQueue

	def __init__(self, output_queue: UploadQueue, bar: Any):
		self._z = zstd.ZstdCompressor(level=ZSTD_COMPRESSION_LEVEL, threads=ZSTD_NUM_THREADS)
		self._c = self._z.chunker(chunk_size=BLOB_SIZE)

		self.blobs = []

		self._bar_fmt = "Compressing (blob #{}, q: {})"
		self._queue = output_queue
		self._blob_idx = 0

		self._bar = bar
		self._bar.set_description_str(msg.slog2(self._bar_fmt.format(self._blob_idx + 1, self._queue.qsize())))

	def upload_blob(self, blob: bytes):
		digest = hashlib.sha256(blob, usedforsecurity=False).hexdigest()
		self._bar.set_description_str(msg.slog2(self._bar_fmt.format(self._blob_idx + 1, self._queue.qsize())))
		self.blobs.append((digest, len(blob)))

		self._queue.put((digest, BytesIO(blob), len(blob), self._blob_idx))
		self._blob_idx += 1

	def write(self, s: Any) -> int:
		for _blob in self._c.compress(s):
			self.upload_blob(cast(bytes, _blob))

		return len(s)

	def close(self):
		for _blob in self._c.finish():
			self.upload_blob(cast(bytes, _blob))


def get_tarinfos(prefix: str, path: str, bar: Any) -> list[tuple[tarfile.TarInfo, BinaryIO]]:
	total_size = 0
	ret: list[tuple[tarfile.TarInfo, BinaryIO]] = []
	for filename in sorted(os.listdir(path)):
		st = os.stat(f"{path}/{filename}")

		tf = tarfile.TarInfo(f"{prefix}/{filename}")
		tf.mtime = int(st.st_mtime)
		tf.size = st.st_size
		tf.uid = 0
		tf.gid = 0

		ret.append((tf, open(f"{path}/{filename}", "rb")))
		total_size += st.st_size

	bar.reset(total=total_size)
	return ret


def upload_func(ociw: oci.OciWrapper, queue: UploadQueue, bar_offset: AtomicInt):
	try:
		while (work := queue.get()) is not None:
			retries = 0
			while True:
				try:
					digest, blob, size, _blob_idx = work
					(bar, blob_io) = _make_bar(
					    f"Uploading blob #{_blob_idx+1}",
					    loglevel=3,
					    data=blob,
					    total=size,
					    position=1 + (bar_offset.fetch_add() % 2),
					    leave=None,
					    delay=1,
					)

					if ociw.upload_blob(ociw.make_namespace(for_package=BUNDLE_MANIFEST_NAME), digest, blob_io):
						bar.refresh()

					bar.close()
					break

				except:
					if retries < 2:
						retries += 1
						continue
	except:
		pass

	# since we got a None, signal the other threads to quit too.
	queue.put(None)


def upload_bundle():
	cfg = config.config()
	if (sandbox_path := cfg.sandbox.path) is None:
		msg.error_and_exit(f"Sandbox path not configured! Set `sandbox.path` in your config.toml")

	if (remote := cfg.sandbox.remote) is None:
		msg.error_and_exit(f"Sandbox remote not configured! Set `sandbox.remote` in your config.toml")

	# check if there is a `vm.bundle` in there
	bundle_path = os.path.join(sandbox_path, "vm.bundle")
	if not os.path.exists(bundle_path) or not os.path.isdir(bundle_path):
		msg.error_and_exit(f"VM bundle '{bundle_path}' either does not exist, or is not a directory")

	if not os.path.exists(f"{bundle_path}/config.json"):
		msg.error_and_exit(f"VM bundle '{bundle_path}' does not contain config.json")

	os_ver: str
	vm_arch: str
	with open(f"{bundle_path}/config.json", "r") as j:
		jj = json.load(j)
		if ("os_ver" not in jj) or ("arch" not in jj):
			msg.error_and_exit(f"VM bundle's config.json does not contain required `os_ver` or `arch` keys")
		os_ver = str(jj["os_ver"])
		vm_arch = str(jj["arch"])

	repo = registry.AdHocRepo(name="$pmutils-sandbox", remote=remote)

	oauth_token = cfg.registry.oauth_token(repo)
	msg.log(f"Compressing {bundle_path} ({os_ver}, {vm_arch}) into blobs")

	# start with no layers
	ociw = oci.OciWrapper(cfg.registry.url(), repo.remote, oauth_token)

	upload_queue: UploadQueue = Queue(maxsize=MAX_PENDING_UPLOADS)
	bar_offset = AtomicInt()

	workers: list[Thread] = [
	    Thread(
	        target=upload_func,
	        args=[ociw, upload_queue, bar_offset],
	    ) for _ in range(NUM_UPLOAD_THREADS)
	]

	for w in workers:
		w.start()

	bar, _ = _make_bar("", loglevel=2, position=0)
	zcmp = ZstdCompressionStreamer(upload_queue, bar)

	interrupted = False
	try:
		with tarfile.open(name=None, mode="w|", bufsize=TARFILE_CHUNK_SIZE, fileobj=zcmp) as tar:
			for tf, file in get_tarinfos("vm.bundle", bundle_path, bar):
				with file as f:
					tar.addfile(tf, cast(IO[bytes], tqdm_utils.CallbackIOWrapper(bar.update, f, method="read")))

	except:
		interrupted = True
		bar.close()

		# yeet everything from the queue
		while True:
			try:
				upload_queue.get_nowait()
			except:
				break

		print("\n\n")
		msg.warn("Waiting for any in-progress uploads to finish")

	# here, the tarfile should be closed -- so the zcmp should be as well.
	upload_queue.put(None)
	for w in workers:
		w.join()

	if interrupted:
		msg.error("Upload cancelled!")
		return

	manifest = oci.OciManifest(
	    BUNDLE_MANIFEST_NAME,
	    NonPacmanVersion(os_ver),
	    remote,
	    [oci.OciObject(sha, mimes.BYTES, size) for (sha, size) in zcmp.blobs],
	)

	msg.log("Uploading manifest")
	ociw.upload(manifest=manifest, platform=vm_arch)

	msg.log("Done!")


def download_bundle():
	pass
