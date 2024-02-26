#!/usr/bin/env python3
# Copyright (c) 2024, zhiayang
# SPDX-License-Identifier: Apache-2.0

import os
import time
import shlex
import atexit
import subprocess

from typing import *
from pmutils import msg, util
from pmutils.config import config
from pmutils.vm.manager import VMSandBox

SSH_OPTIONS = ["-o", "LogLevel=QUIET", "-o", "UserKnownHostsFile=/dev/null", "-o", "StrictHostKeyChecking=no"]


class PackageBuilder:
	ssh_identity: Optional[str]
	ssh_username: Optional[str]
	ssh_hostname: Optional[str]
	ssh_cmd_prefix: list[str]
	vm: Optional[VMSandBox]
	local: bool
	owned: bool

	def __init__(self, sandboxed: bool):
		self.vm = None

		if not sandboxed:
			self.owned = False
			self.local = True
			return

		self.local = False

		# we just need to check if the vm is running
		# but because i can't design APIs, it's not that easy
		if (sandbox_path := config().sandbox.path) is None:
			msg.error_and_exit(f"Sandbox path not configured! Set `sandbox.path` in your config.toml")

		# check if there is a `vm.bundle` in there
		bundle_path = os.path.join(sandbox_path, "vm.bundle")

		ip: Optional[str] = None
		if os.path.exists(f"{bundle_path}/.vm-running"):
			# vm is already running -- don't start/stop it.
			# get the ip address and make the connection
			self.owned = False
			if (mac_addr := VMSandBox.get_mac_address(bundle_path)) is None:
				msg.error_and_exit(f"VM did not have a MAC address!")

			self.vm = None
			ip = VMSandBox.get_ip_from_mac(mac_addr)
			msg.log(f"Connecting to existing VM at {ip}")

		else:
			self.owned = True
			msg.log(f"Starting sandbox VM")
			if (vm := VMSandBox.start(bundle_path, gui=False)) is None:
				msg.error_and_exit("Failed to start VM!")

			self.vm = vm

			# wait for the ip
			msg.log(f"Waiting for VM to start...")
			for _ in range(30):
				if (ip := self.vm.get_ip()) is not None:
					break
				time.sleep(1)

		if ip is None:
			msg.error_and_exit("Failed to get IP of VM!")

		self.ssh_identity = f"{bundle_path}/../id_ed25519"
		self.ssh_username = config().sandbox.username
		self.ssh_hostname = ip
		self.ssh_cmd_prefix = [
		    "ssh",
		    "-t",
		    *SSH_OPTIONS,
		    "-i",
		    self.ssh_identity,
		    f"{self.ssh_username}@{self.ssh_hostname}",
		    "--",
		]

		atexit.register(lambda: self.__del__())

	def __del__(self):
		if self.vm is not None and not self.vm.stopped:
			self.vm.stop()

	def run(self,
	        cmd: str | list[str],
	        *,
	        env: dict[str, str] = {},
	        capture: bool = False) -> subprocess.CompletedProcess[str]:

		if self.local:
			return subprocess.run(cmd, env=env, capture_output=capture, text=True)

		if not isinstance(cmd, str):
			cmd = shlex.join(cmd)

		return subprocess.run(
		    [
		        *self.ssh_cmd_prefix,
		        f". /opt/pacman/etc/profile; {cmd}",
		    ],
		    capture_output=capture,
		    text=True,
		)

	def scp(self, args: list[str]) -> bool:
		return subprocess.run([
		    "scp",
		    *SSH_OPTIONS,
		    "-q",
		    "-i",
		    cast(str, self.ssh_identity),
		    *args,
		]).returncode == 0

	def copy_to_remote(self, src: str, dest: str):
		return self.scp([src, f"{self.ssh_username}@{self.ssh_hostname}:{dest}"])

	def copy_from_remote(self, src: str, dest: str):
		return self.scp([f"{self.ssh_username}@{self.ssh_hostname}:{src}", dest])

	# runs makepkg either locally or remotely (if local, passes env),
	# then returns a list of paths to the built packages (if successful).
	# for remote cases, the packages are SCP-ed into a temp folder.
	def makepkg(
	    self,
	    extra_args: list[str],
	    env: dict[str, str],
	    pkgdest: str,
	    check: bool,
	    sandbox_folder: Optional[str],
	    sandbox_keep: bool,
	) -> Optional[list[str]]:
		if self.local:
			env["PKGDEST"] = pkgdest
			try:
				if self.run(["makepkg", "-f", *extra_args, f"PKGDEST={pkgdest}"], env=env).returncode != 0:
					return None
			except:
				return None

			# for local builds, pkgdest must exist
			pkgs: list[str] = []
			for pkg in os.listdir(pkgdest):
				if pkg.endswith(".pkg.tar.zst"):
					pkgs.append(pkg)
			return pkgs

		msg.log(f"Running `makepkg` remotely")
		srcinfo = util.get_srcinfo("./PKGBUILD")

		# for remote builds, cache the downloaded sources together since we
		# use a new tmpdir each time. don't spam servers i guess
		srcdest = f"/tmp/makepkg.srcdest/{srcinfo.pkgbase}/"
		if self.run(f"mkdir -p {srcdest}").returncode != 0:
			msg.error2(f"Failed to create SRCDEST at '{srcdest}'")
			return None

		if sandbox_folder is not None:
			tmpdir = sandbox_folder
			self.run(f"mkdir -p {tmpdir}")
		else:
			if (tmp := self.run(f"mktemp -d -t makepkg.{srcinfo.pkgbase}", capture=True)).returncode != 0:
				msg.error("Could not make temporary working directory")
				return None

			tmpdir = tmp.stdout.strip()

		msg.log(f"Working directory: {msg.PINK}{tmpdir}{msg.ALL_OFF}")

		# handle a ^C at this point; delete the build folder unless --sandbox-keep was passed
		try:

			msg.log2(f"Copying PKGBUILD")
			self.copy_to_remote("./PKGBUILD", f"{tmpdir}/")

			# copy the local source files (during which we need to figure out which ones are local)
			msg.log2(f"Copying local source files")
			for src in [
			    *srcinfo.fields["source"],
			    *(srcinfo.fields.get("install", [])),
			    *[y for x in srcinfo.subpkgs for y in x[1].get("install", [])]
			]:
				if "::" in src or "http://" in src or "https://" in src or "git://" in src or "svn://" in src:
					# remote source
					continue

				# local source
				msg.log3(f"{src}")
				self.copy_to_remote(f"./{src}", f"{tmpdir}/")

			msg.log(f"Ensuring system is up-to-date")
			if self.run("sudo pacman --noconfirm -Syu").returncode != 0:
				return None

			# check package list
			before_pkgs = set(map(lambda x: x.strip(), self.run("pacman -Q", capture=True).stdout.splitlines()))

			env_prefix = f"PKGDEST={tmpdir} SRCDEST={srcdest}"
			makepkg_extra_args = ' '.join([shlex.quote(x) for x in extra_args])

			ok = self.run(
			    ';'.join([
			        f"cd {tmpdir}",
			        f"{env_prefix} makepkg --noconfirm --nocheck -srf {makepkg_extra_args}",
			    ])
			).returncode == 0

			# if check, check. the reason we do this in 2 steps is to prevent the package
			# from picking up any of the `checkdepends` and autoconf-ing them into the build.
			# there should not be any extra burden on the package repos since the stuff we
			# downloaded from the actual build (though uninstalled) should still be cached.
			if ok and check:
				msg.log("Running package checks")
				ok = self.run(
				    ';'.join([
				        f"cd {tmpdir}",
				        f"{env_prefix} makepkg --noconfirm --check-only -ers {makepkg_extra_args}",
				    ])
				).returncode == 0

			after_pkgs = set(map(lambda x: x.strip(), self.run("pacman -Q", capture=True).stdout.splitlines()))
			if before_pkgs != after_pkgs:
				msg.warn("Installed packages changed! (this should not happen)")

				new_pkgs = after_pkgs - before_pkgs
				msg.log2("These packages appeared:")
				for p in sorted(new_pkgs):
					msg.log3(f"{p}")

				gone_pkgs = before_pkgs - after_pkgs
				msg.log2("These packages disappeared:")
				for p in sorted(gone_pkgs):
					msg.log3(f"{p}")

			if not ok:
				return None

			# ok now copy all the packages over
			msg.log(f"Copying build products")
			built_pkgs = [
			    s.strip() for s in self.run(f"ls -1 {tmpdir}/*.pkg.tar.zst", capture=True).stdout.splitlines()
			]
			for p in built_pkgs:
				self.copy_from_remote(p, f"{pkgdest}/{os.path.basename(p)}")
				self.run(f"rm -f {p}")

			return list(map(os.path.basename, built_pkgs))

		except KeyboardInterrupt:
			# the ^C is annoying
			print("")
			return None

		finally:
			if not sandbox_keep:
				msg.log(f"Deleting remote build folder")
				self.run(f"rm -rf {tmpdir}")
			else:
				msg.log(f"Keeping remote build folder: {msg.PINK}{tmpdir}{msg.ALL_OFF}")
