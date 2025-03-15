#!/usr/bin/env python3
# Copyright (c) 2024, yuki
# SPDX-License-Identifier: Apache-2.0

import os
import re
import time
import json
import shutil
import signal
import atexit
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

PREFIX = os.path.normpath(f"{os.path.dirname(str(shutil.which('pacman')))}/../../")
DEFAULT_PASSWORD = "password"


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


def clean_mac_addr(mac: str) -> str:
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
	    unit=f"B",
	    total=total,
	    unit_scale=True,
	    unit_divisor=1024,
	    dynamic_ncols=True,
	    miniters=1,
	    ascii=' ▬',
	    bar_format="{desc:>10}: " + f"{msg.blue('[')}{{bar}}{msg.blue(']')}"
	    + " ({n_fmt:>3}/{total_fmt:>3} [{percentage:>3.0f}%], {rate_fmt}{postfix})",
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


def get_vmhelper_path_or_die():
	if (vmhp := get_vmhelper_path()) is not None:
		return vmhp
	else:
		msg.error_and_exit(f"Could not find `pmutils-vmhelper` in $PATH")


class VMSandBox:
	bundle: str
	mac_addr: str
	vmhelper: subprocess.Popen[bytes]
	ip: Optional[str]
	user: str
	stopped: bool = False
	owned: bool = False

	def __init__(self, vmhelper: subprocess.Popen[bytes], bundle_path: str, mac_addr: str, owned: bool):
		# most of the things are handled by the vmhelper, nothing much for us to do
		self.bundle = bundle_path
		self.mac_addr = clean_mac_addr(mac_addr)
		self.vmhelper = vmhelper
		self.ip = None
		self.owned = owned
		self.user = config().sandbox.username
		atexit.register(lambda: self.__del__())

	def __del__(self):
		if self.owned:
			self.vmhelper.send_signal(signal.SIGINT)

	def stop(self, wait: bool = True):
		msg.log("Stopping VM...")
		if self.ip is not None and (not self.vmhelper.poll()):
			# every time we stop the vm, update the version number.
			with open(os.path.join(self.bundle, "config.json"), "r") as c:
				cfg = json.load(c)

			os_version = self.send_command(f"sw_vers -productVersion", capture=True)[0]
			os_arch = self.send_command(f"uname -m", capture=True)[0]

			cfg["os_ver"] = os_version.strip()
			cfg["arch"] = os_arch.strip()
			with open(os.path.join(self.bundle, "config.json"), "w") as c:
				json.dump(cfg, c)

			self.send_command(f"sudo -n shutdown -h now")
			time.sleep(5)

		self.vmhelper.send_signal(signal.SIGINT)
		if wait:
			self.wait()

		self.stopped = True

	def wait(self):
		self.vmhelper.wait()

	def is_owned(self) -> bool:
		return self.owned

	@classmethod
	def get_mac_address(cls, bundle_path: str) -> Optional[str]:
		# load the json and extract the mac address
		cfg_path = os.path.join(bundle_path, "config.json")
		if not os.path.exists(cfg_path):
			msg.error(f"Config JSON path '{cfg_path}' does not exist in the bundle!")
			return None

		cfg = json.loads(open(os.path.join(bundle_path, "config.json"), "r").read())
		return clean_mac_addr(cfg["mac_address"])

	@classmethod
	def start(cls, bundle_path: str, gui: bool) -> Optional[Self]:
		if not os.path.exists(bundle_path) or not os.path.isdir(bundle_path):
			msg.error(f"Sandbox bundle path {bundle_path} does not exist")
			return None

		mac_address = cls.get_mac_address(bundle_path)
		if mac_address is None:
			return None

		vmhelper = subprocess.Popen(
		    [
		        get_vmhelper_path_or_die(),
		        ("rungui" if gui else "run"),
		        bundle_path,
		    ],
		    stdin=subprocess.DEVNULL,
		    start_new_session=True,
		)

		msg.log(f"VM started")
		return cls(vmhelper, bundle_path, mac_address, owned=True)

	@classmethod
	def restore(cls, bundle_path: str, ipsw_path: str) -> Optional[Self]:
		msg.log(f"Starting vmhelper...")
		msg.log(f"Restoring VM with IPSW")

		sb_config = config().sandbox
		rc = subprocess.Popen([
		    get_vmhelper_path_or_die(),
		    "create",
		    bundle_path,
		    ipsw_path,
		    str(sb_config.cpus),
		    str(sb_config.ram),
		    str(sb_config.disk)
		]).wait()

		if rc != 0:
			msg.error(f"VM failed to restore!")
			return None

		msg.log(f"INSTRUCTIONS FOR USE:")
		msg.log2(f"macOS setup should be automated, but it's not foolproof")
		msg.log2(f"If it does not succeed, please set it up manually:")
		msg.log3(f"The username should be '{sb_config.username}'")
		msg.log3(f"          and password '{DEFAULT_PASSWORD}'")
		msg.log3(f"Enable 'Remote Login' in System Setings")

		msg.log2(f"If desired, update macOS (within the same major version) after installation finishes")

		msg.log(f"This might take a while, and requires ~100GiB of disk space.")
		msg.log(f"Proceed? [Y/n]")
		if len(resp := input()) > 0 and not resp.lower().startswith('y'):
			msg.log("Aborting!")
			return None

		if not cls.setup(bundle_path, sb_config.username, "password"):
			return None

		# now run it again
		vz = cls.start(bundle_path, gui=True)
		if not vz:
			return None

		# wait till we can find the guy
		while True:
			time.sleep(1)
			if vz.get_ip() is None:
				msg.warn2(f"Could not get IP, please retry!")
			else:
				break

		# ok, we should be able to find the thing now.
		if not vz.bootstrap():
			vz.stop()

		return vz

	@classmethod
	def setup(cls, bundle_path: str, username: str, password: str) -> bool:
		rc = subprocess.Popen(
		    [
		        get_vmhelper_path_or_die(),
		        "setup",
		        bundle_path,
		        username,
		        password,
		    ],
		    stdin=subprocess.DEVNULL,
		    start_new_session=True,
		).wait()
		return (rc == 0)

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

		if subprocess.run(["sshpass", "ssh-copy-id", "-i", key_path, *SSH_OPTIONS, f"{self.user}@{self.ip}"],
		                  input=DEFAULT_PASSWORD,
		                  text=True).returncode != 0:
			msg.error2(f"Failed to copy SSH key to VM")
			return False

		# only need to edit sudoers if we can't already sudo without a password
		if cmd("sudo -n true")[1] != 0:
			msg.log(f"Editing /etc/sudoers; enter your password when prompted")
			cmd(
			    f"sudo -S -- bash -c \"echo '{self.user} ALL=(ALL) NOPASSWD: ALL' | tee -a /etc/sudoers\"",
			    with_input=DEFAULT_PASSWORD
			)

		msg.log(f"Setting hostname to 'pacman'")
		cmd(f"sudo systemsetup -setcomputername 'pacman'")
		cmd(f"sudo scutil --set LocalHostName 'pacman'")
		cmd(f"sudo scutil --set ComputerName 'pacman'")
		cmd(f"sudo scutil --set HostName 'pacman'")

		msg.log(f"Ensuring timezones are the same")
		host_tz = '/'.join(os.path.realpath('/etc/localtime').split('/')[-2:])
		cmd(f"sudo systemsetup -settimezone '{host_tz}'")

		# https://github.com/cirruslabs/macos-image-templates/blob/master/templates/vanilla-ventura.pkr.hcl
		msg.log(f"Disabling screensavers etc.")
		cmd(f"sudo defaults write /Library/Preferences/com.apple.screensaver loginWindowIdleTime 0")
		cmd(f"defaults -currentHost write com.apple.screensaver idleTime 0")
		cmd(f"sudo systemsetup -setdisplaysleep Off")
		cmd(f"sudo systemsetup -setsleep Off")
		cmd(f"sudo systemsetup -setcomputersleep Off")

		cmd(f"defaults write com.apple.Dock autohide -bool true")
		cmd(f"defaults write com.apple.Dock show-recents -bool false")
		cmd(f"defaults write com.apple.Dock minimize-to-application -bool true")
		cmd(f"defaults write com.apple.loginwindow TALLogoutSavesState -bool false")
		cmd(f"defaults write com.apple.loginwindow LoginwindowLaunchesRelaunchApps -bool false")

		if cmd("xcode-select -p", capture=True)[1] != 0:
			msg.log(f"Installing Xcode CLT")
			msg.log2(f"Looking for software updates...")

			flag = "/tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress"
			cmd(f"sudo touch {flag}")

			prod = cmd(
			    r"""softwareupdate -l 2>/dev/null        |
			    	grep -B 1 -E 'Command Line Tools'    |
			    	awk -F'*' '/^ *\*/ {print $2}'       |
			    	sed -e 's/^ *Label: //' -e 's/^ *//' |
			    	sort -V | tail -n1
			    	""",
			    capture=True
			)[0].strip()

			msg.log2(f"Found: '{prod}'")

			success = cmd(f"sudo softwareupdate --verbose --install \"{prod}\" 2>/dev/null")[1] == 0

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

		msg.log(f"Enabling Developer Mode")
		cmd(f"sudo DevToolsSecurity -enable")
		cmd(f"sudo security authorizationdb write system.privilege.taskport allow")

		msg.log(f"Bootstrapping Pacman")

		script_url = "https://raw.githubusercontent.com/macos-pacman/core/master/bootstrap/bootstrap.sh"
		if cmd(f"curl -fsSL {script_url} > /tmp/bootstrap.sh")[1] != 0:
			return False

		if cmd(f"yes | /bin/sh /tmp/bootstrap.sh")[1] != 0:
			return False

		cmd(f"mv -f {PREFIX}/etc/pacman.conf{{.pacnew,}} || true", capture=True)
		cmd(f"mv -f {PREFIX}/etc/makepkg.conf{{.pacnew,}} || true", capture=True)

		# set the packager
		cmd(f"echo 'PACKAGER=\"macos-pacman-bot <bot@macos-pacman>\"' > ~/.makepkg.conf")

		# ok, now bash should be installed, and everything should work (including the paths)...
		# running `pacman` itself should just work.

		msg.log(f"Done!")

		msg.log(f"Installing system toolchain")
		cmd(f"sudo pacman -S --noconfirm base-devel")

		msg.log(f"Additionally installing GNU nano because pico is unusable")
		cmd(f"sudo pacman -S --noconfirm nano")

		msg.log(f"Installed packages:")
		self.send_command(f"pacman -Q")

		self.stop()
		return True

	def send_command(
	    self,
	    cmd: str,
	    bootstrapping: bool = False,
	    capture: bool = False,
	    with_tty: bool = True,
	    with_input: Optional[str] = None
	) -> tuple[str, int]:
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
		    input=with_input,
		)

		if capture:
			return (p.stdout, p.returncode)
		else:
			return ("", p.returncode)

	@staticmethod
	def get_ip_from_mac(mac: str) -> Optional[str]:
		for line in subprocess.check_output(["arp", "-a"], text=True).splitlines():
			pat = r"\(([A-Fa-f0-9\.:]+)\) at ((?:[0-9A-Fa-f]{1,2}:){5}[0-9A-Fa-f]{1,2})"
			if (m := re.search(pat, line)) is not None:
				if len(m.groups()) != 2:
					continue

				if clean_mac_addr(m.groups()[1]) == mac:
					msg.log(f"VM IP address: {msg.PINK}{m.groups()[0]}{msg.ALL_OFF}")
					return m.groups()[0]

		return None

	def get_ip(self) -> Optional[str]:
		self.ip = self.get_ip_from_mac(self.mac_addr)
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

	hasher = hashlib.sha256()
	with tempfile.NamedTemporaryFile("wb", suffix=".ipsw") as ipsw:
		with _download_bar(total_size) as bar:
			for data in dlreq.iter_content(chunk_size=1024 * 1024):
				size = ipsw.write(data)
				hasher.update(data)

				bar.update(size)

		if (sha := hasher.hexdigest()) != ipsw_sha256:
			msg.warn2(f"Checksum mismatch! Expected: {ipsw_sha256}")
			msg.warn2(f"                     Actual: {sha}")

		# make the vm with the file.
		return VMSandBox.restore(bundle_path=bundle_path, ipsw_path=ipsw.name)


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

	# check if the rm is running
	if os.path.exists(os.path.join(bundle_path, ".vm-running")):
		if gui or restore:
			msg.error_and_exit(f"Cannot restore or open GUI on a running VM")

		msg.log("Connecting to existing VM")
		mac_addr = VMSandBox.get_mac_address(bundle_path)

		if mac_addr is None:
			msg.error_and_exit(f"Could not get MAC address!")

		return VMSandBox(subprocess.Popen(["true"]), bundle_path, mac_addr, owned=False)

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
