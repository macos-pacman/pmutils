#!/usr/bin/env python
# Copyright (c) 2023, yuki
# SPDX-License-Identifier: Apache-2.0

import os
import tomllib

from typing import *
from pmutils import msg
from dataclasses import dataclass
from pmutils.registry import Registry

__global_config: "Config"


def config() -> "Config":
	global __global_config
	return __global_config


def _set_config(cfg: "Config") -> "Config":
	global __global_config
	__global_config = cfg
	return __global_config


@dataclass(eq=True, frozen=True)
class CheckerConfig:
	check_out_of_date: bool
	ignore_pkgrel: bool
	ignore_haskell_pkgrel: bool

	ignore_packages: set[str]
	ignore_epochs: set[str]

	@staticmethod
	def load(obj: Optional[dict[str, Any]]) -> "CheckerConfig":
		if obj is None:
			obj = {}

		return CheckerConfig(
		    check_out_of_date=obj.get("check-out-of-date", False),
		    ignore_pkgrel=obj.get("ignore-pkgrel", False),
		    ignore_haskell_pkgrel=obj.get("ignore-haskell-pkgrel", True),
		    ignore_packages=set(obj.get("ignore-packages", [])),
		    ignore_epochs=set(obj.get("ignore-package-epochs", []))
		)


DEFAULT_CPUS = 2
DEFAULT_RAM = 8 * 1024 * 1024 * 1024
DEFAULT_USERNAME = "pacman"
DEFAULT_DISK = 100 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class SandboxConfig:
	path: Optional[str]
	macos_build: Optional[str]
	remote: Optional[str]
	username: str
	cpus: int
	ram: int
	disk: int

	@staticmethod
	def load(obj: Optional[dict[str, Any]]) -> "SandboxConfig":
		if obj is None:
			obj = {}

		return SandboxConfig(
		    path=obj.get("path"),
		    macos_build=obj.get("macos-build"),
		    remote=obj.get("remote"),
		    username=obj.get("username", DEFAULT_USERNAME),
		    cpus=obj.get("cpus", DEFAULT_CPUS),
		    ram=obj.get("ram", DEFAULT_RAM),
		    disk=obj.get("disk", DEFAULT_DISK)
		)


@dataclass(eq=True, frozen=True)
class Config:
	registry: Registry
	upstream_url: Optional[str]
	checker: CheckerConfig
	sandbox: SandboxConfig

	@staticmethod
	def load(path: str) -> None:
		with open(path, "rb") as file:
			f = tomllib.load(file)
			if "registry" not in f:
				msg.error_and_exit(f"Missing 'registry' section in config file")

			reg_url = _get(f["registry"], "url", "registry")
			reg_token = _get(f["registry"], "token", "registry")

			registry = Registry(reg_url, reg_token)

			if "repository" not in f:
				msg.warn("No repositories configured")
			else:
				repos = f["repository"]
				for name, repo in repos.items():
					r_remote = _get(repo, "remote", f"repository.{name}")
					r_database = _get(repo, "database", f"repository.{name}")
					r_release_name = _get(repo, "release-name", f"repository.{name}")
					r_root_dir = _get(repo, "root-dir", f"repository.{name}", required=False)
					if (r_root_dir is not None) and (not os.path.isabs(r_root_dir)):
						msg.error_and_exit(f"`root-dir` should be an absolute path")

					registry.add_repository(name, r_remote, r_database, r_release_name, r_root_dir)

			upstream_url = _get(f["upstream"], "url", "upstream") if "upstream" in f else None
			_set_config(
			    Config(
			        registry,
			        upstream_url,
			        CheckerConfig.load(f.get("checker")),
			        SandboxConfig.load(f.get("sandbox")),
			    )
			)


def _get(c: dict[str, Any], k: str, aa: str, *, required: bool = True, default: Any = None) -> Any:
	if k not in c:
		if required:
			msg.error_and_exit(f"Missing required key '{k}' in section '{aa}'")
		else:
			return default
	else:
		return c[k]
