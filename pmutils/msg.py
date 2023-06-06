#!/usr/bin/env python
# Copyright (c) 2023, zhiayang
# SPDX-License-Identifier: Apache-2.0

import sys

def log(msg: str):
	print(f"\x1b[90;1m[log]\x1b[0m {msg}")

def warn(msg: str):
	print(f"\x1b[93;1m[wrn]\x1b[0m \x1b[1m{msg}\x1b[0m", file=sys.stderr)

def error(msg: str):
	print(f"\x1b[91;1m[err]\x1b[0m \x1b[1m{msg}\x1b[0m", file=sys.stderr)

def error_and_exit(msg: str):
	error(msg)
	sys.exit(1)
