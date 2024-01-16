#!/usr/bin/env python
# Copyright (c) 2023, zhiayang
# SPDX-License-Identifier: Apache-2.0

import os
import click

from typing import *
from pmutils.config import Config, config
from pmutils import msg, build, check, diff, rebase
from pmutils.registry import Registry, Repository

DEFAULT_CONFIG = "config.toml"
CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help'])

@click.group(context_settings=CONTEXT_SETTINGS)
@click.pass_context
@click.option("-c", "--config", metavar="CONFIG", default=DEFAULT_CONFIG,
			  required=False,
			  help="The configuration file to use")
def cli(ctx: Any, config: str) -> int:
	if not os.path.exists(config):
		xdg_home = os.getenv("XDG_CONFIG_HOME", f"{os.getenv('HOME')}/.config")
		cfg = f"{xdg_home}/pmutils/config.toml"

		if os.path.exists(cfg):
			ctx.meta["config_file"] = cfg
		else:
			msg.error_and_exit(f"Could not load `config.toml`")

	else:
		ctx.meta["config_file"] = config

	return 0


@cli.command(name="add")
@click.pass_context
@click.option("-v", "--verbose", is_flag=True, default=False, help="Print verbose output")
@click.option("-k", "--keep", is_flag=True, default=False, help="Keep packages after uploading (do not delete)")
@click.option("--allow-downgrade", is_flag=True, default=False, help="Allow downgrading packages when adding them to the repository")
@click.option("--upload/--no-upload", is_flag=True, default=True, help="Upload packages to remote repositories")
@click.argument("repo", required=True)
@click.argument("package", required=True, nargs=-1, type=click.Path(exists=True, dir_okay=False))
def db_add(ctx: Any,
		   repo: str,
		   package: list[click.Path],
		   verbose: bool,
		   upload: bool,
		   keep: bool,
		   allow_downgrade: bool):
	"""Add PACKAGE files to the DATABASE"""

	Config.load(ctx.meta["config_file"])
	registry = config().registry

	r = registry.get_repository(repo)
	if r is None:
		msg.error_and_exit(f"Repository {repo} does not exist")

	msg.log(f"Processing {len(package)} new package{'' if len(package) == 1 else 's'}")
	with msg.Indent():
		for pkg in package:
			r.database.add(str(pkg), verbose=verbose, allow_downgrade=allow_downgrade)

	if upload:
		r.sync()

	if not keep:
		msg.log(f"Deleting package files")
		for pkg in [p for p in map(str, package) if os.path.exists(p)]:
			os.remove(pkg)
			if verbose:
				msg.log2(f"{pkg}", end='')

			if os.path.exists(f"{pkg}.sig"):
				os.remove(f"{pkg}.sig")

				if verbose:
					print(f", {msg.bold('.sig')}")

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


def get_outdated_packages(repo: str, verbose: bool) -> list[str]:
	registry = config().registry
	r = registry.get_repository(repo)
	if r is None:
		msg.error_and_exit(f"Repository {repo} does not exist")

	url = config().upstream_url
	if url is None:
		msg.error_and_exit(f"Cannot check for package updates without any `upstream` configured")

	return check.check_packages(url, r, config().checker, verbose=verbose)



@cli.command(name="check", help="Check for out-of-date packages in the database")
@click.pass_context
@click.argument("repo", required=True)
def cmd_check(ctx: Any, repo: str):
	Config.load(ctx.meta["config_file"])

	"""Check packages in DATABASE for any out-of-date packages"""
	get_outdated_packages(repo, verbose=True)


@cli.command(name="diff", help="Generate diffs between local PKGBUILD and upstream (Arch Linux)")
@click.argument("package", required=True, nargs=-1, type=click.Path(exists=True))
def cmd_diff(package: list[click.Path]):
	for file in map(str, package):
		if not os.path.isdir(file):
			msg.log2(f"Skipping non-folder '{file}'")
			continue
		elif not os.path.exists(f"{file}/PKGBUILD"):
			msg.log2(f"Skipping folder '{file}' with no PKGBUILD")
			continue

		if (dd := diff.diff_package(file)) is not None:
			for d in dd.files:
				with open(f"{file}/{d.name}.pmdiff", "w") as f:
					f.write(d.diff)

	msg.log("Done")


@cli.command(name="rebase", help="Automatically rebase PKGBUILDs on top of upstream (Arch Linux)")
@click.pass_context
@click.argument("package", required=False, nargs=-1, type=click.Path(exists=False))
@click.option("--repo", default=None, metavar="REPO", help="Use REPO as the package repository")
@click.option("-o", "--outdated", is_flag=True, default=False, help="Automatically rebase all outdated packages (requires `--repo`)")
@click.option("-f", "--force", is_flag=True, default=False, help="Proceed even if the working directory is dirty")
@click.option("-k", "--keep", is_flag=True, default=False, help="Keep pmdiff files even if patches apply cleanly")
@click.option("-b", "--build", is_flag=True, default=False, help="Build packages after rebasing them")
@click.option("-i", "--install", is_flag=True, default=False, help="Install packages after building them (implies `--build`)")
@click.option("--allow-downgrade", is_flag=True, default=False, help="Allow downgrading packages when adding them to the repository")
@click.option("--upload/--no-upload", is_flag=True, default=True, help="Upload built packages to remote (requires `--build`)")
@click.option("--commit/--no-commit", is_flag=True, default=True, help="Commit the patched files with git if successful")
@click.option("--check/--no-check", help="Run the check() function in the PKGBUILD", default=True)
def cmd_rebase(ctx: Any, package: list[click.Path], repo: Optional[str], outdated: bool,
	force: bool, keep: bool, build: bool, install: bool, check: bool, upload: bool, commit: bool, allow_downgrade: bool):

	packages: list[str] = []
	registry: Optional[Registry] = None
	r: Optional[Repository] = None

	if repo is not None:
		Config.load(ctx.meta["config_file"])
		registry = config().registry
		if (r := registry.get_repository(repo)) is None:
			msg.error_and_exit(f"Repository {repo} does not exist")

	if outdated:
		if repo is None:
			msg.error_and_exit(f"`--outdated` flag requires `--repo` to be provided")

		assert r is not None
		if r.root_dir is None:
			msg.error_and_exit(f"Cannot rebase packages without configured `root-dir` setting")

		pp = get_outdated_packages(repo=repo, verbose=False)
		wanted = set(map(str, package))

		# get the list of outdated packages.
		# TODO: might need smarter handling for split packages (where just using the folder doesn't work)
		packages = [f"{r.root_dir}/{p}" for p in pp if p in wanted]

		skipped = wanted - set(pp)
		if len(skipped) > 0:
			msg.log(f"Skipping up-to-date packages:")
			for skip in skipped:
				msg.log2(skip)

	else:
		if len(package) == 0:
			msg.log(f"No packages provided")
			return

		if (build or install) and (repo is None):
			msg.error_and_exit(f"Building or installing requires `--repo` option")

		packages = list(map(str, package))


	fails: list[str] = []
	for p in packages:
		x = rebase.rebase_package(p, force=force, keep_diffs=keep,
			registry=registry, repository=r, build_pkg=build,
			install_pkg=install, check_pkg=check, upload=upload, commit=commit, allow_downgrade=allow_downgrade)
		if not x:
			fails.append(p)

	if len(fails) > 0:
		msg.warn(f"Some packages might require manual intervention:")
		for f in fails:
			msg.log2(f)

	msg.log("Done")



@cli.command(name="build", help="Build a local PKGBUILD")
@click.pass_context
@click.option("--verify-pgp/--no-verify-pgp", help="Verify PGP signatures", default=False)
@click.option("--check/--no-check", help="Run the check() function in the PKGBUILD", default=True)
@click.option("--keep/--delete", help="Keep the built package after adding it (requires `--add`)", default=False)
@click.option("--upload/--no-upload", is_flag=True, default=True, help="Upload built packages to remote repositories")
@click.option("--allow-downgrade", is_flag=True, default=False, help="Allow downgrading packages when adding them to the repository")
@click.option("--add", "database", metavar="DATABASE", help="Add built package to the database", required=False)
@click.option("-i", "--install", is_flag=True, help="Install the package after building")
def cmd_build(ctx: Any, verify_pgp: bool,
			  check: bool,
			  keep: bool,
			  upload: bool,
			  install: bool,
			  database: Optional[str],
			  allow_downgrade: bool):
	"""Build a package"""

	Config.load(ctx.meta["config_file"])
	registry = config().registry

	build.makepkg(registry, verify_pgp=verify_pgp, check=check, keep=keep, database=database,
		upload=upload, install=install, allow_downgrade=allow_downgrade)
	msg.log("Done")






def main() -> int:
	return cli()
