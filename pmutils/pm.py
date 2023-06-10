#!/usr/bin/env python
# Copyright (c) 2023, zhiayang
# SPDX-License-Identifier: Apache-2.0

import sys
import textwrap

from pmutils import msg
from pmutils.config import Config, config
from pmutils.package import Package
from pmutils.database import Database

def main() -> int:
	operations = [ "add", "list" ]

	if (len(sys.argv) < 2) or (sys.argv[1] not in operations):
		print(textwrap.dedent("""\
			usage: ./pm <operation> [flags]

			Supported operations:
			  add <repo> [files...]             add packages to the repository
			  list <repo>                       list packages in the repository
			"""))
		sys.exit(0)

	# load config file first
	Config.load("config.toml")
	registry = config().registry

	op = sys.argv[1]
	args = sys.argv[2:]

	if op == "add":
		if len(args) < 2:
			msg.warn(f"Expected repository and at least one package for 'add'")
			return 1

		repo = registry.get_repository(args[0])
		if repo is None:
			msg.error_and_exit(f"Repository {args[0]} does not exist")

		msg.log(f"Processing {len(args) - 1} new package{'' if len(args) == 2 else 's'}")
		with msg.Indent():
			for pkg in args[1:]:
				repo.database.add(pkg)

		repo.sync()
		msg.log("Done")

	elif op == "list":
		if len(args) != 1:
			print(f"Usage: ./pm list <repo>")
			return 0

		msg.log("Packages:")
		repo = registry.get_repository(args[0])
		if repo is None:
			msg.error_and_exit(f"Repository {args[0]} does not exist")

		for p in repo.database.packages():
			print(f"  {p.name} {msg.GREEN}{p.version}{msg.ALL_OFF}")


	return 0


