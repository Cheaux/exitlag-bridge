#!/usr/bin/env python3
"""Account-free AppKit regression check for the Wine Dock animation patch.
The native fixture stays hidden; no desktop capture or Wine account is used.
SPDX-License-Identifier: GPL-3.0-only
"""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ORIGINAL_SHA256 = '57aa0b3b06ac601f179c073845aeb8fb392b20d60766d6fe9d7e64f99dc45c6f'

def method(source, signature):
    start = source.index(signature)
    opening = source.index('{', start)
    depth = 1
    end = opening + 1
    while depth:
        if source[end] == '{': depth += 1
        if source[end] == '}': depth -= 1
        end += 1
    return source[start:end]

PREFIX = r'''
/* Test of Wine-derived code, retaining Wine's LGPL-2.1-or-later notices. */
#import <AppKit/AppKit.h>
#import <QuartzCore/QuartzCore.h>
#import <objc/runtime.h>
#include <unistd.h>
#include <string.h>
#include <stdlib.h>
static BOOL stage_manager_enabled(void) { return NO; }
static void release_pixels(void *info, const void *data, size_t size) { free((void *)data); }
@interface WineWindow : NSWindow
@property BOOL closing, usePerPixelAlpha, shapeChangedSinceLastDraw;
- (void)windowDidDrawContent;
@end
@implementation WineWindow
- (BOOL)isVisible { return YES; } // The test window remains hidden.
- (void)windowDidDrawContent {}
@end
'''

MIDDLE = r'''
@interface TestContentView : NSView {
    CGImageRef colorImage, shapeImage;
    CGRect surfaceRect;
}
- (void)setColorImage:(CGImageRef)image;
@end
@implementation TestContentView
- (BOOL)wantsUpdateLayer { return YES; }
'''

SUFFIX = r'''
@end
int main(int argc, char **argv) {
 @autoreleasepool {
    [NSApplication sharedApplication];
    [NSApp setActivationPolicy:NSApplicationActivationPolicyProhibited];
    WineWindow *window = [[WineWindow alloc] initWithContentRect:NSMakeRect(0,0,1440,810)
        styleMask:NSWindowStyleMaskBorderless backing:NSBackingStoreBuffered defer:NO];
    window.title = @"ExitLag";
    TestContentView *view = [[TestContentView alloc] initWithFrame:NSMakeRect(0,0,1440,810)];
    view.wantsLayer = YES;
    view.layer.contentsScale = 1;
    [view setAutoresizesSubviews:NO];
    window.contentView = view;
    CGColorSpaceRef space = CGColorSpaceCreateDeviceRGB();
    unsigned char *pixels = malloc(1536*896*4);
    for (unsigned y=0;y<896;y++) for (unsigned x=0;x<1536;x++) {
        unsigned char *p=pixels+(y*1536+x)*4;
        BOOL visible=x<1440 && y<810;
        p[0]=visible?30:255; p[1]=visible?40:255; p[2]=visible?50:255; p[3]=255;
    }
    CGDataProviderRef provider=CGDataProviderCreateWithData(NULL,pixels,1536*896*4,release_pixels);
    CGImageRef padded=CGImageCreate(1536,896,8,32,1536*4,space,
        kCGImageAlphaPremultipliedLast | kCGBitmapByteOrder32Big,provider,NULL,false,kCGRenderingIntentDefault);
    CGDataProviderRelease(provider); CGColorSpaceRelease(space);
    [view setColorImage:padded];
    CGImageRelease(padded);
    [view updateLayer];
    NSImage *snapshot = objc_getAssociatedObject(window,&frameKey);
    CGImageRef displayed = (CGImageRef)view.layer.contents;
    NSBitmapImageRep *sample=[[NSBitmapImageRep alloc] initWithBitmapDataPlanes:NULL pixelsWide:1440
        pixelsHigh:810 bitsPerSample:8 samplesPerPixel:4 hasAlpha:YES isPlanar:NO
        colorSpaceName:NSDeviceRGBColorSpace bytesPerRow:1440*4 bitsPerPixel:32];
    [NSGraphicsContext saveGraphicsState];
    [NSGraphicsContext setCurrentContext:[NSGraphicsContext graphicsContextWithBitmapImageRep:sample]];
    [snapshot drawInRect:NSMakeRect(0,0,1440,810) fromRect:NSZeroRect operation:NSCompositingOperationCopy
        fraction:1 respectFlipped:NO hints:nil];
    [[NSGraphicsContext currentContext] flushGraphics];
    [NSGraphicsContext restoreGraphicsState];
    NSColor *right=[[sample colorAtX:1430 y:400] colorUsingColorSpace:NSColorSpace.genericRGBColorSpace];
    NSColor *bottom=[[sample colorAtX:700 y:800] colorUsingColorSpace:NSColorSpace.genericRGBColorSpace];
    NSColor *top=[[sample colorAtX:700 y:10] colorUsingColorSpace:NSColorSpace.genericRGBColorSpace];
    fprintf(stderr,"Rendered snapshot edges: right %.2f, bottom %.2f, top %.2f\n",right.redComponent,bottom.redComponent,top.redComponent);
    NSCAssert(CGImageGetWidth(displayed)==1440 && CGImageGetHeight(displayed)==810,
        @"Wine must display the cropped viewport");
        NSCAssert(right.redComponent<.4 && bottom.redComponent<.4 && top.redComponent<.4,
            @"The snapshot must fill the displayed viewport, without white padding");
        puts("PASS: full 1440x810 window image with no right/bottom padding");
    exitlag_preserve_frame(window);
    NSView *overlay = objc_getAssociatedObject(window,&overlayKey);
    NSCAssert(overlay != nil && ![overlay hitTest:NSMakePoint(20,20)], @"Keep the animation image input-transparent");
    overlay.frame = NSMakeRect(0,0,1350,732);
    exitlag_restore_frame(window);
    NSCAssert(NSEqualRects(overlay.frame,view.bounds), @"Restore must fit the current viewport immediately");
    CFRunLoopRunInMode(kCFRunLoopDefaultMode,.3,false);
    NSCAssert(!overlay.superview, @"Restore must still remove the overlay");
    puts("PASS: immediate restore sizing, input transparency, and overlay cleanup");
 }
 return 0;
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wine-source', type=Path, required=True,
                        help='Unmodified pinned Wine 11.17 source directory')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('This check requires macOS AppKit and Xcode command-line tools')
    original = args.wine_source / 'dlls/winemac.drv/cocoa_window.m'
    if hashlib.sha256(original.read_bytes()).hexdigest() != ORIGINAL_SHA256:
        parser.error('The Wine Cocoa source differs from the pinned unmodified version')
    patch = Path(__file__).resolve().parent / 'wine-patches/0001-preserve-exitlag-cropped-window-frame.patch'
    with tempfile.TemporaryDirectory(prefix='exitlag-window-test-') as folder:
        work = Path(folder)
        target = work / 'dlls/winemac.drv/cocoa_window.m'
        target.parent.mkdir(parents=True)
        shutil.copyfile(original, target)
        subprocess.run(['patch', '-p1', '-i', str(patch)], cwd=work, check=True,
                       stdout=subprocess.DEVNULL)
        source = target.read_text()
        start = source.index('/* ExitLag-only minimize frame retention;')
        end = source.index('@implementation WineContentView', start)
        code = PREFIX + source[start:end] + MIDDLE + method(source, '- (void) updateLayer') + '\n' + \
               method(source, '- (void) setColorImage:') + SUFFIX
        fixture = work / 'window-test.m'
        fixture.write_text(code)
        binary = work / 'window-test'
        subprocess.run(['xcrun', 'clang', '-arch', 'x86_64', '-mmacosx-version-min=15.0',
                        '-fblocks', '-framework', 'AppKit', '-framework', 'QuartzCore',
                        str(fixture), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    main()
