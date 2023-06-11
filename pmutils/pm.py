#!/usr/bin/env python
# Copyright (c) 2023, zhiayang
# SPDX-License-Identifier: Apache-2.0

import click

from typing import *
from pmutils import msg
from pmutils.config import Config, config

DEFAULT_CONFIG = "config.toml"
CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help'])

@click.group(context_settings=CONTEXT_SETTINGS)
@click.pass_context
@click.option("-c", "--config", default=DEFAULT_CONFIG,
			  required=False,
			  type=click.Path(exists=True, dir_okay = False),
			  help="The configuration file to use")
def cli(ctx: Any, config: str) -> int:
	ctx.meta["config_file"] = config
	return 0


@cli.command(name="add")
@click.pass_context
@click.option("-v", "--verbose", is_flag=True, help="Print verbose output")
@click.option("-s", "--skip-upload", is_flag=True, help="Do not upload to remote repositories")
@click.argument("repo", required=True)
@click.argument("package", nargs=-1, type=click.Path(exists=True, dir_okay=False))
def db_add(ctx: Any, repo: str, package: list[click.Path], verbose: bool = False, skip_upload: bool = False):
	"""Add PACKAGE files to the DATABASE"""

	Config.load(ctx.meta["config_file"])
	registry = config().registry

	r = registry.get_repository(repo)
	if r is None:
		msg.error_and_exit(f"Repository {repo} does not exist")

	msg.log(f"Processing {len(package)} new package{'' if len(package) == 1 else 's'}")
	with msg.Indent():
		for pkg in package:
			r.database.add(str(pkg), verbose=verbose)

	if not skip_upload:
		r.sync()

	msg.log("Done")


@cli.command(name="list", help="List packages in the database")
@click.pass_context
@click.argument("repo", required=True)
def db_list(ctx: Any, repo: str):
	"""List packages in DATABASE"""

	Config.load(ctx.meta["config_file"])
	registry = config().registry

	msg.log("Packages:")
	r = registry.get_repository(repo)
	if r is None:
		msg.error_and_exit(f"Repository {repo} does not exist")

	for p in r.database.packages():
		print(f"  {p.name} {msg.GREEN}{p.version}{msg.ALL_OFF}")




def main() -> int:
	return cli()
