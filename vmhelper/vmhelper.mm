// vmhelper.mm
// Copyright (c) 2024, zhiayang
// SPDX-License-Identifier: Apache-2.0

#import "App.h"
#import "VirtualMachine.h"

#include <cstdio>
#include <cerrno>
#include <cstdlib>
#include <cstring>

#include <tuple>
#include <string>
#include <utility>
#include <filesystem>

#include "zpr.h"

static void print_usage(char* argv0)
{
	zpr::println("Usage: {} <COMMAND> [args...]", argv0);
	zpr::println("Supported commands:");
	zpr::println("  create <PATH> <OS BUILD> <CPU> <RAM> <DISK>");
	zpr::println("  run    <PATH>");
}

int main(int argc, char** argv)
{
	if(argc < 2)
	{
		print_usage(argv[0]);
		return 0;
	}

	[NSApplication sharedApplication];

	auto command = std::string(argv[1]);
	argv += 2;
	argc -= 2;

	auto app = [App alloc];
	if(command == "create")
	{
		if(argc != 5)
			error_and_exit("Expected arguments for create: <PATH> <IPSW_PATH> <CPU> <RAM> <DISK>");

		auto bundle_path = std::string(argv[0]);
		auto ipsw_path = std::string(argv[1]);
		auto cpu_count = string_to_number(argv[2]);
		auto ram_size = string_to_number(argv[3]);
		auto disk_size = string_to_number(argv[4]);

		auto cs = CreationSettings {
			.cpu_count = cpu_count,
			.ram_size = ram_size,
			.disk_size = disk_size,
		};

		app = [app
		       initFromNewBundle:ns_url(bundle_path)
		    withCreationSettings:std::move(cs)
		            restoreImage:ns_url(ipsw_path)];
	}
	else if(command == "restore")
	{
		if(argc != 2)
			error_and_exit("Expected arguments for restore: <PATH> <IPSW_PATH>");

		auto bundle_path = std::string(argv[0]);
		auto ipsw_path = std::string(argv[1]);

		app = [app initFromBundle:ns_url(bundle_path) withRestoreImage:ns_url(ipsw_path)];
	}
	else if(command == "run" || command == "rungui")
	{
		if(argc != 1)
			error_and_exit("Expected arguments for run: <PATH>");

		auto bundle_path = std::string(argv[0]);
		app = [app initForRunningFromBundle:ns_url(bundle_path) withGUI:(command == "rungui")];
	}
	else
	{
		zpr::println("Invalid command '{}'", command);
		print_usage(argv[0]);
		return 1;
	}

	// set up signal handler
	signal(SIGINT, [](int) {
		[(App*) NSApp.delegate stopVM];
		[NSApp terminate:nil];
	});

	// run the app
	NSApp.delegate = app;
	[NSApp run];
}
