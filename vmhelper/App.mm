// App.mm
// Copyright (c) 2024, zhiayang
// SPDX-License-Identifier: Apache-2.0

#import "App.h"
#import "common.h"

// note: gui code adapted from https://github.com/s-u/macosvm
extern "C" void CPSEnableForegroundOperation(ProcessSerialNumber* psn);

@implementation App
- (instancetype)init
{
	self = [super init];
	self->window = nil;
	self->vmView = nil;
	return self;
}

- (instancetype)initForRunningFromBundle:(NSURL*)bundle withGUI:(bool)gui
{
	self = [self init];

	if(gui)
		self->runMode = RunMode::RunGUI;
	else
		self->runMode = RunMode::Run;

	self->bundleURL = bundle;
	self->vm = nil;

	self->window = nil;
	self->vmView = nil;
	return self;
}

- (instancetype)
       initFromNewBundle:(NSURL*)bundle
    withCreationSettings:(CreationSettings)settings
            restoreImage:(NSURL*)_restoreImage
{
	self = [self init];
	self->vm = nil;
	self->runMode = RunMode::CreateAndRestore;
	self->bundleURL = bundle;

	self->restoreImage = _restoreImage;
	self->creationSettings = std::move(settings);
	return self;
}

- (instancetype)initFromBundle:(NSURL*)bundle withRestoreImage:(NSURL*)_restoreImage
{
	self = [self init];
	self->vm = nil;
	self->runMode = RunMode::Restore;
	self->bundleURL = bundle;
	self->restoreImage = _restoreImage;
	return self;
}

- (void)windowWillClose:(NSNotification*)notification
{
	[self->vm stop];
	dispatch_async(dispatch_get_main_queue(), ^{
		[self->vm waitForStop]; //
		[NSApp terminate:self];
	});
}

- (void)applicationWillTerminate:(NSNotification*)notification
{
	zpr::println("[log] Done");
}

- (void)stopVM
{
	if(self->vm != nil)
	{
		[self->vm stop];
		[self->vm waitForStop];
	}
}

- (void)applicationDidFinishLaunching:(NSNotification*)notification
{
	switch(self->runMode)
	{
		case RunMode::CreateAndRestore: {
			NSAssert(self->restoreImage != nil, @"Restore image missing!");

			auto& cs = self->creationSettings;
			self->vm = [VirtualMachine
			    createWithBundleURL:self->bundleURL
			           restoreImage:self->restoreImage
			               cpuCount:cs.cpu_count
			                ramSize:cs.ram_size
			               diskSize:cs.disk_size];

			[self->vm saveToBundleURL:self->bundleURL];
			[self->vm restoreFromIPSW:self->restoreImage];
			[self->vm waitForStop];
			[NSApp terminate:self];
			break;
		}

		case RunMode::Restore: {
			NSAssert(self->restoreImage != nil, @"Restore image missing!");

			self->vm = [VirtualMachine loadFromBundleURL:self->bundleURL];
			[self->vm restoreFromIPSW:self->restoreImage];
			[self->vm waitForStop];
			[NSApp terminate:self];
			break;
		}

		case RunMode::Run: {
			self->vm = [VirtualMachine loadFromBundleURL:self->bundleURL];

			// start the vm, then wait for it to stop
			[self->vm start];
			[self->vm waitForStop];
			[NSApp terminate:self];
			break;
		}

		case RunMode::RunGUI: {
			self->vm = [VirtualMachine loadFromBundleURL:self->bundleURL];
			[self createGui];

			// start the vm; for gui, wait for stopping in the other place.
			[self->vm start];
			break;
		}
	}
}

- (void)createGui
{
	vmView = [[VZVirtualMachineView alloc] init];
	vmView.capturesSystemKeys = YES;
	vmView.virtualMachine = [self->vm vm];

	auto rect = NSMakeRect(10, 10, 1280, 800);
	window = [[NSWindow alloc]
	    initWithContentRect:rect
	              styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable
	                        | NSWindowStyleMaskResizable
	                backing:NSBackingStoreBuffered
	                  defer:NO];

	[window setOpaque:NO];
	[window setDelegate:self];
	[window setContentView:vmView];
	[window setInitialFirstResponder:vmView];
	[window setTitle:@"vm"];

	if(![NSApp mainMenu])
	{
		auto mainMenu = [[NSMenu alloc] init];

		auto menu = [[NSMenu alloc] initWithTitle:@"Window"];
		[menu addItem:[[NSMenuItem
		                  alloc] initWithTitle:@"Minimise" action:@selector(performMiniaturize:) keyEquivalent:@"m"]];
		[menu addItem:[[NSMenuItem alloc] initWithTitle:@"Zoom" action:@selector(performZoom:) keyEquivalent:@""]];
		[menu
		    addItem:
		        [[NSMenuItem alloc] initWithTitle:@"Close Window" action:@selector(performClose:) keyEquivalent:@"w"]];

		[menu addItem:[[NSMenuItem alloc] initWithTitle:@"Copy" action:@selector(copy:) keyEquivalent:@"c"]];
		[menu addItem:[[NSMenuItem alloc] initWithTitle:@"Paste" action:@selector(paste:) keyEquivalent:@"v"]];

		auto item = [[NSMenuItem alloc] initWithTitle:@"Window" action:nil keyEquivalent:@""];
		[item setSubmenu:menu];

		[mainMenu addItem:item];
		[NSApp setMainMenu:mainMenu];
	}

	[window makeKeyAndOrderFront:vmView];

	if(![[NSRunningApplication currentApplication] isActive])
		[[NSRunningApplication currentApplication] activateWithOptions:NSApplicationActivateAllWindows];


	{
		/* we have to make us foreground process so we can receive keyboard
		     events - I know of no way that doesn't involve deprecated API .. */
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
		ProcessSerialNumber myProc {};

		if(GetCurrentProcess(&myProc) == noErr)
			CPSEnableForegroundOperation(&myProc);

#pragma clang diagnostic pop
	}
}

@end
