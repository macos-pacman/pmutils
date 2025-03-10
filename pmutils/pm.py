#!/usr/bin/env python
# Copyright (c) 2023, yuki
# SPDX-License-Identifier: Apache-2.0

import os
import time
import click
import signal
import contextlib
import importlib.metadata as im

from typing import *
from pmutils.config import Config, config
from pmutils.registry import Registry, Repository
from pmutils import msg, build, check, diff, fetch, rebase, vm, download

DEFAULT_CONFIG = "config.toml"
CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help'])


@click.group(context_settings=CONTEXT_SETTINGS)
@click.pass_context
@click.option(
    "-c", "--config", metavar="CONFIG", default=DEFAULT_CONFIG, required=False, help="The configuration file to use"
)
@click.version_option(im.version("pmutils"), "--version", "-V", prog_name="pmutils")
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
@click.option(
    "--allow-downgrade",
    is_flag=True,
    default=False,
    help="Allow downgrading packages when adding them to the repository"
)
@click.option("--upload/--no-upload", is_flag=True, default=True, help="Upload packages to remote repositories")
@click.option("--repo", metavar="REPO", required=False, help="Use the given repository instead of the default")
@click.argument("package", required=True, nargs=-1, type=click.Path(exists=True, dir_okay=False))
def cmd_add(
    ctx: Any,
    repo: Optional[str],
    package: list[click.Path],
    verbose: bool,
    upload: bool,
    keep: bool,
    allow_downgrade: bool
):
	"""Add PACKAGE files to the DATABASE"""

	Config.load(ctx.meta["config_file"])
	registry = config().registry

	if (repo is None) and (repo := config().registry.get_default_repository()) is None:
		msg.error_and_exit(f"Unable to determine default repository, specify explicitly")

	r = registry.get_repository(repo)
	if r is None:
		msg.error_and_exit(f"Repository {repo} does not exist")

	msg.log(f"Processing {len(package)} new package{'' if len(package) == 1 else 's'}")
	with msg.Indent():
		for pkg in map(str, package):
			if pkg.endswith(".sig"):
				continue
			r.add_package(pkg, verbose=verbose, allow_downgrade=allow_downgrade)

	r.sync(upload)

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
@click.argument("repo", required=False)
def cmd_list(ctx: Any, repo: Optional[str]):
	"""List packages in DATABASE"""

	Config.load(ctx.meta["config_file"])
	registry = config().registry

	if (repo is None) and (repo := config().registry.get_default_repository()) is None:
		msg.error_and_exit(f"Unable to determine default repository, specify explicitly")

	msg.log("Packages:")
	r = registry.get_repository(repo)
	if r is None:
		msg.error_and_exit(f"Repository {repo} does not exist")

	for p in r.database().packages():
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
@click.argument("repo", required=False)
def cmd_check(ctx: Any, repo: Optional[str]):
	Config.load(ctx.meta["config_file"])

	if (repo is None) and (repo := config().registry.get_default_repository()) is None:
		msg.error_and_exit(f"Unable to determine default repository, specify explicitly")
	"""Check packages in DATABASE for any out-of-date packages"""
	get_outdated_packages(repo, verbose=True)


@cli.command(name="fetch", help="Fetch PKGBUILD recipes from upstream")
@click.pass_context
@click.option(
    "-f", "--force", is_flag=True, default=False, help="Proceed even if the package directory exists and/or is dirty"
)
@click.option("--repo", metavar="REPO", required=False, help="Use the given repository instead of the default")
@click.argument("package", required=True, nargs=-1, type=click.STRING)
def cmd_fetch(ctx: Any, package: list[str], repo: Optional[str], force: bool):
	"""Fetch package sources from upstream, creating a new package locally"""
	Config.load(ctx.meta["config_file"])

	if (repo is None) and (repo := config().registry.get_default_repository()) is None:
		msg.error_and_exit(f"Unable to determine default repository, specify explicitly")

	if (r := config().registry.get_repository(repo)) is None:
		msg.error_and_exit(f"Repository '{repo}' does not exist")
	elif r.root_dir() is None:
		msg.error_and_exit(f"`root-dir` not configured for repository, cannot fetch")

	for pkg in package:
		# note: we know root_dir is not none here
		fetch.fetch_upstream_package(root_dir=cast(str, r.root_dir()), pkg_name=pkg, force=force)

	msg.log("Done")


@cli.command(name="diff", help="Generate diffs between local PKGBUILD and upstream (Arch Linux)")
@click.option(
    "-k", "--keep", is_flag=True, default=False, help="Keep old files after updating (useless without `--update`)"
)
@click.option("-l", "--fetch", is_flag=True, default=False, help="Diff against the latest upstream files")
@click.option("-f", "--force", is_flag=True, default=False, help="Proceed even if the working directory is dirty")
@click.option("-u", "--update", is_flag=True, default=False, help="Update local files with the latest upstream version")
@click.option(
    "--commit/--no-commit", is_flag=True, default=True, help="Commit the patched files with git if successful"
)
@click.argument("package", required=True, nargs=-1, type=click.Path(exists=True))
def cmd_diff(package: list[click.Path], force: bool, fetch: bool, update: bool, keep: bool, commit: bool):
	"""Generates diffs between currently edited package sources and upstream"""
	for file in map(str, package):
		if not os.path.isdir(file):
			msg.log2(f"Skipping non-folder '{file}'")
			continue
		elif not os.path.exists(f"{file}/PKGBUILD"):
			msg.log2(f"Skipping folder '{file}' with no PKGBUILD")
			continue

		diff.diff_package(file, force=force, keep_old=keep, fetch_latest=fetch, update_local=update, commit=commit)

	msg.log("Done")


@cli.command(name="rebase", help="Automatically rebase PKGBUILDs on top of upstream (Arch Linux)")
@click.pass_context
@click.argument("package", required=False, nargs=-1, type=click.Path(exists=False))
@click.option("--repo", required=False, default=None, metavar="REPO", help="Use REPO as the package repository")
@click.option(
    "-o",
    "--outdated",
    is_flag=True,
    default=False,
    help="Automatically rebase all outdated packages (requires `--repo`)"
)
@click.option("-f", "--force", is_flag=True, default=False, help="Proceed even if the working directory is dirty")
@click.option("-b", "--build", is_flag=True, default=False, help="Build packages after rebasing them")
@click.option(
    "-i", "--install", is_flag=True, default=False, help="Install packages after building them (implies `--build`)"
)
@click.option(
    "--allow-downgrade",
    is_flag=True,
    default=False,
    help="Allow downgrading packages when adding them to the repository"
)
@click.option(
    "--commit/--no-commit", is_flag=True, default=True, help="Commit the patched files with git if successful"
)
@click.option(
    "--upload/--no-upload", is_flag=True, default=True, help="Upload built packages to remote (requires `--build`)"
)
@click.option("--check/--no-check", help="Run the check() function in the PKGBUILD", default=True)
@click.option("--buildnum/--no-buildnum", help="Automatically increment the build number in the PKGBUILD", default=True)
def cmd_rebase(
    ctx: Any,
    package: list[click.Path],
    repo: Optional[str],
    outdated: bool,
    force: bool,
    build: bool,
    install: bool,
    check: bool,
    upload: bool,
    commit: bool,
    allow_downgrade: bool,
    buildnum: bool
):
	"""Rebase patched package sources on top of latest upstream sources"""
	packages: list[str] = []
	registry: Optional[Registry] = None
	r: Optional[Repository] = None

	Config.load(ctx.meta["config_file"])
	registry = config().registry

	if repo is not None:
		if (r := registry.get_repository(repo)) is None:
			msg.error_and_exit(f"Repository {repo} does not exist")
	elif (rn := registry.get_default_repository()) is not None:
		r = registry.get_repository(rn)

	if outdated:
		if r is None:
			msg.error_and_exit(f"`--outdated` flag requires `--repo` to be provided")

		assert r is not None
		if r.root_dir() is None:
			msg.error_and_exit(f"Cannot rebase packages without configured `root-dir` setting")

		pp = get_outdated_packages(repo=r.name(), verbose=False)
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

		# if the folder exists, assume it's a folder; otherwise, assume it's a package if we were given the repo.
		def _folder_or_pkgname(p: str) -> str:
			if (r is not None) and ('/' not in p) and ('.' not in p) and (r.root_dir()
			                                                              is not None) and r.database().contains(p):
				return f"{r.root_dir}/{p}"
			return p

		packages = list(map(_folder_or_pkgname, map(str, package)))

	fails: list[str] = []
	for p in packages:
		x = rebase.rebase_package(
		    p,
		    force=force,
		    registry=registry,
		    repository=r,
		    build_pkg=build,
		    install_pkg=install,
		    check_pkg=check,
		    upload=upload,
		    commit=commit,
		    allow_downgrade=allow_downgrade,
		    update_buildnum=buildnum
		)
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
@click.option("--buildnum/--no-buildnum", help="Automatically increment the build number in the PKGBUILD", default=True)
@click.option("--upload/--no-upload", is_flag=True, default=True, help="Upload built packages to remote repositories")
@click.option(
    "--allow-downgrade",
    is_flag=True,
    default=False,
    help="Allow downgrading packages when adding them to the repository"
)
@click.option("--repo", metavar="REPO", help="Use the given repository when adding packages", required=False)
@click.option("-a", "--add", is_flag=True, default=False, help="Add built package to the database")
@click.option("-i", "--install", is_flag=True, help="Install the package after building")
@click.option("-k", "--keep", is_flag=True, help="Keep the package after building it")
@click.option("-d", "--delete", is_flag=True, help="Delete the package after building it")
@click.option("--sync/--no-sync", is_flag=True, default=True, help="Sync and update packages before building")
@click.option("--sandbox/--no-sandbox", is_flag=True, default=True, help="Use the VM Sandbox to build packages")
@click.option(
    "--sandbox-folder", type=str, default=None, metavar="FOLDER", help="Use FOLDER as the build folder in the sandbox"
)
@click.option("--sandbox-keep", is_flag=True, default=False, help="Keep the remote build folder even if build succeeds")
@click.argument("directory", required=False, nargs=1, type=click.Path(exists=True, dir_okay=True))
def cmd_build(
    ctx: Any,
    verify_pgp: bool,
    directory: Optional[str],
    check: bool,
    keep: bool,
    delete: bool,
    upload: bool,
    install: bool,
    repo: Optional[str],
    add: bool,
    allow_downgrade: bool,
    buildnum: bool,
    sync: bool,
    sandbox: bool,
    sandbox_folder: Optional[str],
    sandbox_keep: bool,
):
	if keep and delete:
		msg.error_and_exit(f"`--keep` and `--delete` cannot be used together")

	Config.load(ctx.meta["config_file"])
	registry = config().registry

	if (repo is None) and (repo := config().registry.get_default_repository()) is None:
		msg.error_and_exit(f"Unable to determine default repository, specify explicitly")

	if not sandbox and sandbox_folder:
		msg.error_and_exit(f"`--sandbox-folder` requires `--sandbox`")

	# if keep and delete were both false, then it's "auto":
	# keep if we are not adding, delete if we are.
	if (not keep) and (not delete):
		_keep = add
	elif delete:
		_keep = False
	else:
		_keep = keep

	with contextlib.chdir(directory or ".") as _:
		build.makepkg(
		    registry,
		    verify_pgp=verify_pgp,
		    check=check,
		    keep=_keep,
		    database=(repo if add else None),
		    upload=upload,
		    install=install,
		    allow_downgrade=allow_downgrade,
		    update_buildnum=buildnum,
		    sync=sync,
		    use_sandbox=sandbox,
		    sandbox_folder=sandbox_folder,
		    sandbox_keep=sandbox_keep,
		)

	msg.log("Done")


@cli.command(
    name="download",
    help=
    "Download PACKAGE from the remote repository. Use VERSION if specified, otherwise download the latest version. Supports limited fuzzy-matching of versions."
)
@click.argument("package", required=True)
@click.argument("version", required=False)
@click.option("--repo", required=False)
@click.option("--os", required=False)
@click.option("--arch", required=False)
@click.option("-l", "--list", is_flag=True, default=False, help="List package versions instead of downloading")
@click.pass_context
def cmd_download(
    ctx: Any,
    repo: Optional[str],
    package: str,
    version: Optional[str],
    os: Optional[str],
    arch: Optional[str],
    list: bool
):
	Config.load(ctx.meta["config_file"])

	if (repo is None) and (repo := config().registry.get_default_repository()) is None:
		msg.error_and_exit(f"Unable to determine default repository, specify explicitly")

	download.download_package(repo_name=repo, package=package, version=version, os=os, arch=arch, list_versions=list)


@cli.command(name="sbman", help="Manage the sandbox virtual machine")
@click.option("--gui", is_flag=True, default=False, help="Run with the GUI open")
@click.option("--upload", is_flag=True, default=False, help="Upload the current VM bundle to the registry")
@click.option("--download", is_flag=True, default=False, help="Download the VM bundle from the registry")
@click.option("--restore", is_flag=True, default=False, help="Restore the VM")
@click.option("--bootstrap", is_flag=True, default=False, help="Perform Pacman bootstrapping")
@click.option("--ipsw", required=False, metavar="IPSW", help="The path to a local IPSW file to use for restoring")
@click.pass_context
def cmd_sandbox(
    ctx: Any,
    gui: bool,
    restore: bool,
    bootstrap: bool,
    upload: bool,
    download: bool,
    ipsw: Optional[str],
):
	if upload and download:
		msg.error_and_exit(f"`--upload` and `--download` cannot be used together")
	elif (upload or download) and (gui or restore or bootstrap or ipsw):
		msg.error_and_exit(f"`--upload` and `--download` must be used alone (without other options)")

	Config.load(ctx.meta["config_file"])
	if upload:
		vm.remote.upload_bundle()
	elif download:
		vm.remote.download_bundle()
	else:
		vz = vm.manager.load_or_create_sandbox(gui=gui, restore=restore, bootstrap=bootstrap, ipsw_path=ipsw)
		if vz is None:
			return

		# wait till we get sigwait, then exit.
		if not vz.stopped:
			msg.log2("Use Ctrl-C (or send SIGINT) to stop")
			signal.signal(signal.SIGINT, lambda _1, _2: vz.stop(wait=False))

			while vz.get_ip() is None:
				time.sleep(0.5)

		vz.wait()


@cli.command(name="internal-testing", hidden=True)
@click.pass_context
def cmd_internal_testing(ctx: Any):
	Config.load(ctx.meta["config_file"])
	print("nothing to see here")


def main() -> int:
	return cli()
