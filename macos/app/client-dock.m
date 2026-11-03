// Draw the generated client's pinned Dock tile while ExitLag is closed.
// SPDX-License-Identifier: GPL-3.0-only
#import <AppKit/AppKit.h>

@interface ExitLagIdleIconView : NSView
@property(nonatomic, strong) NSImage *icon;
@end

@implementation ExitLagIdleIconView
- (BOOL)isOpaque { return NO; }
- (void)drawRect:(NSRect)dirtyRect
{
    [[NSColor clearColor] setFill];
    NSRectFillUsingOperation(self.bounds, NSCompositingOperationCopy);
    [self.icon drawInRect:self.bounds fromRect:NSZeroRect
               operation:NSCompositingOperationSourceOver fraction:1
          respectFlipped:YES hints:nil];
}
@end

@interface ExitLagIdleDockTile : NSObject <NSDockTilePlugIn>
@property(nonatomic, strong) NSDockTile *tile;
@end

@implementation ExitLagIdleDockTile
- (void)setDockTile:(NSDockTile *)dockTile
{
    if (self.tile) {
        self.tile.contentView = nil;
        [self.tile display];
    }
    self.tile = dockTile;
    if (!dockTile) return;
    // The plug-in only reads the generated app's marker and local logo.
    NSURL *contents = [[NSBundle bundleForClass:self.class].bundleURL
                       URLByDeletingLastPathComponent].URLByDeletingLastPathComponent;
    NSURL *resources = [contents URLByAppendingPathComponent:@"Resources" isDirectory:YES];
    NSString *marker = [NSString stringWithContentsOfURL:
        [resources URLByAppendingPathComponent:@".exitlag-bridge-client"]
        encoding:NSUTF8StringEncoding error:nil];
    if (![marker isEqualToString:@"exitlag-bridge\n"]) return;
    NSImage *image = [[NSImage alloc] initWithContentsOfURL:
        [resources URLByAppendingPathComponent:@"ClientIcon.icns"]];
    if (!image) return;
    ExitLagIdleIconView *view = [[ExitLagIdleIconView alloc]
        initWithFrame:NSMakeRect(0, 0, dockTile.size.width, dockTile.size.height)];
    view.icon = image;
    dockTile.contentView = view;
    [dockTile display];
}
@end
