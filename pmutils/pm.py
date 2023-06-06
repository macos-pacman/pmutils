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
	operations = [ "db" ]

	if (len(sys.argv) < 2) or (sys.argv[1] not in operations):
		print(textwrap.dedent("""\
			usage: ./pm <operation> [flags]

			Supported operations:
			  db add [files...]             add packages to the database
			"""))
		sys.exit(0)

	Config.load("config.toml")

	db = Database.load("tmp/core.db.tar.zst")

	if sys.argv[1]:
		return command_db(db, sys.argv[2:])

	else:
		# unreachable
		assert False



def command_db(db: Database, args: list[str]) -> int:
	ops = [ "add", "list", "upload" ]
	if len(args) < 1 or args[0] not in ops:
		print(textwrap.dedent("""\
			usage: ./pm db <operation> [flags] [packages...]

			Operations:
			  list                  list all packages in the database (equivalent to `pacman -Q`)
			  add [packages...]     add the given packages to the database

			Supported flags:
			  ?
		"""))
		return 0

	if args[0] == "add":
		if len(args) < 2:
			msg.warn(f"Expected at least one package for 'db add'")
			return 0

		msg.log(f"Processing {len(args) - 1} new package{'' if len(args) == 2 else 's'}")
		with msg.Indent():
			for pkg in args[1:]:
				db.add(pkg)

		db.save()

	elif args[0] == "list":
		if len(args) != 1:
			msg.warn(f"Ignoring arguments after 'db list'")

		msg.log("Listing packages:")
		for pkg in db.packages():
			print(f"  {pkg.name} {msg.GREEN}{pkg.version}{msg.ALL_OFF}")

	elif args[0] == "upload":

		pass


	return 0












	# TODO: allow specifying config path
	# cfg = Config.load("config.json")
	# token = cfg.get_token()
	# assert len(token) > 0





	# msg.log("successfully authenticated")
	# return 0
