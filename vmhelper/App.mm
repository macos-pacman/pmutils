// App.mm
// Copyright (c) 2024, zhiayang
// SPDX-License-Identifier: Apache-2.0

#include <signal.h>
#include <Carbon/Carbon.h>

#import "App.h"
#import "common.h"

#include <thread>
#include <chrono>
#include <utility>
#include <unordered_map>

// note: gui code adapted from https://github.com/s-u/macosvm
extern "C" void CPSEnableForegroundOperation(ProcessSerialNumber* psn);

static void dispatch_after(double seconds, void (^block)())
{
	::dispatch_after(dispatch_time(DISPATCH_TIME_NOW, static_cast<int64_t>(seconds * NSEC_PER_SEC)),
	    dispatch_get_main_queue(), block);
}

struct Pixel
{
	uint8_t r;
	uint8_t g;
	uint8_t b;
	uint8_t a;
};

template <typename Fn>
static void foreach_pixel_in_cgcontext(CGContextRef ctx, Fn&& fn)
{
	auto pixels = static_cast<Pixel*>(CGBitmapContextGetData(ctx));
	auto width = CGBitmapContextGetWidth(ctx);
	auto height = CGBitmapContextGetHeight(ctx);

	for(size_t y = 0; y < height; y++)
		for(size_t x = 0; x < width; x++)
			fn(pixels[x + y * width]);
}


static void wait_for_screen_change(App* app, CGContextRef cgc, double percentage, double interval = 0.2);


// https://stackoverflow.com/a/1971027/
static NSString* get_string_for_keycode(CGKeyCode keyCode)
{
	TISInputSourceRef currentKeyboard = TISCopyCurrentKeyboardLayoutInputSource();
	CFDataRef layoutData = static_cast<CFDataRef>(TISGetInputSourceProperty(currentKeyboard,
	    kTISPropertyUnicodeKeyLayoutData));

	auto keyboardLayout = (const UCKeyboardLayout*) CFDataGetBytePtr(layoutData);
	if(keyboardLayout == nullptr)
		return @"x";

	UniChar chars[32];
	memset(&chars[0], 0, sizeof(UniChar) * 32);

	UInt32 keysDown = 0;
	UniCharCount realLength = 0;

	auto err = UCKeyTranslate(keyboardLayout, keyCode, kUCKeyActionDown, 0, LMGetKbdType(),
	    kUCKeyTranslateNoDeadKeysBit, &keysDown, sizeof(chars) / sizeof(chars[0]), &realLength, chars);

	if(err != noErr)
		return @"z";

	CFRelease(currentKeyboard);

	return [NSString stringWithCharacters:chars length:1];
}

static CGKeyCode get_keycode_for_char(const char c)
{
	static std::unordered_map<char, CGKeyCode> mapping {};

	if(mapping.empty())
	{
		/* Loop through every keycode (0 - 127) to find its current mapping. */
		for(CGKeyCode i = 0; i < 128; ++i)
		{
			auto s = get_string_for_keycode((CGKeyCode) i);
			if(s.length > 0)
				mapping[s.UTF8String[0]] = i;
		}
	}

	if('A' <= c && c <= 'Z')
		return mapping[c + ('a' - 'A')];

	return mapping[c];
}




@implementation App
- (instancetype)init
{
	self = [super init];
	self->automaticSetup = false;
	self->setupUsername = nil;
	self->setupPassword = nil;

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
	if(self->vm.vm.state == VZVirtualMachineStateRunning)
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

	if(self->automaticSetup)
	{
		// disable resizing the window
		self->window.styleMask &= ~NSWindowStyleMaskResizable;

		zpr::println("[log] Running automatic setup; waiting 15 seconds for VM to boot");

		// stupid carbon shit must be run on the main thread; do it now to pre-populate the cache.
		get_keycode_for_char('x');

		auto queue = dispatch_queue_create("setup_queue", DISPATCH_QUEUE_SERIAL);
		dispatch_async(queue, ^{ [self runAutomaticSetup]; });
	}
}

- (std::pair<NSEvent*, NSEvent*>)makeEventPairWithKeyCode:(unsigned)keyCode
{
	auto time = [NSDate timeIntervalSinceReferenceDate];
	auto down = [NSEvent
	               keyEventWithType:NSEventTypeKeyDown
	                       location:CGPointZero
	                  modifierFlags:0
	                      timestamp:time
	                   windowNumber:0
	                        context:nil
	                     characters:@""
	    charactersIgnoringModifiers:@""
	                      isARepeat:NO
	                        keyCode:keyCode];

	auto up = [NSEvent
	               keyEventWithType:NSEventTypeKeyUp
	                       location:CGPointZero
	                  modifierFlags:0
	                      timestamp:(time + 0.1)
	                   windowNumber:0
	                        context:nil
	                     characters:@""
	    charactersIgnoringModifiers:@""
	                      isARepeat:NO
	                        keyCode:keyCode];

	return { down, up };
}

- (void)sendKeyCode:(unsigned)keyCode
      withModifiers:(NSEventModifierFlags)modifiers
              after:(std::chrono::duration<double>)delta
       thenSleepFor:(std::chrono::duration<double>)slp
{
	if(delta != delta.zero())
		std::this_thread::sleep_for(delta);

	// modifiers appear to be fucked for some reason
	std::vector<NSEvent*> down_events {};
	std::vector<NSEvent*> up_events {};

	auto add_key = [&](unsigned key) {
		NSEvent* down = nil;
		NSEvent* up = nil;
		std::tie(down, up) = [self makeEventPairWithKeyCode:key];
		down_events.push_back(down);
		up_events.push_back(up);
	};

	if(modifiers & NSEventModifierFlagCommand)
		add_key(kVK_Command);

	if(modifiers & NSEventModifierFlagOption)
		add_key(kVK_Option);

	if(modifiers & NSEventModifierFlagFunction)
		add_key(kVK_Function);

	if(modifiers & NSEventModifierFlagControl)
		add_key(kVK_Control);

	if(modifiers & NSEventModifierFlagShift)
		add_key(kVK_Shift);

	add_key(keyCode);

	dispatch_async(dispatch_get_main_queue(), ^{
		for(auto e : down_events)
			[self->window postEvent:e atStart:NO];

		for(auto e : up_events)
			[self->window postEvent:e atStart:NO];
	});

	if(slp != slp.zero())
		std::this_thread::sleep_for(slp);
}

- (void)sendKeyCode:(unsigned)keyCode
      withModifiers:(NSEventModifierFlags)modifiers
              after:(std::chrono::duration<double>)delta
{
	using namespace std::chrono_literals;
	[self sendKeyCode:keyCode withModifiers:modifiers after:delta thenSleepFor:0s];
}

- (void)
     sendKeyCode:(unsigned)keyCode
           after:(std::chrono::duration<double>)delta
    thenSleepFor:(std::chrono::duration<double>)slp
{
	[self sendKeyCode:keyCode withModifiers:0 after:delta thenSleepFor:slp];
}

- (void)sendKeyCode:(unsigned)keyCode thenSleepFor:(std::chrono::duration<double>)slp
{
	using namespace std::chrono_literals;
	[self sendKeyCode:keyCode withModifiers:0 after:0s thenSleepFor:slp];
}

- (void)sendKeyCode:(unsigned)keyCode after:(std::chrono::duration<double>)delta
{
	[self sendKeyCode:keyCode withModifiers:0 after:delta];
}

- (void)sendKeyCode:(unsigned)keyCode
{
	[self sendKeyCode:keyCode after: {}];
}



- (void)sendKeyPress:(char)key after:(std::chrono::duration<double>)delta
{
	if(delta != delta.zero())
		std::this_thread::sleep_for(delta);

	// for caps, press shift.
	NSEventModifierFlags mods = 0;
	if('A' <= key && key <= 'Z')
		mods = NSEventModifierFlagShift;

	[self sendKeyCode:get_keycode_for_char(key) withModifiers:mods after:delta];
}

- (void)sendString:(NSString*)str
{
	using namespace std::chrono_literals;

	auto chrs = std::string(str.UTF8String);
	for(auto c : chrs)
		[self sendKeyPress:c after:70ms];
}

- (void)clickWindowAt:(NSPoint)point
{
	using namespace std::chrono_literals;

	auto time = [NSDate timeIntervalSinceReferenceDate];
	auto move = [NSEvent
	    mouseEventWithType:NSEventTypeMouseMoved
	              location:point
	         modifierFlags:0
	             timestamp:(time) windowNumber:0
	               context:nil
	           eventNumber:0
	            clickCount:0
	              pressure:1.0];

	auto down = [NSEvent
	    mouseEventWithType:NSEventTypeLeftMouseDown
	              location:point
	         modifierFlags:0
	             timestamp:(time + 0.1)
	          windowNumber:0
	               context:nil
	           eventNumber:1
	            clickCount:0
	              pressure:1.0];

	auto up = [NSEvent
	    mouseEventWithType:NSEventTypeLeftMouseUp
	              location:point
	         modifierFlags:0
	             timestamp:(time + 0.2)
	          windowNumber:0
	               context:nil
	           eventNumber:2
	            clickCount:0
	              pressure:1.0];


	[self->vmView mouseMoved:move];
	std::this_thread::sleep_for(0.3s);

	[self->vmView mouseMoved:down];
	[self->vmView mouseMoved:up];
}


- (void)runAutomaticSetup
{
	signal(SIGTRAP, [](int) { NSLog(@"%@", NSThread.callStackSymbols); });


	// make a context for the thing
	auto colour_space = CGColorSpaceCreateDeviceRGB();
	auto cgc = CGBitmapContextCreate(nullptr, vmView.bounds.size.width, vmView.bounds.size.height, 8, 0, colour_space,
	    kCGImageAlphaPremultipliedLast);

	if(cgc == nullptr)
		error_and_exit("Failed to create CGContext!");

	// wait for the vm to boot.
	zpr::println("[log] Waiting to reach setup screen...");
	wait_for_screen_change(self, cgc, 0.9);

	using namespace std::chrono_literals;

	zpr::println("[log] Entering setup screen");
	[self sendKeyCode:kVK_Space after:1s];
	std::this_thread::sleep_for(3s);

	auto send_tab_sequence_then_space = [&](int num) {
		for(int i = 0; i < num; i++)
			[self sendKeyCode:kVK_Tab after:0.35s];

		[self sendKeyCode:kVK_Space after:0.35s thenSleepFor:1s];
	};

	auto click_continue = [&](bool sleep = true) { //
		[self clickWindowAt:NSPoint { .x = vmView.bounds.size.width - 150, .y = 65 }];
		if(sleep)
			std::this_thread::sleep_for(0.5s);
	};

	auto click_left_button = [&]() { //
		[self clickWindowAt:NSPoint { .x = 150, .y = 65 }];
		std::this_thread::sleep_for(0.5s);
	};

	zpr::println("");
	zpr::println("===== SCREEN: Language =====");
	zpr::println("[log] Using default language");

	// language selection: just use the default for now
	[self sendKeyCode:kVK_Return];

	zpr::println("[log] Waiting...");

	// note: it seems like the window goes away, then re-appears.
	// so wait for a change after a longer time period
	wait_for_screen_change(self, cgc, 0.1, /* interval: */ 10);

	// default region too
	zpr::println("");
	zpr::println("===== SCREEN: Select Your Country or Region =====");

	zpr::println("[log] Enabling: full keyboard access, reduce transparency");
	[self sendKeyCode:kVK_F5
	    withModifiers:(NSEventModifierFlagCommand | NSEventModifierFlagOption | NSEventModifierFlagFunction)
	            after:0s
	     thenSleepFor:2s];

	send_tab_sequence_then_space(6);
	send_tab_sequence_then_space(4);
	send_tab_sequence_then_space(2);

	zpr::println("[log] Setting default region");
	send_tab_sequence_then_space(2);

	zpr::println("");
	zpr::println("===== SCREEN: Written and Spoken Languages =====");
	std::this_thread::sleep_for(1s);
	zpr::println("[log] Continue");
	click_continue();

	// this one takes a while
	zpr::println("");
	zpr::println("===== SCREEN: Accessibility =====");
	std::this_thread::sleep_for(1s);
	zpr::println("[log] Not Now");
	click_continue(/* sleep: */ false);

	wait_for_screen_change(self, cgc, 0.07);

	zpr::println("");
	zpr::println("===== SCREEN: Data & Privacy =====");
	std::this_thread::sleep_for(1s);
	zpr::println("[log] Continue");
	click_continue();

	zpr::println("");
	zpr::println("===== SCREEN: Migration Assistant =====");
	zpr::println("[log] Not Now");
	click_left_button();
	// send_tab_sequence_then_space(2);

	zpr::println("");
	zpr::println("===== SCREEN: Sign In with Your Apple ID =====");
	std::this_thread::sleep_for(2s);

	zpr::println("[log] Set Up Later");
	click_left_button();
	// send_tab_sequence_then_space(5);

	zpr::println("[log] Skip");
	[self clickWindowAt:NSPoint { .x = 350, .y = vmView.bounds.size.height - 250 }];
	// send_tab_sequence_then_space(1);
	std::this_thread::sleep_for(0.5s);


	zpr::println("");
	zpr::println("===== SCREEN: Terms and Conditions =====");
	std::this_thread::sleep_for(1s);
	zpr::println("[log] Agree");
	click_continue();

	std::this_thread::sleep_for(1s);
	zpr::println("[log] Agree");
	// [self clickWindowAt:NSPoint { .x = 350, .y = vmView.bounds.size.height - 240 }];
	send_tab_sequence_then_space(1);

	zpr::println("");
	zpr::println("===== SCREEN: Create a Computer Account =====");
	zpr::println("[log] Username: {}", self->setupUsername);
	zpr::println("[log] Password: {}", self->setupPassword);
	std::this_thread::sleep_for(1s);

	[self sendString:self->setupUsername];
	[self sendKeyCode:kVK_Tab];
	[self sendString:self->setupUsername];
	[self sendKeyCode:kVK_Tab];
	[self sendString:self->setupPassword];
	[self sendKeyCode:kVK_Tab];
	[self sendString:self->setupPassword];

	std::this_thread::sleep_for(1s);
	zpr::println("[log] Continue");
	click_continue();

	// yes, it's only a ~5% change
	wait_for_screen_change(self, cgc, 0.03);

	zpr::println("");
	zpr::println("===== SCREEN: Enable Location Services =====");
	std::this_thread::sleep_for(1s);
	zpr::println("[log] Continue");
	click_continue();

	std::this_thread::sleep_for(1s);
	zpr::println("[log] Don't Use");
	[self clickWindowAt:NSPoint { .x = 320, .y = 160 }];
	// send_tab_sequence_then_space(1);

	zpr::println("");
	zpr::println("===== SCREEN: Select Your Time Zone =====");
	std::this_thread::sleep_for(1s);
	zpr::println("[log] Continue");
	click_continue();

	// just wait
	std::this_thread::sleep_for(3s);

	zpr::println("");
	zpr::println("===== SCREEN: Analytics =====");
	zpr::println("[log] No sharing");
	send_tab_sequence_then_space(0);
	std::this_thread::sleep_for(1s);
	zpr::println("[log] Continue");
	click_continue();

	// just wait
	std::this_thread::sleep_for(3s);

	zpr::println("");
	zpr::println("===== SCREEN: Screen Time =====");
	std::this_thread::sleep_for(1s);
	zpr::println("[log] Continue");
	click_continue();

	wait_for_screen_change(self, cgc, 0.1);

	zpr::println("");
	zpr::println("===== SCREEN: Choose Your Look =====");
	std::this_thread::sleep_for(1s);
	zpr::println("[log] Continue");
	click_continue();

	wait_for_screen_change(self, cgc, 0.1);

	zpr::println("");
	zpr::println("===== SCREEN: The Desktop =====");
	std::this_thread::sleep_for(3s);

	// enable remote login
	// zpr::println("[log] Open Spotlight");
	// [self sendKeyCode:kVK_Space withModifiers:(NSEventModifierFlagCommand) after:0s thenSleepFor:0.5s];
	// [self sendString:@"/System/Applications/Utilities/Terminal.app"];
	// [self sendKeyCode:kVK_Return after:0.5s thenSleepFor:2s];

	// spotlight likes to fuck with you sometimes. just use finder to launch terminal.
	zpr::println("[log] Open Terminal");
	[self sendKeyCode:get_keycode_for_char('u')
	    withModifiers:(NSEventModifierFlagCommand | NSEventModifierFlagShift)
	            after:0.5s
	     thenSleepFor:1s];

	// move to terminal, then cmd-o it
	[self sendKeyPress:'t' after:0.5s];
	[self sendKeyCode:get_keycode_for_char('o') withModifiers:(NSEventModifierFlagCommand) after:0.5s thenSleepFor:2s];

	zpr::println("[log] Enable Remote Access");
	[self sendString:@"sudo launchctl load -w /System/Library/LaunchDaemons/ssh.plist"];
	[self sendKeyCode:kVK_Return after:0.5s thenSleepFor:1s];

	zpr::println("[log] Typing password...");
	[self sendString:self->setupPassword];
	[self sendKeyCode:kVK_Return after:0.5s thenSleepFor:1s];

	zpr::println("[log] Done! Shutting down...");

	std::this_thread::sleep_for(2s);

	zpr::println("[log] Click: Apple Menu");
	[self clickWindowAt:NSPoint { .x = 10, .y = vmView.bounds.size.height - 10 }];
	std::this_thread::sleep_for(0.5s);

	zpr::println("[log] Click: Shutdown");
	[self clickWindowAt:NSPoint { .x = 10, .y = vmView.bounds.size.height - 120 }];
	std::this_thread::sleep_for(1s);

	zpr::println("[log] Enter: Shutdown");
	[self sendKeyCode:kVK_Return];

	CGColorSpaceRelease(colour_space);
	CGContextRelease(cgc);

	// kill the vm.
}


- (NSImage*)getWindowContentsAsNSImage
{
	auto rep = [vmView bitmapImageRepForCachingDisplayInRect:vmView.bounds];
	[vmView cacheDisplayInRect:vmView.bounds toBitmapImageRep:rep];

	auto img = [[NSImage alloc] initWithSize:vmView.bounds.size];
	[img addRepresentation:rep];
	return img;
}

- (void)drawWindowContentsIntoCGContext:(CGContextRef)ctx
{
	auto rep = [vmView bitmapImageRepForCachingDisplayInRect:vmView.bounds];
	[vmView cacheDisplayInRect:vmView.bounds toBitmapImageRep:rep];

	auto img = [[NSImage alloc] initWithSize:vmView.bounds.size];
	[img addRepresentation:rep];

	auto nsgc = [NSGraphicsContext graphicsContextWithCGContext:ctx flipped:false];
	auto old = [NSGraphicsContext currentContext];

	[NSGraphicsContext setCurrentContext:nsgc];

	[img drawInRect:NSMakeRect(0, 0, img.size.width, img.size.height)];
	[NSGraphicsContext setCurrentContext:old];
}


- (void)createGui
{
	vmView = [[VZVirtualMachineView alloc] init];
	vmView.capturesSystemKeys = YES;
	vmView.virtualMachine = [self->vm vm];

	auto rect = NSMakeRect(20, 20, 640, 400);
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



static void wait_for_screen_change(App* app, CGContextRef cgc, double percentage, double interval)
{
	auto sem = dispatch_semaphore_create(0);
	using Waiter = void (*)(void*, App*, dispatch_semaphore_t, CGContextRef, CFDataRef, size_t, size_t, double, double);

	const auto cg_width = CGBitmapContextGetWidth(cgc);
	const auto cg_height = CGBitmapContextGetHeight(cgc);

	Waiter waiter =
	    [](void* _self, App* app, dispatch_semaphore_t sem, CGContextRef cgc, CFDataRef reference, size_t cg_width,
	        size_t cg_height, double interval, double min_change //
	    ) {
		    [app drawWindowContentsIntoCGContext:cgc];
		    auto new_img = CGBitmapContextCreateImage(cgc);

		    auto new_data = CGDataProviderCopyData(CGImageGetDataProvider(new_img));

		    auto old_pixels = reinterpret_cast<const Pixel*>(CFDataGetBytePtr(reference));
		    auto new_pixels = reinterpret_cast<const Pixel*>(CFDataGetBytePtr(new_data));

		    size_t different_pixels = 0;
		    for(size_t y = 0; y < cg_height; y++)
		    {
			    for(size_t x = 0; x < cg_width; x++)
			    {
				    auto p0 = old_pixels[x + y * cg_width];
				    auto p1 = new_pixels[x + y * cg_width];

				    if(p0.r != p1.r || p0.g != p1.g || p0.b != p1.b)
					    different_pixels++;
			    }
		    }

		    CGImageRelease(new_img);
		    CFRelease(new_data);

		    auto pct = static_cast<double>(different_pixels) / (cg_width * cg_height);

		    zpr::print("\r                                      ");
		    zpr::print("\r[log] {.1f}% of pixels changed", 100 * pct);
		    // zpr::println("[log] {.1f}% of pixels changed", 100 * pct);
		    fflush(stdout);

		    if(pct < min_change)
		    {
			    dispatch_after(interval, ^{
				    ((Waiter) _self)(_self, app, sem, cgc, reference, cg_width, cg_height, min_change, interval); //
			    });
		    }
		    else
		    {
			    zpr::println("");
			    dispatch_semaphore_signal(sem);
		    }
	    };

	// get the current image
	[app drawWindowContentsIntoCGContext:cgc];
	auto img = CGBitmapContextCreateImage(cgc);

	auto data = CGDataProviderCopyData(CGImageGetDataProvider(img));
	CGImageRelease(img);

	dispatch_after(interval, ^{
		waiter((void*) waiter, app, sem, cgc, data, cg_width, cg_height, percentage, interval);
	});
	dispatch_semaphore_wait(sem, DISPATCH_TIME_FOREVER);

	CFRelease(data);

	// for safety
	using namespace std::chrono_literals;
	std::this_thread::sleep_for(1s);
}
