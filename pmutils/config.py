#!/usr/bin/env python
# Copyright (c) 2023, zhiayang
# SPDX-License-Identifier: Apache-2.0

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
class Config:
	registry: Registry

	@staticmethod
	def load(path: str) -> None:
		with open(path, "rb") as file:
			f = tomllib.load(file)
			if "registry" not in f:
				msg.error_and_exit(f"missing 'registry' section in config file")

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
					registry.add_repository(name, r_remote, r_database, r_release_name)

			_set_config(Config(registry))



def _get(c: dict[str, Any], k: str, aa: str, *, required: bool = True, default: Any = None) -> Any:
	if k not in c:
		if required:
			msg.error_and_exit(f"Missing required key '{k}' in section '{aa}'")
		else:
			return default
	else:
		return c[k]

