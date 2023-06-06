#!/usr/bin/env python
# Copyright (c) 2023, zhiayang
# SPDX-License-Identifier: Apache-2.0

import os
from pmutils import msg

from typing import *
from dataclasses import dataclass

@dataclass(eq=True, frozen=True)
class Repository:
	name: str
	remote: str
	db_file: str






class Registry:
	def __init__(self, url: str, token: str, oauth: Optional[str]):
		self._url = url
		self._token = token
		self._oauth = oauth
		self._repos: dict[str, Repository] = dict()

	def add_repository(self, name: str, remote: str, db_file: str):
		if name in self._repos:
			msg.error_and_exit(f"duplicate repository '{name}'")

		if not db_file.endswith(".db"):
			msg.error_and_exit(f"db path should end in `.db`, and not have .tar.*")

		self._repos[name] = Repository(name, remote, db_file)
