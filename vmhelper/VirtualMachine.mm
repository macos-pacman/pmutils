// VirtualMachine.mm
// Copyright (c) 2024, zhiayang
// SPDX-License-Identifier: Apache-2.0

#include <tuple>

#import "common.h"
#import "VirtualMachine.h"



static VZVirtualMachine* setup_vm(dispatch_queue_t vm_queue,
    NSURL* bundle_path,
    VZMacHardwareModel* hardware_model,
    VZMacMachineIdentifier* machine_identifier,
    VZMACAddress* mac_address,
    size_t cpus,
    size_t rams)
{
	// the bundle path must exist
	if(not [NSFileManager.defaultManager fileExistsAtPath:bundle_path.path])
		error_and_exit("Bundle path '{}' does not exist", bundle_path);

	NSError* error = nil;
	auto disk_image_url = [bundle_path URLByAppendingPathComponent:ns_string(DISK_IMAGE_NAME)];
	auto nvram_image_url = [bundle_path URLByAppendingPathComponent:ns_string(NVRAM_IMAGE_NAME)];

	auto attachment = [[VZDiskImageStorageDeviceAttachment alloc] initWithURL:disk_image_url readOnly:NO error:&error];
	if(error != nil)
		error_and_exit("Failed to create StorageDeviceAttachment for disk image: {}", error.description.UTF8String);

	auto vm_config = [[VZVirtualMachineConfiguration alloc] init];
	auto nvram = [[VZMacAuxiliaryStorage alloc] initWithURL:nvram_image_url];

	auto platform = [[VZMacPlatformConfiguration alloc] init];
	platform.auxiliaryStorage = nvram;

	platform.hardwareModel = hardware_model;
	platform.machineIdentifier = machine_identifier;

	if(not platform.hardwareModel.supported)
		error_and_exit("Hardware model not supported");

	vm_config.platform = platform;
	vm_config.CPUCount = cpus;
	vm_config.memorySize = rams;
	vm_config.bootLoader = [[VZMacOSBootLoader alloc] init];

	zpr::println("[log] Config: cpus={}, ram={}", cpus, rams);

	auto storage = [[VZVirtioBlockDeviceConfiguration alloc] initWithAttachment:attachment];
	auto memory = [[VZVirtioTraditionalMemoryBalloonDeviceConfiguration alloc] init];
	auto network = [[VZNATNetworkDeviceAttachment alloc] init];
	auto keyboard = [[VZUSBKeyboardConfiguration alloc] init];
	auto mouse = [[VZUSBScreenCoordinatePointingDeviceConfiguration alloc] init];
	auto graphics = [[VZMacGraphicsDeviceConfiguration alloc] init];

	graphics.displays = @[
		[[VZMacGraphicsDisplayConfiguration alloc] initWithWidthInPixels:1280 heightInPixels:800 pixelsPerInch:72],
	];

	auto net_dev = [[VZVirtioNetworkDeviceConfiguration alloc] init];

	net_dev.attachment = network;
	net_dev.MACAddress = mac_address;
	zpr::println("[log] MAC address: {}", net_dev.MACAddress.string);

	vm_config.memoryBalloonDevices = @[ memory ];
	vm_config.storageDevices = @[ storage ];
	vm_config.networkDevices = @[ net_dev ];
	vm_config.graphicsDevices = @[ graphics ];
	vm_config.keyboards = @[ keyboard ];
	vm_config.pointingDevices = @[ mouse ];

	// ensure the config is valid
	if([vm_config validateWithError:&error] != YES)
		error_and_exit("VM configuration failed to validate: {}", error.description.UTF8String);

	zpr::println("[log] VM configuration is valid");
	auto vm = [[VZVirtualMachine alloc] initWithConfiguration:vm_config queue:vm_queue];

	return vm;
}


static std::tuple<VZVirtualMachine*, VZMacHardwareModel*, VZMacMachineIdentifier*, VZMACAddress*> create_new_vm(
    dispatch_queue_t vm_queue,
    NSURL* bundle_path,
    NSURL* restore_image_url,
    size_t cpus,
    size_t rams,
    size_t disks)
{
	// the bundle path must not exist
	if([NSFileManager.defaultManager fileExistsAtPath:bundle_path.path])
		error_and_exit("Bundle path '{}' already exists", bundle_path.path.UTF8String);

	NSError* error = nil;
	[NSFileManager.defaultManager
	           createDirectoryAtURL:bundle_path
	    withIntermediateDirectories:YES
	                     attributes:nil
	                          error:&error];
	if(error != nil)
		error_and_exit("Failed to create VM bundle folder: {}", error.description);


	// create the disk image
	const auto disk_image_url = [bundle_path URLByAppendingPathComponent:ns_string(DISK_IMAGE_NAME)];
	const auto nvram_image_url = [bundle_path URLByAppendingPathComponent:ns_string(NVRAM_IMAGE_NAME)];

	if(int fd = open(disk_image_url.path.UTF8String, O_CREAT | O_WRONLY, 0644); fd < 0)
	{
		error_and_exit("Failed to create disk image: {}", strerror(errno));
	}
	else
	{
		if(ftruncate(fd, disks) < 0)
			error_and_exit("Failed to resize disk image: {}", strerror(errno));

		close(fd);
	}

	zpr::println("[log] Created {} byte disk image", disks);

	// load the restore image
	auto sem = dispatch_semaphore_create(0);
	__block VZMacOSRestoreImage* restore_image = nil;

	[VZMacOSRestoreImage
	          loadFileURL:restore_image_url
	    completionHandler:^(VZMacOSRestoreImage* img, NSError* error) {
		    if(img == nil || error != nil)
			    error_and_exit("Failed to load IPSW: {}", error.description.UTF8String);

		    zpr::println("[log] Loaded restore IPSW: {}", img.URL);

		    restore_image = img;
		    dispatch_semaphore_signal(sem);
	    }];

	dispatch_semaphore_wait(sem, DISPATCH_TIME_FOREVER);

	auto image_req = restore_image.mostFeaturefulSupportedConfiguration;
	// auto vm_config = [[VZVirtualMachineConfiguration alloc] init];

	// just make the nvram
	auto nvram = [[VZMacAuxiliaryStorage alloc] //
	    initCreatingStorageAtURL:nvram_image_url
	               hardwareModel:image_req.hardwareModel
	                     options:0
	                       error:&error];

	if(error != nil)
		error_and_exit("Failed to create AuxiliaryStorage: {}", error.description.UTF8String);

	(void) nvram;

	auto mid = [[VZMacMachineIdentifier alloc] init];
	auto hwm = image_req.hardwareModel;
	auto mac = VZMACAddress.randomLocallyAdministeredAddress;
	auto vm = setup_vm(vm_queue, bundle_path, hwm, mid, mac, cpus, rams);

	return { vm, hwm, mid, mac };
}





constexpr NSString* kCpuCount = @"cpu_count";
constexpr NSString* kRamSize = @"ram_size";
constexpr NSString* kMachineIdentifier = @"machine_identifier";
constexpr NSString* kHardwareModel = @"hardware_model";
constexpr NSString* kMacAddress = @"mac_address";

@implementation VirtualMachine
- (instancetype)init
{
	self = [super init];
	self->queue = dispatch_queue_create("vm_queue", DISPATCH_QUEUE_SERIAL);
	self->stopSemaphore = dispatch_semaphore_create(0);
	return self;
}

- (instancetype)
       initFromBundle:(NSURL*)bundle
             cpuCount:(size_t)cpus
              ramSize:(size_t)rams
    machineIdentifier:(VZMacMachineIdentifier*)mid
        hardwareModel:(VZMacHardwareModel*)hw
           macAddress:(VZMACAddress*)macAddr
{
	self = [self init];
	self->cpuCount = cpus;
	self->ramSize = rams;

	self->hardwareModel = hw;
	self->machineIdentifier = mid;
	self->macAddress = macAddr;

	self->vmHandle = setup_vm(self->queue, bundle, self->hardwareModel, self->machineIdentifier, self->macAddress,
	    self->cpuCount, self->ramSize);

	self->vmHandle.delegate = self;
	return self;
}

- (VZVirtualMachine*)vm
{
	return self->vmHandle;
}

- (void)restoreFromIPSW:(NSURL*)ipswURL
{
	auto sem = dispatch_semaphore_create(0);

	dispatch_async(self->queue, ^{
		zpr::println("[log] Restoring VM from {}", ipswURL);

		auto installer = [[VZMacOSInstaller alloc] initWithVirtualMachine:self->vmHandle restoreImageURL:ipswURL];

		[installer.progress
		    addObserver:self
		     forKeyPath:NSStringFromSelector(@selector(fractionCompleted))
		        options:NSKeyValueObservingOptionInitial | NSKeyValueObservingOptionNew
		        context:nil];

		[installer installWithCompletionHandler:^(NSError* error) {
			// extra newline because of the progress
			zpr::println("");
			if(error != nil)
				error_and_exit("Restore failed: {}", error.description);

			zpr::println("[log] Restore completed");
			dispatch_semaphore_signal(sem);
		}];
	});

	dispatch_semaphore_wait(sem, DISPATCH_TIME_FOREVER);
}

- (void)start
{
	dispatch_async(self->queue, ^{
		if(self->vmHandle.state != VZVirtualMachineStateStopped || not self->vmHandle.canStart)
		{
			zpr::println("[log] VM cannot be started at this time");
			return;
		}

		[self->vmHandle startWithCompletionHandler:^(NSError* _Nullable errorOrNil) {
			zpr::println("[log] VM started");
			(void) errorOrNil;
		}];
	});
}

- (void)stop
{
	dispatch_async(self->queue, ^{
		if(not self->vmHandle.canStop)
		{
			zpr::println("[log] VM cannot be stopped at this time");
			return;
		}

		// first, request a stop from the guest
		NSError* error = nil;
		if(not [self->vmHandle requestStopWithError:&error])
		{
			zpr::println("[log] Could not request guest to stop: {}", error.description);
		}
		else
		{
			zpr::println("[log] Requested guest to stop");

			// macos doesn't respond to this, so don't bother waiting
			dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 0 * NSEC_PER_SEC), self->queue, ^{
				zpr::println("[log] Guest did not stop; terminating");

				[self->vmHandle stopWithCompletionHandler:^(NSError* _Nullable errorOrNil) {
					if(error != nil)
						error_and_exit("Guest failed to stop... giving up");

					zpr::println("[log] Guest stopped.");
					dispatch_semaphore_signal(self->stopSemaphore);
					(void) errorOrNil;
				}];
			});
		}
	});
}

- (void)waitForStop
{
	zpr::println("[log] Waiting for VM to stop...");
	dispatch_semaphore_wait(self->stopSemaphore, DISPATCH_TIME_FOREVER);
}

- (void)guestDidStopVirtualMachine:(VZVirtualMachine*)vm
{
	zpr::println("[log] VM shutdown");
	dispatch_semaphore_signal(self->stopSemaphore);
	dispatch_async(dispatch_get_main_queue(), ^{
		[NSApp terminate:self]; //
	});
}

- (void)virtualMachine:(VZVirtualMachine*)vm didStopWithError:(NSError*)error
{
	zpr::println("[log] VM stopped with error: {}", error.description);
	dispatch_semaphore_signal(self->stopSemaphore);
	dispatch_async(dispatch_get_main_queue(), ^{
		[NSApp terminate:self]; //
	});
}


- (void)
    observeValueForKeyPath:(NSString*)keyPath
                  ofObject:(id)object
                    change:(NSDictionary<NSKeyValueChangeKey, id>*)change
                   context:(void*)context
{
	if([keyPath isEqualToString:NSStringFromSelector(@selector(fractionCompleted))] && object != nil)
	{
		zpr::print("\r                 \rProgress: {.2f}%", 100 * ((NSProgress*) object).fractionCompleted);
		fflush(stdout);
	}
	else
	{
		[super observeValueForKeyPath:keyPath ofObject:object change:change context:context];
	}
}


- (bool)saveToBundleURL:(NSURL*)bundle
{
	auto json_url = [bundle URLByAppendingPathComponent:ns_string(CONFIG_JSON_NAME)];

	auto object = @{
		kCpuCount : [NSNumber numberWithUnsignedLongLong:self->cpuCount],
		kRamSize : [NSNumber numberWithUnsignedLongLong:self->ramSize],
		kMachineIdentifier : [self->machineIdentifier.dataRepresentation base64EncodedStringWithOptions:kNilOptions],
		kHardwareModel : [self->hardwareModel.dataRepresentation base64EncodedStringWithOptions:kNilOptions],
		kMacAddress : self->macAddress.string,
	};

	NSError* error = nil;
	auto json_data = [NSJSONSerialization dataWithJSONObject:object options:NSJSONWritingPrettyPrinted error:&error];
	if(json_data == nil || error != nil)
		error_and_exit("Failed to serialise VM config to json: {}", error.description);

	if(not [json_data writeToURL:json_url options:kNilOptions error:&error])
		error_and_exit("Failed to write VM config json: {}", error.description);

	return true;
}

+ (instancetype)loadFromBundleURL:(NSURL*)bundle
{
	auto json_url = [bundle URLByAppendingPathComponent:ns_string(CONFIG_JSON_NAME)];
	if(not [NSFileManager.defaultManager fileExistsAtPath:json_url.path])
		error_and_exit("Could not find {} in VM bundle", CONFIG_JSON_NAME);

	NSError* error = nil;
	auto data = [[NSData alloc] initWithContentsOfURL:json_url options:kNilOptions error:&error];
	if(error != nil)
		error_and_exit("Failed to read {}: {}", CONFIG_JSON_NAME, error.description);

	NSDictionary* json = [NSJSONSerialization JSONObjectWithData:data options:kNilOptions error:&error];
	if(error != nil)
		error_and_exit("Invalid JSON: {}", error.description);

	auto cpus = [(NSNumber*) json[kCpuCount] unsignedLongLongValue];
	auto rams = [(NSNumber*) json[kRamSize] unsignedLongLongValue];
	auto mid_base64 = (NSString*) json[kMachineIdentifier];
	auto hw_base64 = (NSString*) json[kHardwareModel];
	auto mac_str = (NSString*) json[kMacAddress];

	auto mid = [[VZMacMachineIdentifier alloc]
	    initWithDataRepresentation:[[NSData alloc] initWithBase64EncodedString:mid_base64 options:kNilOptions]];

	auto hw = [[VZMacHardwareModel alloc]
	    initWithDataRepresentation:[[NSData alloc] initWithBase64EncodedString:hw_base64 options:kNilOptions]];

	return [[VirtualMachine alloc]
	       initFromBundle:bundle
	             cpuCount:cpus
	              ramSize:rams
	    machineIdentifier:mid
	        hardwareModel:hw
	           macAddress:[[VZMACAddress alloc] initWithString:mac_str]];
}


+ (instancetype)
    createWithBundleURL:(NSURL*)_bundle
           restoreImage:(NSURL*)_restoreImage
               cpuCount:(size_t)_cpus
                ramSize:(size_t)_ram
               diskSize:(size_t)_disk
{
	auto vm = [[VirtualMachine alloc] init];
	vm->bundlePath = _bundle;
	vm->cpuCount = _cpus;
	vm->ramSize = _ram;

	zpr::println("[log] Creating new VM at {}", vm->bundlePath);
	auto setup = create_new_vm(vm->queue, vm->bundlePath, _restoreImage, vm->cpuCount, vm->ramSize, _disk);

	vm->vmHandle = std::get<0>(setup);
	vm->hardwareModel = std::get<1>(setup);
	vm->machineIdentifier = std::get<2>(setup);
	vm->macAddress = std::get<3>(setup);
	vm->vmHandle.delegate = vm;

	zpr::println("[log] Creation successful");
	return vm;
}

@end
