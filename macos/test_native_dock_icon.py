#!/usr/bin/env python3
"""Render the real patched Dock callback offscreen, without Wine or an account.
SPDX-License-Identifier: GPL-3.0-only
"""
import argparse
import hashlib
from pathlib import Path
import platform
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from test_native_window_snapshot import method

ORIGINAL_SHA256 = 'ad49b526a6230396bd6fee263ff14febfad9d28b106788038968d099d376a1cf'

PREFIX = r'''
/* Wine-derived method retains Wine's LGPL-2.1-or-later notices. */
#import <AppKit/AppKit.h>
#include <stdlib.h>
'''

SUFFIX = r'''
int main(int argc, char **argv) { @autoreleasepool {
    [NSApplication sharedApplication];
    [NSApp setActivationPolicy:NSApplicationActivationPolicyProhibited];
    NSView *original = [[[NSView alloc] initWithFrame:NSMakeRect(0,0,128,128)] autorelease];
    [NSApp dockTile].contentView = original;
    IconFixture *fixture = [[[IconFixture alloc] init] autorelease];
    unsetenv("EXITLAG_MACOS_APP_ICON");
    [fixture setApplicationIconFromCGImageArray:@[]];
    NSCAssert([NSApp dockTile].contentView == original, @"Other Wine programs keep their Dock behavior");
    setenv("EXITLAG_MACOS_APP_ICON", argv[1], 1);
    [fixture setApplicationIconFromCGImageArray:@[]];
    NSView *view = [NSApp dockTile].contentView;
    NSCAssert([view isKindOfClass:ExitLagDockView.class] && !view.isOpaque,
        @"Use the scoped transparent Dock view");
    NSCAssert(NSEqualSizes(view.frame.size, NSMakeSize(128,128)), @"Fill the Dock canvas");
    NSBitmapImageRep *bitmap = [[[NSBitmapImageRep alloc] initWithBitmapDataPlanes:NULL
        pixelsWide:128 pixelsHigh:128 bitsPerSample:8 samplesPerPixel:4 hasAlpha:YES
        isPlanar:NO colorSpaceName:NSDeviceRGBColorSpace bytesPerRow:0 bitsPerPixel:0] autorelease];
    [NSGraphicsContext saveGraphicsState];
    [NSGraphicsContext setCurrentContext:[NSGraphicsContext graphicsContextWithBitmapImageRep:bitmap]];
    /* Invoke the actual view callback, not NSImage's drawing directly: an
       NSImageView used here previously painted an entirely empty Dock tile. */
    [view drawRect:view.bounds];
    [NSGraphicsContext restoreGraphicsState];
    NSCAssert([bitmap colorAtX:2 y:2].alphaComponent < .01, @"Keep transparent padding");
    unsigned opaque = 0;
    for (unsigned y=0; y<128; y++) for (unsigned x=0; x<128; x++)
        if ([bitmap colorAtX:x y:y].alphaComponent > .9) opaque++;
    NSCAssert(opaque > 3000 && opaque < 5000, @"Paint the icon instead of an empty tile");
    puts("PASS: actual Dock callback paints the icon, preserves alpha, and leaves other apps alone");
} return 0; }
'''


def fixture_png():
    def chunk(kind, data):
        return (struct.pack('!I', len(data)) + kind + data
                + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff))
    rows = []
    for y in range(128):
        rows.append(b'\0' + b''.join(b'\xc8\x10\x10\xff' if 32 <= x < 96 and 32 <= y < 96
                                    else bytes(4) for x in range(128)))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', 128, 128, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b''.join(rows))) + chunk(b'IEND', b''))


def check_idle_plugin(work):
    """Load the real closed-app plug-in from a fixture bundle and paint it."""
    app = work / 'ExitLag.app'
    resources = app / 'Contents/Resources'
    resources.mkdir(parents=True)
    (resources / '.exitlag-bridge-client').write_text('exitlag-bridge\n')
    (resources / 'ClientIcon.icns').write_bytes(fixture_png())
    plugin = app / 'Contents/PlugIns/ExitLagIcon.docktileplugin'
    (plugin / 'Contents/MacOS').mkdir(parents=True)
    (plugin / 'Contents/Info.plist').write_bytes(plistlib.dumps(dict(
        CFBundleExecutable='ExitLagDockTile', CFBundleIdentifier='local.cheaux.ExitLagClient.DockTile',
        CFBundlePackageType='BNDL', CFBundleVersion='1', NSPrincipalClass='ExitLagIdleDockTile')))
    native = ['xcrun', 'clang', '-arch', platform.machine(), '-mmacosx-version-min=15.0',
              '-fobjc-arc', '-framework', 'AppKit']
    subprocess.run(native + ['-bundle', str(Path(__file__).parent / 'app/client-dock.m'),
                            '-o', str(plugin / 'Contents/MacOS/ExitLagDockTile')], check=True)
    subprocess.run(['codesign', '--force', '--sign', '-', str(plugin)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    fixture = work / 'idle-test.m'
    fixture.write_text(r'''
#import <AppKit/AppKit.h>
int main(int argc, char **argv) { @autoreleasepool {
    [NSApplication sharedApplication];
    [NSApp setActivationPolicy:NSApplicationActivationPolicyProhibited];
    NSBundle *bundle = [NSBundle bundleWithPath:[NSString stringWithUTF8String:argv[1]]];
    NSError *error;
    NSCAssert([bundle loadAndReturnError:&error], @"Load the signed closed-app plug-in: %@", error);
    id<NSDockTilePlugIn> plugin = [[bundle principalClass] new];
    [plugin setDockTile:NSApp.dockTile];
    NSView *view = NSApp.dockTile.contentView;
    NSCAssert(view != nil && !view.isOpaque, @"Install the inactive transparent view");
    NSBitmapImageRep *bitmap = [[NSBitmapImageRep alloc] initWithBitmapDataPlanes:NULL
        pixelsWide:128 pixelsHigh:128 bitsPerSample:8 samplesPerPixel:4 hasAlpha:YES
        isPlanar:NO colorSpaceName:NSDeviceRGBColorSpace bytesPerRow:0 bitsPerPixel:0];
    [NSGraphicsContext saveGraphicsState];
    [NSGraphicsContext setCurrentContext:[NSGraphicsContext graphicsContextWithBitmapImageRep:bitmap]];
    [view drawRect:view.bounds];
    [NSGraphicsContext restoreGraphicsState];
    NSCAssert([bitmap colorAtX:2 y:2].alphaComponent < .01, @"Keep the closed tile transparent");
    unsigned opaque = 0;
    for (unsigned y=0; y<128; y++) for (unsigned x=0; x<128; x++)
        if ([bitmap colorAtX:x y:y].alphaComponent > .9) opaque++;
    NSCAssert(opaque > 3000 && opaque < 5000, @"Paint the local icon while the app is closed");
    [plugin setDockTile:nil];
    NSCAssert(NSApp.dockTile.contentView == nil, @"Release the detached tile");
    puts("PASS: closed-app Dock plug-in loads, paints transparent pixels, and detaches cleanly");
} return 0; }
''')
    binary = work / 'idle-test'
    subprocess.run(native + [str(fixture), '-o', str(binary)], check=True)
    subprocess.run([str(binary), str(plugin)], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wine-source', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('This check requires macOS AppKit and Xcode command-line tools')
    original = args.wine_source / 'dlls/winemac.drv/cocoa_app.m'
    if hashlib.sha256(original.read_bytes()).hexdigest() != ORIGINAL_SHA256:
        parser.error('The Wine Cocoa source differs from the pinned unmodified version')
    patch = Path(__file__).resolve().parent / 'wine-patches/0002-use-exitlag-transparent-dock-icon.patch'
    with tempfile.TemporaryDirectory(prefix='exitlag-dock-test-') as folder:
        work = Path(folder)
        target = work / 'dlls/winemac.drv/cocoa_app.m'
        target.parent.mkdir(parents=True)
        shutil.copyfile(original, target)
        subprocess.run(['patch', '-p1', '-i', str(patch)], cwd=work, check=True,
                       stdout=subprocess.DEVNULL)
        source = target.read_text()
        helper = source[source.index('/* ExitLag-only Dock drawing;'):
                        source.index('@interface WineApplicationController ()')]
        code = PREFIX + helper + '''
@interface IconFixture : NSObject { NSImage *applicationIcon; }
@property(retain,nonatomic) NSImage *applicationIcon;
@end
@implementation IconFixture
@synthesize applicationIcon;
''' + method(source, '- (void) setApplicationIconFromCGImageArray:') + '\n@end\n' + SUFFIX
        fixture = work / 'dock-test.m'
        fixture.write_text(code)
        binary = work / 'dock-test'
        subprocess.run(['xcrun', 'clang', '-arch', 'x86_64', '-mmacosx-version-min=15.0',
                        '-fblocks', '-framework', 'AppKit', str(fixture), '-o', str(binary)], check=True)
        # NSImage recognizes this self-contained PNG by its data. The filename
        # exercises the same opt-in guard as the generated ICNS in production.
        icon = work / 'ClientIcon.icns'
        icon.write_bytes(fixture_png())
        subprocess.run([str(binary), str(icon)], check=True)
        check_idle_plugin(work)


if __name__ == '__main__':
    main()
