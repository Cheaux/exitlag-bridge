import SwiftUI
import Darwin
import UniformTypeIdentifiers

struct Status: Decodable, Sendable {
    let stage: String
    let message: String
    let updated_at: Double
    let games: [String]?
    let routing: Bool?
    let relay_active: Int?
    let bytes_received: Int?
}

func readStatus() -> Status? {
    let url = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/ExitLag Mac Bridge/launcher-status.json")
    let fd = Darwin.open(url.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
    guard fd >= 0 else { return nil }
    defer { Darwin.close(fd) }
    var info = stat()
    guard fstat(fd, &info) == 0, info.st_uid == getuid(), info.st_nlink == 1,
          info.st_mode & S_IFMT == S_IFREG, info.st_mode & 0o077 == 0, info.st_size <= 16384,
          flock(fd, LOCK_SH | LOCK_NB) == 0 else { return nil }
    defer { flock(fd, LOCK_UN) }
    var data = [UInt8](repeating: 0, count: Int(info.st_size))
    let count = data.withUnsafeMutableBytes { Darwin.pread(fd, $0.baseAddress, $0.count, 0) }
    guard count == data.count, let value = try? JSONDecoder().decode(Status.self, from: Data(data)),
          abs(Date().timeIntervalSince1970 - value.updated_at) < 4 else { return nil }
    return value
}

@MainActor final class Launcher: ObservableObject {
    @Published var status: Status?
    @Published var error: String?
    @Published var needsSetup = false
    @Published var settingUp = false
    @Published var setupMessage = "Download the Windows installer from ExitLag, then choose the .exe below."
    private var agent: Process?
    private var setup: Process?
    private let folder = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/ExitLag Mac Bridge")
    private let python = "/Library/PrivilegedHelperTools/local.cheaux.ExitLagRouting/python/bin/python3"
    private var clientInstalled: Bool {
        let manifest = folder.appendingPathComponent("setup-manifest.json")
        if FileManager.default.fileExists(atPath: manifest.path) {
            guard let data = try? Data(contentsOf: manifest),
                  let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  value["complete"] as? Bool == true else { return false }
        }
        return ["ExitLag", "Program Files/ExitLag", "Program Files (x86)/ExitLag"].contains {
            FileManager.default.fileExists(atPath: folder.appendingPathComponent("prefix/drive_c/\($0)/ExitLag.exe").path)
        } && FileManager.default.fileExists(atPath: folder.appendingPathComponent("runtime/bin/wine").path)
    }
    init() {
        Session.launcher = self
        needsSetup = !clientInstalled
        if !needsSetup { start() }
        Task {
            while !Task.isCancelled {
                if settingUp {
                    let path = folder.appendingPathComponent("setup-status.json")
                    if let bytes = try? Data(contentsOf: path),
                       let progress = try? JSONDecoder().decode(Status.self, from: bytes) {
                        setupMessage = progress.message
                    }
                    if let setup, !setup.isRunning {
                        settingUp = false
                        if setup.terminationStatus == 0 && clientInstalled {
                            needsSetup = false
                            start()
                        } else { error = "Setup did not finish. Review the message and choose the installer again to retry." }
                    }
                }
                status = await Task.detached { readStatus() }.value
                if let agent, !agent.isRunning {
                    if agent.terminationStatus == 0 { NSApp.terminate(nil) }
                    else { error = "The bridge could not finish starting. Open it again to retry." }
                }
                try? await Task.sleep(for: .seconds(1))
            }
        }
    }
    func start() {
        guard agent?.isRunning != true else { return }
        guard let entry = Bundle.main.url(forResource: "entry", withExtension: "py", subdirectory: "bridge") else {
            error = "The launcher files are missing. Reinstall ExitLag Mac Setup."; return
        }
        let folder = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/ExitLag Mac Bridge")
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            let path = folder.appendingPathComponent("launcher-agent.log")
            let fd = Darwin.open(path.path, O_WRONLY | O_APPEND | O_CREAT | O_NOFOLLOW, 0o600)
            guard fd >= 0 else { throw CocoaError(.fileWriteNoPermission) }
            let log = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
            let child = Process()
            child.executableURL = URL(fileURLWithPath: python)
            child.arguments = ["-I", entry.path, "--parent", String(getpid())]
            child.currentDirectoryURL = URL(fileURLWithPath: "/")
            child.standardInput = FileHandle.nullDevice
            child.standardOutput = log
            child.standardError = log
            try child.run()
            agent = child
            error = nil
        } catch { self.error = "ExitLag could not start: \(error.localizedDescription)" }
    }
    func chooseInstaller() {
        guard !settingUp else { return }
        if !FileManager.default.fileExists(atPath: python) {
            error = "Install ExitLag Mac Setup.pkg first to add the routing helper."; return
        }
        let panel = NSOpenPanel()
        panel.title = "Choose the official ExitLag Windows installer"
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        if let exe = UTType(filenameExtension: "exe") { panel.allowedContentTypes = [exe] }
        guard panel.runModal() == .OK, let selected = panel.url,
              let entry = Bundle.main.url(forResource: "entry", withExtension: "py", subdirectory: "bridge") else { return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = ["-I", entry.path, "--setup", "--installer", selected.path]
        process.currentDirectoryURL = URL(fileURLWithPath: "/")
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            setup = process
            settingUp = true
            setupMessage = "Preparing setup…"
            error = nil
        } catch { self.error = "Setup could not start: \(error.localizedDescription)" }
    }
    func stop() {
        if agent?.isRunning == true { agent?.terminate() }
        if setup?.isRunning == true { setup?.terminate() }
    }
    var message: String {
        if let error { return error }
        if needsSetup { return settingUp ? "Setting up ExitLag…" : "Set up ExitLag" }
        guard let status else { return "Starting the ExitLag client…" }
        if (status.relay_active ?? 0) > 0 { return "Routing game traffic through ExitLag" }
        switch status.stage {
        case "ready": return "Ready for a supported game"
        case "waiting_client": return "Connecting to the ExitLag client…"
        default: return status.message
        }
    }
    var ready: Bool { status?.routing == true && error == nil }
}

@MainActor enum Session { static weak var launcher: Launcher? }
@MainActor final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationWillTerminate(_ notification: Notification) { Session.launcher?.stop() }
}

@main struct ExitLagMacApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var launcher = Launcher()
    var body: some Scene {
        Window("ExitLag Bridge", id: "main") {
            VStack(alignment: .leading, spacing: 18) {
                HStack(spacing: 12) {
                    Image(nsImage: launcherIcon())
                        .resizable().frame(width: 45, height: 45)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("ExitLag Bridge").font(.title.bold())
                        Text("Unofficial macOS launcher").foregroundStyle(.secondary)
                    }
                }
                Label(launcher.message, systemImage: launcher.ready ? "checkmark.circle.fill" : "clock")
                    .foregroundStyle(launcher.ready ? Color.green : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Divider()
                if launcher.needsSetup {
                    Text(launcher.setupMessage).fixedSize(horizontal: false, vertical: true)
                    Text("Complete the official wizard, review its terms yourself, and leave Launch ExitLag unchecked. Sign in directly in ExitLag afterward.")
                        .font(.callout).foregroundStyle(.secondary)
                    Text("If the wizard warns that its Windows NDIS driver could not install, click OK to continue. The bridge supplies native routing instead.")
                        .font(.callout).foregroundStyle(.secondary)
                    HStack {
                        Button("Download from ExitLag") { NSWorkspace.shared.open(URL(string: "https://www.exitlag.com/download")!) }
                        Button("Choose installer…") { launcher.chooseInstaller() }.disabled(launcher.settingUp)
                    }
                    if launcher.settingUp { ProgressView().controlSize(.small) }
                } else {
                Text("Choose your game and apply its routes in the ExitLag dashboard, then launch the game.")
                    .fixedSize(horizontal: false, vertical: true)
                Text("Tested: WoW Classic Era · Forever character selection")
                    .font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if let active = launcher.status?.relay_active, active > 0 {
                    Text("\(active) active game connection\(active == 1 ? "" : "s") · \((launcher.status?.bytes_received ?? 0) / 1024) KB received")
                        .font(.callout).monospacedDigit()
                }
                HStack {
                    Text("Closing this window keeps the bridge running.").font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    if launcher.error != nil { Button("Retry") { launcher.start() } }
                }
                }
            }.padding(26).frame(width: 450)
        }.windowResizability(.contentSize)
            .defaultLaunchBehavior(launcher.needsSetup ? .presented : .suppressed).restorationBehavior(.disabled)
        MenuBarExtra {
            ExitLagMenu(launcher: launcher)
        } label: { Image(systemName: "arrow.triangle.branch") }
    }
}

struct ExitLagMenu: View {
    @ObservedObject var launcher: Launcher
    @Environment(\.openWindow) var openWindow
    var body: some View {
        Text(launcher.message)
        Button("Show routing status") { openWindow(id: "main"); NSApp.activate() }
        Divider()
        Button("Quit ExitLag Bridge") { NSApp.terminate(nil) }.keyboardShortcut("q")
    }
}

func launcherIcon() -> NSImage {
    if let url = Bundle.main.url(forResource: "BridgeIcon", withExtension: "icns"),
       let image = NSImage(contentsOf: url) { return image }
    return NSImage(systemSymbolName: "arrow.triangle.branch", accessibilityDescription: "ExitLag Bridge") ?? NSImage()
}
