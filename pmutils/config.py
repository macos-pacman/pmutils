#!/usr/bin/env python
# Copyright (c) 2023, zhiayang
# SPDX-License-Identifier: Apache-2.0

import json
import requests

from . import msg
from typing import *
from dataclasses import dataclass

_FILENAME = "config.json"

@dataclass
class User:
	username: str
	token: str
	repo: str
	oauth: Optional[str]

	@staticmethod
	def from_file() -> "User":
		with open(_FILENAME, "r") as file:
			f = json.load(file)
			if "username" not in f or "token" not in f or "repo" not in f:
				msg.error_and_exit(f"{_FILENAME} must be a JSON dictionary with 'username', 'token', and 'repo' keys")

			elif ':' in f["username"]:
				msg.error_and_exit(f"username cannot contain a colon")

			user = User(f["username"], f["token"], f["repo"], f.get("oauth"))
			msg.log(f"repo: {user.username}/{user.repo}")
			return user


def get_token(user: User) -> str:
	if user.oauth is not None:
		msg.log("using existing OAuth token")
		return user.oauth

	resp = requests.get("https://ghcr.io/token", {
		"scope": f"repository:{user.username}/{user.repo}:*",
	}, auth=(user.username, user.token))

	if resp.status_code != 200:
		msg.error_and_exit(f"failed to authenticate!\n{resp.text}")

	json = resp.json()
	if "token" not in json:
		msg.error_and_exit(f"response did not return a token!\n{resp.text}")

	return json["token"]
