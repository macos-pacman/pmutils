// App.h
// Copyright (c) 2024, yuki
// SPDX-License-Identifier: Apache-2.0

#pragma once

#import <Foundation/Foundation.h>
#import <Virtualization/Virtualization.h>
#import "VirtualMachine.h"

#include "common.h"

NS_ASSUME_NONNULL_BEGIN

enum class RunMode
{
	CreateAndRestore,
	Restore,
	Run,
	RunGUI,
};

@interface App : NSObject<NSApplicationDelegate, NSWindowDelegate, VZVirtualMachineDelegate> {
@public
	bool automaticSetup;
	NSString* setupUsername;
	NSString* setupPassword;

@private
	CreationSettings creationSettings;
	VirtualMachine* vm;
	RunMode runMode;
	NSURL* bundleURL;
	NSURL* _Nullable restoreImage;

	// for gui stuff
	NSWindow* _Nullable window;
	VZVirtualMachineView* _Nullable vmView;
}

- (instancetype)initForRunningFromBundle:(NSURL*)bundle withGUI:(bool)gui;
- (instancetype)initFromBundle:(NSURL*)bundle withRestoreImage:(NSURL*)restoreImage;

- (instancetype)
       initFromNewBundle:(NSURL*)bundle
    withCreationSettings:(CreationSettings)settings
            restoreImage:(NSURL*)restoreImage;

- (void)stopVM;

@end

NS_ASSUME_NONNULL_END
