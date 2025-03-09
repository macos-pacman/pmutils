#!/usr/bin/env python
# Copyright (c) 2025, yuki
# SPDX-License-Identifier: Apache-2.0

import sys

from typing import *

from pmutils import msg, util
from pmutils.oci import OciIndex
from pmutils.config import config
from pmutils.version import Version
from pmutils.package import sanitise_package_name


# this is fucky, because there is no good way to do a bijection from pacman versions to docker tags.
# we just replace '+' and '_' with a character that can appear in neither pacman nor docker versions
# and compare like that, which should be sufficient unless somebody silly releases a package with two
# different versions a+b and a_b.
def unsanitise_version(v: str) -> Version:
	# if the version has two dashes, the first one is actually the epoch separator (:)
	if v.count('-') == 2:
		v = v.replace('-', ':', 1)

	return Version.parse(v.replace('_', '/').replace('+', '/'))


def download_package(
    repo_name: str,
    package: str,
    version: Optional[str],
    os: Optional[str],
    arch: Optional[str],
    list_versions: bool,
):
	registry = config().registry
	r = registry.get_repository(repo_name)
	if r is None:
		msg.error_and_exit(f"Repository {repo_name} does not exist")

	ns: str = r.oci().make_namespace(for_package=sanitise_package_name(package))
	tags = r.oci().get_tags(ns)
	if len(tags) == 0:
		msg.error_and_exit(f"Package '{package}' does not exist")

	sorted_versions = sorted(map(lambda v: (unsanitise_version(v), v), tags), key=lambda x: x[0])

	msg.log(f"Found package {package}")

	if list_versions:
		msg.log2(f"Package has {len(tags)} version{'' if len(tags)==1 else 's'}:")
		for v in sorted_versions[::-1]:
			msg.log3(f"{v[0]}")
		return

	if version is None:
		# get the latest version and heck it.
		selected_tag = sorted_versions[-1][1]
		msg.log2(f"Selecting latest version")

	else:
		versions = {unsanitise_version(v): v for v in tags}
		user_ver: Optional[Version] = None
		try:
			user_ver = Version.parse(version)
		except:
			pass

		if user_ver is None or (selected_tag := versions.get(user_ver)) is None:
			# try a fuzzy-ish match
			matches = list(filter(lambda kv: str(kv[0]).startswith(version), versions.items()))
			if len(matches) == 0:
				msg.error(f"Package has no version matching '{version}'")
				msg.log2(f"Use `--list` to see available versions")
				sys.exit(1)

			msg.log2(f"Found {len(matches)} matching version{'' if len(matches)==1 else 's'} for '{version}':")
			for m in matches[::-1]:
				msg.log3(f"{m[0]}")

			msg.log2(f"Selecting latest version '{matches[-1][0]}'")
			selected_tag = matches[-1][1]

	selected_version = unsanitise_version(selected_tag)
	t = "" if str(selected_version) == selected_tag else f" {msg.BOLD}{msg.GREY}(tag: {selected_tag})"

	msg.log(f"Version: {selected_version}{t}")
	index = r.oci().get_index(ns, selected_tag)
	if index is None:
		msg.error_and_exit(f"Could not find manifest index for tag '{selected_tag}'")

	manifest_digest: str
	manifest_arch: Optional[str] = None

	if len(index.manifests) == 0:
		msg.error_and_exit(f"Invalid manifest index: no manifests!")

	elif len(index.manifests) == 1:
		m = index.manifests[0]

		if (os is not None and m.platform_os != os) or (arch is not None and m.platform_arch != arch):
			msg.error2(
			    f"Could not find manifest matching OS/architecture choice: have {m.platform_os}, {m.platform_arch}"
			)
			sys.exit(1)

		manifest_digest = m.digest
		manifest_arch = m.platform_arch

	else:
		msg.log2(f"Found {len(index.manifests)} manifests")

		candidates: list[OciIndex.ManifestShim] = []
		for m in index.manifests:
			if (m.platform_os is None or m.platform_os == os) and (m.platform_arch is None or m.platform_arch == arch):
				candidates.append(m)

		if len(candidates) > 1:
			msg.error2(f"Found more than one manifest for version '{selected_version}':")
			for c in candidates:
				msg.log3(f"os: {c.platform_os}, arch: {c.platform_arch}, digest: {c.digest}")

			msg.log2(f"Use `--os` and/or `--arch` to narrow down")
			sys.exit(1)

		elif len(candidates) == 0:
			msg.error2(f"Could not find manifest matching OS/architecture choice")
			sys.exit(1)

		else:
			manifest_digest = candidates[0].digest
			manifest_arch = candidates[0].platform_arch

	msg.log(f"Manifest: {msg.ALL_OFF}{msg.PINK}{manifest_digest}")

	# grab the manifest itself
	manifest = r.oci().get_manifest(ns, manifest_digest)
	if manifest is None:
		msg.error_and_exit(f"Failed to fetch manifest")

	msg.log2(f"Object has {len(manifest.layers)} layer{'' if len(manifest.layers) == 1 else 's'}")

	# download each layer
	total_size = sum(map(lambda l: l.size, manifest.layers))

	def chunk_gen() -> Iterator[Any]:
		for layer in manifest.layers:
			resp = r.oci().http_get(
			    f"/v2/{ns}/blobs/sha256:{layer.sha256}",
			    accept=["application/octet-stream"],
			    stream=True,
			)
			yield from resp.iter_content(1024)

	# assume zst...
	file_name = f"{manifest.name}-{selected_version}-{manifest_arch or 'any'}.pkg.tar.zst"

	util.write_file_chunks_with_progress_bar(
	    file=file_name,
	    file_size=total_size,
	    progress_bar_threshold=1 * 1024 * 1024,
	    bar_desc=msg.slog3(f"Downloading package file"),
	    data_iterator=chunk_gen(),
	)

	msg.log(f"Downloaded '{file_name}'")
