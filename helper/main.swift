// herdr-spring-load: notice image files dropped onto a herdr pane and open
// them in the herdr-spring-load popup.
//
// It polls the system drag pasteboard. When a drag that carries image file
// URLs starts, it records herdr's focused pane and its recent text. When the
// drag is released over the terminal app, it opens the herdr popup only if
// that same pane now shows the drop (the file name, or a new `[Image #N]`
// placeholder from an agent). Anything else (a named or remote session, a
// plain shell, a cancelled drag) gets a local Quick Look preview instead.
// No Accessibility or Input Monitoring permission is needed.

import AppKit
import CoreGraphics
import Foundation

let pluginID = "herdr-spring-load"

struct Options {
    var herdr = ""
    var terminals: Set<String> = []
    var dryRun = false
    var verbose = false
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
        case "--verbose":
            options.verbose = true
        case "-h", "--help":
            print("usage: herdr-spring-load [--herdr PATH] [--terminal BUNDLE_ID]... [--dry-run] [--verbose]")
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

let home = ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()
let logURL = URL(fileURLWithPath: home + "/Library/Logs/herdr-spring-load.log")
let logLimit: UInt64 = 1_000_000

/// Append to the log file, starting over once it passes logLimit bytes.
func log(_ message: String) {
    let stamp = ISO8601DateFormatter().string(from: Date())
    let line = "\(stamp) \(message)\n"
    let fm = FileManager.default
    if let size = (try? fm.attributesOfItem(atPath: logURL.path))?[.size] as? UInt64, size > logLimit {
        try? fm.removeItem(at: logURL.appendingPathExtension("old"))
        try? fm.moveItem(at: logURL, to: logURL.appendingPathExtension("old"))
    }
    if !fm.fileExists(atPath: logURL.path) {
        fm.createFile(atPath: logURL.path, contents: nil)
    }
    if let handle = try? FileHandle(forWritingTo: logURL) {
        handle.seekToEndOfFile()
        handle.write(line.data(using: .utf8)!)
        try? handle.close()
    }
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

/// herdr's focused pane and its recent text when an image drag started.
struct Baseline {
    var pane: String
    var text: String
}

final class Watcher {
    let options: Options
    let pasteboard = NSPasteboard(name: .drag)
    var lastChange: Int
    var pending: [URL] = []

    // Main thread only.
    var generation = 0
    var quickLook: Process?

    // All herdr CLI calls happen here, one at a time and in drop order, so a
    // slow or stuck CLI never stalls drag polling.
    let worker = DispatchQueue(label: "herdr-spring-load.worker")
    // Worker queue only.
    var baselines: [Int: Baseline] = [:]

    let latestLock = NSLock()
    var latestGeneration = 0

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
            if !pending.isEmpty {
                generation += 1
                let gen = generation
                latestLock.lock()
                latestGeneration = gen
                latestLock.unlock()
                worker.async { self.captureBaseline(gen) }
            }
        }
        // Wait until the drag that carries images is released.
        guard !pending.isEmpty, NSEvent.pressedMouseButtons == 0 else { return }
        let urls = pending
        pending = []
        let owner = windowOwner(at: NSEvent.mouseLocation) ?? "none"
        guard options.terminals.contains(owner) else {
            if options.verbose { log("drag released over \(owner), ignored") }
            return
        }
        let gen = generation
        worker.async { self.handleDrop(urls, gen) }
    }

    func herdr(_ args: [String], timeout: TimeInterval = 2) -> CommandResult {
        runCommand(options.herdr, args, timeout: timeout)
    }

    func focusedPane() -> String? {
        let result = herdr(["pane", "current"])
        return result.status == 0 ? focusedPaneID(fromJSON: result.output) : nil
    }

    func paneText(_ pane: String) -> String? {
        let result = herdr(["pane", "read", pane, "--source", "recent-unwrapped", "--lines", "80"])
        return result.status == 0 ? result.output : nil
    }

    func captureBaseline(_ gen: Int) {
        guard let pane = focusedPane(), let text = paneText(pane) else { return }
        baselines[gen] = Baseline(pane: pane, text: text)
    }

    func isLatest(_ gen: Int) -> Bool {
        latestLock.lock()
        defer { latestLock.unlock() }
        return gen == latestGeneration
    }

    func handleDrop(_ urls: [URL], _ gen: Int) {
        let baseline = baselines[gen]
        baselines = baselines.filter { $0.key > gen }
        // Screenshot thumbnails and other file promises can land a moment
        // after the drop, so wait up to one second for them.
        var present: [URL] = []
        for attempt in 0...10 {
            present = urls.filter { FileManager.default.fileExists(atPath: $0.path) }
            if present.count == urls.count || attempt == 10 { break }
            Thread.sleep(forTimeInterval: 0.1)
        }
        guard !present.isEmpty else {
            log("dropped files never appeared: \(urls.map(\.path))")
            return
        }
        guard isLatest(gen) else {
            log("skipped an older drop because a newer drag started")
            return
        }
        let config = loadConfig()
        let paths = Array(present.map(\.path).prefix(config.maxFiles))
        if options.dryRun {
            let landed = baseline.map { confirmDrop($0, paths) } ?? false
            log("dry run: \(paths), viewer \(config.viewer), seen in herdr pane: \(landed)")
            return
        }
        switch config.viewer {
        case "preview":
            _ = runCommand("/usr/bin/open", ["-a", "Preview"] + paths, timeout: 5)
            log("opened \(paths) in Preview")
        case "popup":
            guard let baseline = baseline else {
                log("herdr not reachable when the drag started; using Quick Look")
                showQuickLook(paths)
                return
            }
            guard confirmDrop(baseline, paths) else {
                log("drop not seen in herdr pane \(baseline.pane); using Quick Look")
                showQuickLook(paths)
                return
            }
            openPopup(paths)
        default:
            showQuickLook(paths)
        }
    }

    /// Poll the pane that was focused when the drag started until it shows
    /// the drop, for at most 1.5 seconds.
    func confirmDrop(_ baseline: Baseline, _ paths: [String]) -> Bool {
        let deadline = Date().addingTimeInterval(1.5)
        repeat {
            guard focusedPane() == baseline.pane else { return false }
            if let text = paneText(baseline.pane),
               dropLanded(before: baseline.text, after: text, paths: paths) {
                return true
            }
            Thread.sleep(forTimeInterval: 0.15)
        } while Date() < deadline
        return false
    }

    func openPopup(_ paths: [String]) {
        let json = (try? JSONSerialization.data(withJSONObject: paths))
            .map { String(decoding: $0, as: UTF8.self) } ?? "[]"
        let result = herdr([
            "plugin", "pane", "open",
            "--plugin", pluginID,
            "--entrypoint", "viewer",
            "--env", "IMAGE=\(paths[0])",
            "--env", "IMAGES=\(json)",
        ], timeout: 5)
        if result.status == 0 {
            log("opened \(paths) in herdr popup")
            return
        }
        let reason = result.timedOut ? "timed out" : result.output.trimmingCharacters(in: .whitespacesAndNewlines)
        log("herdr popup failed (\(reason)); using Quick Look")
        showQuickLook(paths)
    }

    /// Read viewer and max_files from the plugin's config.toml.
    func loadConfig() -> (viewer: String, maxFiles: Int) {
        var viewer = "popup"
        var maxFiles = 4
        let result = herdr(["plugin", "config-dir", pluginID])
        let dir = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
        guard result.status == 0, !dir.isEmpty,
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

    /// Quick Look runs until its panel closes. Keep one at a time, owned by
    /// this process, so it is reaped and stopped when the helper exits.
    func showQuickLook(_ paths: [String]) {
        DispatchQueue.main.async {
            self.quickLook?.terminate()
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/qlmanage")
            process.arguments = ["-p"] + paths
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            process.terminationHandler = { finished in
                DispatchQueue.main.async {
                    if self.quickLook === finished { self.quickLook = nil }
                }
            }
            do {
                try process.run()
                self.quickLook = process
                log("opened \(paths) in Quick Look")
            } catch {
                log("Quick Look failed: \(error)")
            }
        }
    }

    func shutdown() {
        quickLook?.terminate()
        quickLook?.waitUntilExit()
    }
}

let options = parseOptions(CommandLine.arguments)
let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let watcher = Watcher(options: options)

var signalSources: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT, SIGHUP] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler {
        watcher.shutdown()
        exit(0)
    }
    source.resume()
    signalSources.append(source)
}

log("watching drags (herdr: \(options.herdr), terminals: \(options.terminals.sorted()), dry run: \(options.dryRun))")
let timer = Timer(timeInterval: 0.1, repeats: true) { _ in
    MainActor.assumeIsolated { watcher.tick() }
}
RunLoop.main.add(timer, forMode: .common)
RunLoop.main.run()
