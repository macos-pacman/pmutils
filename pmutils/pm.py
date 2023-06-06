#!/usr/bin/env python
# Copyright (c) 2023, zhiayang
# SPDX-License-Identifier: Apache-2.0

import os
import sys

from . import msg, config
from .config import User

def main() -> int:
	user = User.from_file()
	token = config.get_token(user)
	assert len(token) > 0

	msg.log("successfully authenticated")


	return 0
