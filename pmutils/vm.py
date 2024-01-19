#!/usr/bin/env python3
# Copyright (c) 2024, zhiayang
# SPDX-License-Identifier: Apache-2.0

import os
import re
import time
import json
import shutil
import signal
import hashlib
import tempfile
import functools
import subprocess
import requests as req
import tqdm.auto as tqdm

from typing import *
from pmutils import msg
from pmutils.config import config

_vmhelper_path: str = ""

PREFIX = "/opt/pacman"


def get_vmhelper_path() -> Optional[str]:
	global _vmhelper_path
	if (x := shutil.which("pmutils-vmhelper")) is not None:
		_vmhelper_path = x
		return x

	return None


def get_mac_model_id() -> str:
	p = subprocess.run(["system_profiler", "-json", "SPHardwareDataType"], capture_output=True, text=True)
	if p.returncode != 0:
		msg.warn(f"Could not get Mac model id!")
		return "Macmini9,1"

	return json.loads(p.stdout)["SPHardwareDataType"][0]["machine_model"]


def _clean_mac_addr(mac: str) -> str:
	return ':'.join(map(lambda x: (x if len(x) == 2 else f"0{x}"), mac.split(':')))


def _wait_for_ok():
	while True:
		if input().lower() != 'ok':
			msg.warn2("Expected 'ok' -- retrying")
			continue
		return


def _download_bar(total: int) -> Any:
	return tqdm.tqdm(
	    desc=f"{msg.blue('  ->')} {msg.bold('Downloading')}",
	    unit=f"iB",
	    total=total,
	    unit_scale=True,
	    unit_divisor=1024,
	    dynamic_ncols=True,
	    miniters=1,
	    ascii=' =',
	    bar_format="{desc:>10}: [{bar}] ({n_fmt:>3}/{total_fmt:>3} [{percentage:>3.0f}%], {rate_fmt}{postfix})",
	)


def stream_process_output(process: subprocess.Popen[bytes]) -> int:
	assert process.stdout is not None
	while True:
		while (c := process.stdout.read(1)):
			print(c.decode(), end='', flush=True)

		if (rc := process.poll()) is None:
			time.sleep(0.1)
		else:
			return rc


SSH_OPTIONS = ["-o", "LogLevel=QUIET", "-o", "UserKnownHostsFile=/dev/null", "-o", "StrictHostKeyChecking=no"]


class VMSandBox:
	bundle: str
	mac_addr: str
	vmhelper: subprocess.Popen[bytes]
	ip: Optional[str]
	user: str
	stopped: bool = False

	def __init__(self, vmhelper: subprocess.Popen[bytes], bundle_path: str, mac_addr: str):
		# most of the things are handled by the vmhelper, nothing much for us to do
		self.bundle = bundle_path
		self.mac_addr = mac_addr
		self.vmhelper = vmhelper
		self.ip = None
		self.user = config().sandbox.username

	def stop(self, wait: bool = True):
		msg.log("Stopping VM...")
		if self.ip is not None:
			self.send_command(f"sudo -n shutdown -h now")
			time.sleep(2)

		self.vmhelper.send_signal(signal.SIGINT)
		if wait:
			self.wait()

		self.stopped = True

	def wait(self):
		self.vmhelper.wait()

	@classmethod
	def start(cls, bundle_path: str, gui: bool) -> Optional[Self]:
		if (vmhp := get_vmhelper_path()) is None:
			msg.error(f"Could not find `pmutils-vmhelper` in $PATH")
			return None

		if not os.path.exists(bundle_path) or not os.path.isdir(bundle_path):
			msg.error(f"Sandbox bundle path {bundle_path} does not exist")
			return None

		# load the json and extract the mac address
		cfg = json.loads(open(os.path.join(bundle_path, "config.json"), "r").read())
		mac_address = _clean_mac_addr(cfg["mac_address"])

		vmhelper = subprocess.Popen(
		    [
		        vmhp,
		        ("rungui" if gui else "run"),
		        bundle_path,
		    ],
		    stdin=subprocess.DEVNULL,
		    start_new_session=True,
		)

		msg.log(f"VM started")
		return cls(vmhelper, bundle_path, mac_address)

	@classmethod
	def restore(cls, bundle_path: str, ipsw_path: str) -> Optional[Self]:
		if (vmhp := get_vmhelper_path()) is None:
			msg.error(f"Could not find `pmutils-vmhelper` in $PATH")
			return None

		msg.log(f"Starting vmhelper...")
		msg.log(f"Restoring VM with IPSW")

		sb_config = config().sandbox
		subprocess.Popen([
		    vmhp,
		    "create",
		    bundle_path,
		    ipsw_path,
		    str(sb_config.cpus),
		    str(sb_config.ram),
		    str(60 * 1024 * 1024 * 1024)     # 60gb disk
		]).wait()

		msg.log(f"INSTRUCTIONS FOR USE:")
		msg.log2(f"Proceed through macOS setup")
		msg.log2(f"Create a user account, and set a password")
		msg.log3(f"The username should be '{sb_config.username}'")
		msg.log2(f"If desired, update macOS (within the same major version) after installation finishes")
		msg.log2(f"Enable 'Remote Login' in System Setings")

		msg.log(f"This might take a while, and requires ~60GiB of disk space.")
		msg.log(f"Proceed? [Y/n]")
		if len(resp := input()) > 0 and not resp.lower().startswith('y'):
			msg.log("Aborting!")
			return None

		if (vz := cls.start(bundle_path, gui=True)) is None:
			return None

		# wait till the user says we can communicate
		while True:
			# no way you can blast through this in < 10 seconds
			time.sleep(10)

			msg.log(f"Once Setup is done and 'Remote Login' is enabled, type 'ok': ", end='')
			if input().lower() != 'ok':
				msg.warn2("Expected 'ok' -- retrying")
				continue

			if vz.get_ip() is None:
				msg.warn2(f"Could not get IP, please retry!")
			else:
				break

		# ok, we should be able to find the thing now.
		if not vz.bootstrap():
			vz.stop()

		return None

	def bootstrap(self, ip_retries: int = 10) -> bool:
		while (self.get_ip() is None):
			msg.error(f"Could not resolve VM IP, ", end='')
			if ip_retries > 0:
				print(msg.bold("retrying..."))
				time.sleep(1)
				ip_retries -= 1
				continue
			else:
				print(msg.bold("giving up"))
				return False

		msg.log(f"Bootstrapping the VM...")

		cmd = functools.partial(self.send_command, bootstrapping=True)

		# check if a key already exists.
		key_path = os.path.join(self.bundle, "..", "id_ed25519")
		if not os.path.exists(key_path) or not os.path.exists(f"{key_path}.pub"):
			msg.log2(f"Generating SSH key")

			# note: no passphrase
			if subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", key_path, "-N", ""],
			                  stdout=subprocess.DEVNULL).returncode != 0:
				msg.error2(f"Failed to generate SSH key")
				return False

		msg.log2(f"Will now attempt to SSH into the VM")
		msg.log2(f"Please provide your password if prompted by SSH")

		if subprocess.run(["ssh-copy-id", "-i", key_path, *SSH_OPTIONS, f"{self.user}@{self.ip}"]).returncode != 0:
			msg.error2(f"Failed to copy SSH key to VM")
			return False

		# only need to edit sudoers if we can't already sudo without a password
		if cmd("sudo -n true")[1] != 0:
			msg.log(f"Editing /etc/sudoers; enter your password when prompted")
			cmd(f"echo '{self.user} ALL=(ALL) NOPASSWD: ALL' | sudo tee -a /etc/sudoers")

		msg.log(f"Setting hostname to 'pacman'")
		cmd(f"sudo systemsetup -setcomputername 'pacman'")

		msg.log(f"Ensuring timezones are the same")
		host_tz = '/'.join(os.path.realpath('/etc/localtime').split('/')[-2:])
		cmd(f"sudo systemsetup -settimezone '{host_tz}'")

		if cmd("xcode-select -p", capture=True)[1] != 0:
			msg.log(f"Installing Xcode CLT")
			msg.log2(f"Looking for software updates...")

			flag = "/tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress"
			cmd(f"sudo touch {flag}")

			prod = cmd(
			    r"""softwareupdate -l                    |
			    	grep -B 1 -E 'Command Line Tools'    |
			    	awk -F'*' '/^ *\*/ {print $2}'       |
			    	sed -e 's/^ *Label: //' -e 's/^ *//' |
			    	sort -V | tail -n1
			    	""",
			    capture=True
			)[0].strip()

			msg.log2(f"Found: '{prod}'")

			success = cmd(f"sudo softwareupdate --verbose --install \"{prod}\"")[1] == 0

			if not success or cmd("stat '/Library/Developer/CommandLineTools'", capture=True)[1] != 0:
				msg.error2(f"CLT failed to install, trying a different way...")
				msg.log2(f"Please log in to the desktop and type 'ok' when done: ", end='')
				_wait_for_ok()

				msg.log2(f"Opening a terminal and running `xcode-select --install` the manual way...")
				cmd(
				    "osascript -e 'tell application \"Terminal\" to do script \"sudo xcode-select --install\" in first window'",
				    capture=True
				)

				msg.log2(f"Hopefully that worked; type 'ok' if it did: ", end='')
				_wait_for_ok()

				if cmd("stat '/Library/Developer/CommandLineTools'", capture=True)[1] != 0:
					msg.error2(f"Turns out it didn't work, sorry")
					return False

			cmd(f"sudo rm -f {flag}")

			# ok, it should exist now.
			cmd("sudo xcode-select --switch /Library/Developer/CommandLineTools")
			msg.log("Xcode CLT installed")

		else:
			msg.log(f"Xcode CLT already installed")

		msg.log(f"Bootstrapping Pacman")

		script_url = "https://raw.githubusercontent.com/macos-pacman/core/master/bootstrap/bootstrap.sh"
		if cmd(f"curl -fsSL {script_url} > /tmp/bootstrap.sh")[1] != 0:
			return False

		if cmd(f"yes | /bin/sh /tmp/bootstrap.sh")[1] != 0:
			return False

		msg.log2(f"Re-installing base packages")
		cmd(f"PATH={PREFIX}/usr/bin:$PATH sudo -E {PREFIX}/usr/bin/pacman -S --noconfirm --overwrite '/*' pacman")

		cmd(f"mv {PREFIX}/etc/pacman.conf{{.pacnew,}} || true", capture=True)
		cmd(f"mv {PREFIX}/etc/makepkg.conf{{.pacnew,}} || true", capture=True)

		# ok, now bash should be installed, and everything should work (including the paths)...
		# running `pacman` itself should just work.

		msg.log(f"Done!")
		msg.log(f"Installed packages:")
		self.send_command(f"pacman -Q")

		self.stop()
		return True

	def send_command(self,
	                 cmd: str,
	                 bootstrapping: bool = False,
	                 capture: bool = False,
	                 with_tty: bool = True) -> tuple[str, int]:
		key_path = os.path.join(self.bundle, "..", "id_ed25519")
		p = subprocess.run(
		    [
		        "ssh",
		        *(["-t"] if with_tty else []),
		        *SSH_OPTIONS,
		        "-i",
		        key_path,
		        f"{self.user}@{self.ip}",
		        "--",
		        *([cmd] if bootstrapping else [f". {PREFIX}/etc/profile; {cmd}"])
		    ],
		    capture_output=capture,
		    text=True,
		)

		if capture:
			return (p.stdout, p.returncode)
		else:
			return ("", p.returncode)

	def get_ip(self) -> Optional[str]:
		for line in subprocess.check_output(["arp", "-a"], text=True).splitlines():
			pat = r"\(([A-Fa-f0-9\.:]+)\) at ((?:[0-9A-Fa-f]{1,2}:){5}[0-9A-Fa-f]{1,2})"
			if (m := re.search(pat, line)) is not None:
				if len(m.groups()) != 2:
					continue

				if _clean_mac_addr(m.groups()[1]) == self.mac_addr:
					msg.log(f"VM IP address: {m.groups()[0]}")
					self.ip = m.groups()[0]
					break

		return self.ip


def create_new_sandbox_with_downloaded_ipsw(bundle_path: str) -> Optional[VMSandBox]:
	if (buildid := config().sandbox.macos_build) is None:
		msg.error(f"`macos-build` must be specified in `sandbox` in config.toml")
		return None

	msg.log(f"Creating new sandbox VM at {bundle_path}")
	msg.log(f"Downloading restore IPSW")

	# download the IPSW
	model = get_mac_model_id()
	resp = req.get(f"https://api.ipsw.me/v4/ipsw/{model}/{buildid}")
	if resp.status_code != 200:
		msg.error2(f"Could not get IPSW download link: {resp.text}")
		return None

	resp_json = resp.json()
	if not resp_json["signed"]:
		msg.error2(f"IPSW for build {buildid} is not signed")
		return None

	ipsw_sha256: str = resp_json["sha256sum"]
	ipsw_filesize: int = resp_json["filesize"]
	ipsw_url: str = resp_json["url"]

	dlreq = req.get(ipsw_url, stream=True)
	total_size = int(dlreq.headers.get("Content-Length", 0))
	if total_size != ipsw_filesize:
		msg.warn2(f"Reported filesizes mismatch: ipsw.me: {ipsw_filesize}, Apple: {total_size}")

	# hasher = hashlib.sha256()
	# with tempfile.NamedTemporaryFile("wb", suffix=".ipsw") as ipsw:
	# 	with _download_bar(total_size) as bar:
	# 		for data in dlreq.iter_content(chunk_size=1024 * 1024):
	# 			size = ipsw.write(data)
	# 			hasher.update(data)

	# 			bar.update(size)

	# 	if (sha := hasher.hexdigest()) != ipsw_sha256:
	# 		msg.warn2(f"Checksum mismatch! Expected: {ipsw_sha256}")
	# 		msg.warn2(f"                     Actual: {sha}")

	# 	# make the vm with the file.
	# 	return VMSandBox.create(bundle_path=bundle_path, ipsw_path=ipsw.name)

	return VMSandBox.restore(bundle_path=bundle_path, ipsw_path="/tmp/macos13.6.ipsw")


def load_or_create_sandbox(gui: bool,
                           restore: bool,
                           bootstrap: bool,
                           ipsw_path: Optional[str] = None) -> Optional[VMSandBox]:
	if get_vmhelper_path() is None:
		msg.error(f"Could not find `pmutils-vmhelper` in $PATH")
		return None

	if (sandbox_path := config().sandbox.path) is None:
		msg.error_and_exit(f"Sandbox path not configured! Set `sandbox.path` in your config.toml")

	# check if there is a `vm.bundle` in there
	bundle_path = os.path.join(sandbox_path, "vm.bundle")
	if not os.path.exists(bundle_path) or restore:
		# make the sandbox path, just in case
		try:
			os.makedirs(sandbox_path, exist_ok=True)
		except PermissionError as _:
			msg.error_and_exit(f"Could not create sandbox directory (check permissions?)")

		if ipsw_path is None:
			return create_new_sandbox_with_downloaded_ipsw(os.path.join(sandbox_path, "vm.bundle"))
		else:
			return VMSandBox.restore(bundle_path=bundle_path, ipsw_path=ipsw_path)

	elif not os.path.isdir(bundle_path):
		msg.error_and_exit(f"`{sandbox_path}/vm.bundle` exists but is not a directory!")

	# ok, we can load it.
	if (vz := VMSandBox.start(bundle_path=bundle_path, gui=(gui or bootstrap))) is None:
		return None

	if bootstrap:
		# give some time for the VM to start
		time.sleep(10)
		vz.bootstrap()

	return vz
