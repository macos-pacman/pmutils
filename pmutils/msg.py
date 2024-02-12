#!/usr/bin/env python
# Copyright (c) 2023, zhiayang
# SPDX-License-Identifier: Apache-2.0

import sys
from typing import *

_indentation: int = 0
GREEN = "\x1b[92;1m" if sys.stdout.isatty() else ""
BLUE = "\x1b[94;1m" if sys.stdout.isatty() else ""
YELLOW = "\x1b[93;1m" if sys.stdout.isatty() else ""
RED = "\x1b[91;1m" if sys.stdout.isatty() else ""
PINK = "\x1b[95;1m" if sys.stdout.isatty() else ""
GREY = "\x1b[90;1m" if sys.stdout.isatty() else ""
WHITE = "\x1b[97;1m" if sys.stdout.isatty() else ""
BOLD = "\x1b[1m" if sys.stdout.isatty() else ""
UNCOLOUR = "\x1b[0m\x1b[1m" if sys.stdout.isatty() else ""
ALL_OFF = "\x1b[0m" if sys.stdout.isatty() else ""


def green(s: str):
	return f"{GREEN}{s}{ALL_OFF}"


def blue(s: str):
	return f"{BLUE}{s}{ALL_OFF}"


def yellow(s: str):
	return f"{YELLOW}{s}{ALL_OFF}"


def pink(s: str):
	return f"{PINK}{s}{ALL_OFF}"


def red(s: str):
	return f"{RED}{s}{ALL_OFF}"


def white(s: str):
	return f"{WHITE}{s}{ALL_OFF}"


def bold(s: str):
	return f"{BOLD}{s}{ALL_OFF}"


def slog(msg: str) -> str:
	return f"{green('==>')} {bold(msg)}"


def slog2(msg: str):
	return f"{blue('  ->')} {bold(msg)}"


def slog3(msg: str):
	return f"{pink('    +')} {bold(msg)}"


def swarn2(msg: str):
	return f"{yellow('  -> WARNING:')} {bold(msg)}"


def swarn(msg: str):
	return f"{yellow('==> WARNING:')} {bold(msg)}"


def serror(msg: str):
	return f"{red('==> ERROR:')} {bold(msg)}"


def serror2(msg: str):
	return f"{red('  -> ERROR:')} {bold(msg)}"


def log(msg: str, end: str = '\n'):
	print(slog(msg), flush=True, end=end)


def log2(msg: str, end: str = '\n'):
	print(slog2(msg), flush=True, end=end)


def log3(msg: str, end: str = '\n'):
	print(slog3(msg), flush=True, end=end)


def warn(msg: str, end: str = '\n'):
	print(swarn(msg), flush=True, end=end, file=sys.stderr)


def warn2(msg: str, end: str = '\n'):
	print(swarn2(msg), flush=True, file=sys.stderr)


def error(msg: str, end: str = '\n'):
	print(serror(msg), end=end, flush=True, file=sys.stderr)


def error2(msg: str, end: str = '\n'):
	print(serror2(msg), flush=True, file=sys.stderr)


def error_and_exit(msg: str) -> NoReturn:
	error(msg)
	sys.exit(1)


def p(msg: str, end: str = '\n'):
	global _indentation
	print(2 * _indentation * ' ' + msg, end=end, flush=True)


def indent():
	global _indentation
	_indentation += 1


def dedent():
	global _indentation
	_indentation -= 1


class Indent:
	def __init__(self):
		pass

	def __enter__(self):
		indent()

	def __exit__(self, *_: list[Any]):
		dedent()
