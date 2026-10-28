// image-drop-watch: notice image files dropped onto a terminal window and
// open them in the herdr-image-drop popup.
//
// It polls the system drag pasteboard. When a drag that carries image file
// URLs ends (mouse buttons released) over a window owned by the terminal app,
// it runs `herdr plugin pane open ... --entrypoint viewer`. No Accessibility
// or Input Monitoring permission is needed.

import AppKit
import CoreGraphics
import Foundation

let pluginID = "herdr-image-drop"
let imageExtensions: Set<String> = [
    "png", "jpg", "jpeg", "gif", "heic", "heif", "webp", "tif", "tiff", "bmp",
]

struct Options {
    var herdr = ""
    var terminals: Set<String> = []
    var dryRun = false
}

func parseOptions(_ args: [String]) -> Options {
    var options = Options()
    var i = 1
    while i < args.count {
        let arg = args[i]
        let next = i + 1 < args.count ? args[i + 1] : ""
        switch arg {
        case "--herdr":
            options.herdr = next
            i += 1
        case "--terminal":
            options.terminals.insert(next)
            i += 1
        case "--dry-run":
            options.dryRun = true
        case "-h", "--help":
            print("usage: image-drop-watch [--herdr PATH] [--terminal BUNDLE_ID]... [--dry-run]")
            exit(0)
        default:
            FileHandle.standardError.write("unknown argument: \(arg)\n".data(using: .utf8)!)
            exit(2)
        }
        i += 1
    }
    if options.terminals.isEmpty {
        options.terminals = ["com.mitchellh.ghostty"]
    }
    if options.herdr.isEmpty {
        let candidates = [
            "/opt/homebrew/bin/herdr",
            "/usr/local/bin/herdr",
            NSHomeDirectory() + "/.local/bin/herdr",
            NSHomeDirectory() + "/.cargo/bin/herdr",
        ]
        options.herdr = candidates.first { FileManager.default.isExecutableFile(atPath: $0) } ?? "herdr"
    }
    return options
}

let logURL = URL(fileURLWithPath: NSHomeDirectory() + "/Library/Logs/herdr-image-drop.log")

func log(_ message: String) {
    let stamp = ISO8601DateFormatter().string(from: Date())
    let line = "\(stamp) \(message)\n"
    FileHandle.standardError.write(line.data(using: .utf8)!)
    if !FileManager.default.fileExists(atPath: logURL.path) {
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
    }
    if let handle = try? FileHandle(forWritingTo: logURL) {
        handle.seekToEndOfFile()
        handle.write(line.data(using: .utf8)!)
        try? handle.close()
    }
}

/// Run a command, wait for it, return (exit status, stdout).
@discardableResult
func run(_ path: String, _ args: [String]) -> (Int32, String) {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: path)
    process.arguments = args
    let out = Pipe()
    process.standardOutput = out
    process.standardError = out
    process.standardInput = FileHandle.nullDevice
    do {
        try process.run()
    } catch {
        return (-1, "\(error)")
    }
    let data = out.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    return (process.terminationStatus, String(decoding: data, as: UTF8.self))
}

/// Read viewer and max_files from the plugin's config.toml.
func loadConfig(herdr: String) -> (viewer: String, maxFiles: Int) {
    var viewer = "popup"
    var maxFiles = 4
    let (status, output) = run(herdr, ["plugin", "config-dir", pluginID])
    let dir = output.trimmingCharacters(in: .whitespacesAndNewlines)
    guard status == 0, !dir.isEmpty,
          let text = try? String(contentsOfFile: dir + "/config.toml", encoding: .utf8)
    else { return (viewer, maxFiles) }
    for raw in text.split(separator: "\n") {
        let line = raw.split(separator: "#", maxSplits: 1, omittingEmptySubsequences: false)[0]
        let parts = line.split(separator: "=", maxSplits: 1).map {
            $0.trimmingCharacters(in: CharacterSet(charactersIn: " \t\"'"))
        }
        guard parts.count == 2 else { continue }
        if parts[0] == "viewer", ["popup", "quicklook", "preview"].contains(parts[1]) {
            viewer = parts[1]
        } else if parts[0] == "max_files", let n = Int(parts[1]), n > 0 {
            maxFiles = n
        }
    }
    return (viewer, maxFiles)
}

/// Bundle id of the frontmost normal window under a point (Cocoa coordinates).
func windowOwner(at point: NSPoint) -> String? {
    guard let primary = NSScreen.screens.first else { return nil }
    // Window bounds use a top-left origin on the primary display.
    let target = CGPoint(x: point.x, y: primary.frame.maxY - point.y)
    let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
    guard let windows = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]]
    else { return nil }
    for info in windows {
        guard let layer = info[kCGWindowLayer as String] as? Int, layer == 0 else { continue }
        if let alpha = info[kCGWindowAlpha as String] as? Double, alpha == 0 { continue }
        guard let bounds = info[kCGWindowBounds as String] as? NSDictionary,
              let rect = CGRect(dictionaryRepresentation: bounds as CFDictionary),
              rect.contains(target)
        else { continue }
        guard let pid = info[kCGWindowOwnerPID as String] as? pid_t else { return nil }
        return NSRunningApplication(processIdentifier: pid)?.bundleIdentifier
    }
    return nil
}

final class Watcher {
    let options: Options
    let pasteboard = NSPasteboard(name: .drag)
    var lastChange: Int
    var pending: [URL] = []
    var pendingChange = -1

    init(options: Options) {
        self.options = options
        lastChange = pasteboard.changeCount
    }

    func imageURLs() -> [URL] {
        let read = pasteboard.readObjects(
            forClasses: [NSURL.self],
            options: [.urlReadingFileURLsOnly: true]
        ) as? [URL] ?? []
        return read.filter { imageExtensions.contains($0.pathExtension.lowercased()) }
    }

    func tick() {
        let count = pasteboard.changeCount
        if count != lastChange {
            lastChange = count
            pending = imageURLs()
            pendingChange = count
        }
        // Wait until the drag that carries images is released.
        guard !pending.isEmpty, NSEvent.pressedMouseButtons == 0 else { return }
        let urls = pending
        pending = []
        let point = NSEvent.mouseLocation
        let owner = windowOwner(at: point) ?? "none"
        guard options.terminals.contains(owner) else {
            log("drag \(pendingChange) released over \(owner), ignored")
            return
        }
        waitForFiles(urls, attempt: 0)
    }

    /// Screenshot thumbnails and other file promises can land a moment after
    /// the drop, so retry for up to one second.
    func waitForFiles(_ urls: [URL], attempt: Int) {
        let present = urls.filter { FileManager.default.fileExists(atPath: $0.path) }
        if present.count == urls.count || attempt >= 10 {
            if present.isEmpty {
                log("dropped files never appeared: \(urls.map(\.path))")
            } else {
                open(present.map(\.path))
            }
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
            self.waitForFiles(urls, attempt: attempt + 1)
        }
    }

    func open(_ allPaths: [String]) {
        let config = loadConfig(herdr: options.herdr)
        let paths = Array(allPaths.prefix(config.maxFiles))
        if options.dryRun {
            log("dry run: would open \(paths) with \(config.viewer)")
            return
        }
        if config.viewer == "preview" {
            run("/usr/bin/open", ["-a", "Preview"] + paths)
            log("opened \(paths) in Preview")
            return
        }
        if config.viewer == "popup" {
            let json = (try? JSONSerialization.data(withJSONObject: paths))
                .map { String(decoding: $0, as: UTF8.self) } ?? "[]"
            let (status, output) = run(options.herdr, [
                "plugin", "pane", "open",
                "--plugin", pluginID,
                "--entrypoint", "viewer",
                "--env", "IMAGE=\(paths[0])",
                "--env", "IMAGES=\(json)",
            ])
            if status == 0 {
                log("opened \(paths) in herdr popup")
                return
            }
            log("herdr popup failed (\(status)): \(output.trimmingCharacters(in: .whitespacesAndNewlines))")
        }
        // Quick Look blocks until its panel closes, so do not wait for it.
        let quicklook = Process()
        quicklook.executableURL = URL(fileURLWithPath: "/usr/bin/qlmanage")
        quicklook.arguments = ["-p"] + paths
        quicklook.standardOutput = FileHandle.nullDevice
        quicklook.standardError = FileHandle.nullDevice
        try? quicklook.run()
        log("opened \(paths) in Quick Look")
    }
}

let options = parseOptions(CommandLine.arguments)
let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let watcher = Watcher(options: options)
log("watching drags (herdr: \(options.herdr), terminals: \(options.terminals.sorted()), dry run: \(options.dryRun))")
let timer = Timer(timeInterval: 0.1, repeats: true) { _ in
    MainActor.assumeIsolated { watcher.tick() }
}
RunLoop.main.add(timer, forMode: .common)
RunLoop.main.run()
