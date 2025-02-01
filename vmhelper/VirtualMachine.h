// VirtualMachine.h
// Copyright (c) 2024, yuki
// SPDX-License-Identifier: Apache-2.0

#pragma once

#include <cstddef>

#import <Foundation/Foundation.h>
#import <Virtualization/Virtualization.h>

NS_ASSUME_NONNULL_BEGIN

@interface VirtualMachine : NSObject<VZVirtualMachineDelegate> {
@public
	NSURL* bundlePath;
	size_t cpuCount;
	size_t ramSize;

@private
	VZMacMachineIdentifier* machineIdentifier;
	VZMacHardwareModel* hardwareModel;
	VZMACAddress* macAddress;
	VZVirtualMachine* vmHandle;
	dispatch_queue_t queue;

	dispatch_semaphore_t stopSemaphore;
}
+ (instancetype)
    createWithBundleURL:(NSURL*)bundle
           restoreImage:(NSURL*)restoreImage
               cpuCount:(size_t)cpus
                ramSize:(size_t)ramSize
               diskSize:(size_t)diskSize;

+ (instancetype)loadFromBundleURL:(NSURL*)bundle;
- (bool)saveToBundleURL:(NSURL*)bundle;

- (void)restoreFromIPSW:(NSURL*)url;

- (void)
    observeValueForKeyPath:(NSString* _Nullable)keyPath
                  ofObject:(id _Nullable)object
                    change:(NSDictionary<NSKeyValueChangeKey, id>* _Nullable)change
                   context:(void* _Nullable)context;

- (VZVirtualMachine*)vm;

- (void)start;
- (void)stop;
- (void)waitForStop;

- (void)guestDidStopVirtualMachine:(VZVirtualMachine*)vm;
- (void)virtualMachine:(VZVirtualMachine*)vm didStopWithError:(NSError*)error;
@end

constexpr const char* DISK_IMAGE_NAME = "disk.img";
constexpr const char* NVRAM_IMAGE_NAME = "nvram.img";
constexpr const char* CONFIG_JSON_NAME = "config.json";

NS_ASSUME_NONNULL_END
