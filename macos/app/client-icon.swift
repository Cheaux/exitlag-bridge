// Set the generated launcher's Finder/Dock image without changing its code.
// SPDX-License-Identifier: GPL-3.0-only
import AppKit

let arguments = CommandLine.arguments
guard arguments.count == 3, ["clear", "apply"].contains(arguments[1]) else { exit(2) }
let app = URL(fileURLWithPath: arguments[2], isDirectory: true)
let marker = app.appendingPathComponent("Contents/Resources/.exitlag-bridge-client")
guard app.lastPathComponent == "ExitLag.app",
      (try? String(contentsOf: marker, encoding: .utf8)) == "exitlag-bridge\n" else { exit(2) }
var image: NSImage?
if arguments[1] == "apply" {
    image = NSImage(contentsOf: app.appendingPathComponent("Contents/Resources/ClientIcon.icns"))
    guard image != nil else { exit(1) }
}
guard NSWorkspace.shared.setIcon(image, forFile: app.path, options: []) else { exit(1) }
