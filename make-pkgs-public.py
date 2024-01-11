#!/usr/bin/env python

import os
import time
import tomllib
import requests as req
from selenium import webdriver
from selenium.webdriver.common.by import By

f = tomllib.load(open("config.toml", "rb"))
token = f["registry"]["token"]

ORG = "macos-pacman"

package_urls: list[tuple[str, str]] = []

# first get a list of packages
page = 1
while True:
	pkgs = req.get(f"https://api.github.com/orgs/{ORG}/packages", params={
		"package_type": "container",
		"visibility": "private",
		"page": page,
		"per_page": 100,
	}, headers={
		"Accept": "application/vnd.github+json",
		"X-GitHub-Api-Version": "2022-11-28",
		"Authorization": f"Bearer {token}"
	}).json()

	if len(pkgs) == 0:
		break

	print(f"Page {page}: {len(pkgs)} package{'' if len(pkgs) == 1 else 's'}")
	for pkg in pkgs:
		package_urls.append((pkg["html_url"], pkg["name"]))

	page += 1


# pyright: reportUnknownMemberType=false

driver = webdriver.Safari()
# driver.maximize_window()
driver.implicitly_wait(0.3)

driver.get("https://github.com/login")

driver.find_element(by=By.ID, value="login_field").clear()
driver.find_element(by=By.ID, value="login_field").send_keys("macos-pacman-bot")
driver.find_element(by=By.ID, value="password").clear()
driver.find_element(by=By.ID, value="password").send_keys(open("pw", "r").read().strip())

time.sleep(1.0)

driver.find_element(by=By.NAME, value="commit").click()
time.sleep(2.0)

print(f"Found {len(package_urls)} packages")

import random
random.shuffle(package_urls)

successed: dict[str, bool] = dict()

i = 0
for pkg_url, pkg_name in package_urls:
	i += 1
	try:
	# for _, pkg_name in [("", "core/haskell-appar")]:
		# print("A");
		print(f"[{i}/{len(package_urls)}]")

		driver.get(f"https://github.com/orgs/{ORG}/packages/container/{pkg_name.replace('/', '%2F')}/settings")
		time.sleep(1.0)

		# print("B");
		box = driver.find_element(by=By.CLASS_NAME, value="Box--danger")
		assert box is not None

		# print("C");
		vis = box.find_element(by=By.CSS_SELECTOR, value="ul > li > p").text
		if "public" in vis:
			continue

		# print("D");
		btn = box.find_element(by=By.CSS_SELECTOR, value="summary.btn-danger")
		assert btn is not None
		driver.execute_script("arguments[0].scrollIntoView();", btn)

		# print("E");
		driver.execute_script("arguments[0].click();", btn)
		time.sleep(0.3)

		# print("F");
		driver.execute_script("document.getElementById('visibility_public').click();", btn)

		# print("G");
		driver.find_element(by=By.NAME, value="verify").send_keys(pkg_name)
		time.sleep(0.5)

		# print("H");
		clicker = "document.getElementById('visibility_public').parentElement.parentElement." + \
			"parentElement.getElementsByTagName('button')[0].click();"
		driver.execute_script(clicker)

		time.sleep(0.5)
		successed[pkg_name] = True

	except KeyboardInterrupt as ki:
		print("breaking")
		successed[pkg_name] = False
		break

	except Exception as e:
		print(e)
		successed[pkg_name] = False
		pass

with open("PACKAGES.tmp", "w") as ff:
	for _, pkg_name in package_urls:
		if (pkg_name in successed) and successed[pkg_name]:
			ff.write(f"{pkg_name}\n")

# input()
