// common.h
// Copyright (c) 2024, zhiayang
// SPDX-License-Identifier: Apache-2.0

#pragma once

#import <Foundation/Foundation.h>

#include <cstddef>
#include <string>

#include "zpr.h"

struct CreationSettings
{
	size_t cpu_count;
	size_t ram_size;
	size_t disk_size;
};


template <typename... Args>
[[noreturn]] static void error_and_exit(const char* fmt, Args&&... args)
{
	zpr::fprintln(stderr, "[error] {}", zpr::fwd(fmt, static_cast<Args&&>(args)...));
	std::exit(1);
}

static size_t string_to_number(const char* s)
{
	try
	{
		size_t num = 0;
		auto ret = std::stoul(s, &num);
		if(num != strlen(s))
			throw 1;

		return ret;
	}
	catch(...)
	{
		error_and_exit("Invalid number '{}'", s);
	}
}

static NSString* ns_string(const std::string& s)
{
	return [NSString stringWithUTF8String:s.c_str()];
}

static NSURL* ns_url(const std::string& s)
{
	return [NSURL fileURLWithPath:ns_string(s)];
}

namespace zpr
{
	template <>
	struct print_formatter<NSString*>
	{
		template <typename Cb>
		void print(NSString* str, Cb&& cb, format_args args)
		{
			detail::print_string(static_cast<Cb&&>(cb), str.UTF8String,
			    [str lengthOfBytesUsingEncoding:NSUTF8StringEncoding], std::move(args));
		}
	};

	template <>
	struct print_formatter<NSURL*>
	{
		template <typename Cb>
		void print(NSURL* url, Cb&& cb, format_args args)
		{
			auto str = url.absoluteString;
			detail::print_string(static_cast<Cb&&>(cb), str.UTF8String,
			    [str lengthOfBytesUsingEncoding:NSUTF8StringEncoding], std::move(args));
		}
	};
}
