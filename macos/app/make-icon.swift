// Original vector route mark. No vendor assets or personal data.
import AppKit
let directory = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
func draw(_ pixels: Int) -> Data {
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    let transform = NSAffineTransform()
    transform.scale(by: CGFloat(pixels) / 1024)
    transform.translateX(by: 0, yBy: 1024)
    transform.scaleX(by: 1, yBy: -1)
    transform.concat()
    let tile = NSBezierPath(roundedRect: NSRect(x: 88, y: 88, width: 848, height: 848), xRadius: 190, yRadius: 190)
    NSGradient(starting: NSColor(srgbRed: 0.16, green: 0.078, blue: 0.114, alpha: 1), ending: NSColor(srgbRed: 0.314, green: 0.078, blue: 0.145, alpha: 1))!.draw(in: tile, angle: 45)
    NSColor(srgbRed: 1, green: 0.706, blue: 0.741, alpha: 0.17).setStroke()
    tile.lineWidth = 2; tile.stroke()
    let path = NSBezierPath()
    path.move(to: NSPoint(x: 286, y: 686)); path.line(to: NSPoint(x: 286, y: 542))
    path.curve(to: NSPoint(x: 476, y: 448), controlPoint1: NSPoint(x: 286, y: 448), controlPoint2: NSPoint(x: 378, y: 448))
    path.line(to: NSPoint(x: 574, y: 448))
    path.curve(to: NSPoint(x: 738, y: 286), controlPoint1: NSPoint(x: 674, y: 448), controlPoint2: NSPoint(x: 738, y: 386))
    path.move(to: NSPoint(x: 476, y: 448))
    path.curve(to: NSPoint(x: 738, y: 686), controlPoint1: NSPoint(x: 650, y: 448), controlPoint2: NSPoint(x: 738, y: 514))
    path.lineCapStyle = .round; path.lineJoinStyle = .round
    NSColor(srgbRed: 1, green: 0.408, blue: 0.482, alpha: 0.18).setStroke(); path.lineWidth = 104; path.stroke()
    NSColor(srgbRed: 1, green: 0.945, blue: 0.949, alpha: 1).setStroke(); path.lineWidth = 58; path.stroke()
    for (x,y) in [(286.0,686.0),(738.0,286.0),(738.0,686.0)] {
        NSColor(srgbRed: 1, green: 0.424, blue: 0.502, alpha: 1).setFill()
        NSBezierPath(ovalIn: NSRect(x: x-72, y: y-72, width: 144, height: 144)).fill()
        NSColor(srgbRed: 1, green: 0.945, blue: 0.949, alpha: 1).setFill()
        NSBezierPath(ovalIn: NSRect(x: x-30, y: y-30, width: 60, height: 60)).fill()
    }
    NSGraphicsContext.restoreGraphicsState()
    return bitmap.representation(using: .png, properties: [:])!
}
for size in [16,32,128,256,512] {
    try draw(size).write(to: directory.appendingPathComponent("icon_\(size)x\(size).png"))
    try draw(size*2).write(to: directory.appendingPathComponent("icon_\(size)x\(size)@2x.png"))
}
