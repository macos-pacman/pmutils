# makefile

CXXFLAGS = -Wall -Wextra -O2 -std=c++20 # -fsanitize=address
WARNINGS = -Wno-unused-function
LIBS = -framework AppKit -framework Virtualization -framework Carbon -fobjc-arc -fobjc-link-runtime
CXX = clang++

MM_HEADERS = $(shell find . -iname "*.h" -print)
MM_SOURCES = $(shell find . -iname "*.mm" -print)

.DEFAULT_GOAL: vmhelper

vmhelper: entitlements.plist $(MM_SOURCES) $(MM_HEADERS)
	$(CXX) $(CXXFLAGS) $(WARNINGS) -o $@ $(MM_SOURCES) $(LDFLAGS) $(LIBS)
	codesign --force --sign - -o runtime --entitlements entitlements.plist --generate-entitlement-der $@

clean:
	rm -f vmhelper
