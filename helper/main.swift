// herdr-spring-load: spring-loaded drag for herdr in Ghostty.
//
// While a file drag is live, it watches where the cursor sits over a Ghostty
// window that runs a local herdr client:
//   - over a tab label in herdr's tab row for 600 ms: clicks that tab
//     (a synthetic click through Ghostty's AppleScript, so herdr does its own
//     hit-testing for the client that shows it);
//   - over a workspace label in the sidebar for 600 ms: clicks that space;
//   - over a pane for 250 ms: focuses that pane by sending the private
//     ctrl+alt+shift+F8..F11 aliases of herdr's native focus_pane_* keys to
//     the receiving client, one verified step at a time.
// The drop itself is untouched; Ghostty pastes the path into the focused pane.
//
// Needs Accessibility (window and text-area geometry, tab row text) and
// Automation for Ghostty (tab clicks). No Input Monitoring: it polls.

import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

let ghosttyBundle = "com.mitchellh.ghostty"

struct Options {
    var session: String?
    var tabDwell: TimeInterval = 0.6
    var paneDwell: TimeInterval = 0.25
    var tabBar: TabBarPosition = .top
    var dryRun = false
    var verbose = false
    var validateOnly = false
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(2)
}

func parseMilliseconds(_ text: String, _ flag: String) -> TimeInterval {
    guard let ms = Int(text), ms >= 50, ms <= 5000 else { fail("\(flag) wants 50..5000 ms") }
    return TimeInterval(ms) / 1000
}

let usage = """
usage: herdr-spring-load [--session NAME] [--tab-dwell-ms N]
                         [--pane-dwell-ms N] [--tab-bar top|bottom] [--dry-run] [--verbose]
"""

func parseOptions(_ args: ArraySlice<String>) -> Options {
    var options = Options()
    var i = args.startIndex
    while i < args.endIndex {
        let arg = args[i]
        let next = i + 1 < args.endIndex ? args[i + 1] : ""
        if ["--session", "--tab-dwell-ms", "--pane-dwell-ms", "--tab-bar"].contains(arg) && (next.isEmpty || next.hasPrefix("--")) { fail("missing value for \(arg)") }
        switch arg {
        case "--session": options.session = next; i += 1
        case "--tab-dwell-ms": options.tabDwell = parseMilliseconds(next, arg); i += 1
        case "--pane-dwell-ms": options.paneDwell = parseMilliseconds(next, arg); i += 1
        case "--tab-bar":
            guard let position = TabBarPosition(rawValue: next) else { fail("--tab-bar wants top or bottom") }
            options.tabBar = position
            i += 1
        case "--dry-run": options.dryRun = true
        case "--verbose": options.verbose = true
        case "--validate": options.validateOnly = true
        case "-h", "--help": print(usage); exit(0)
        default: fail("unknown argument: \(arg)\n\(usage)")
        }
        i += 1
    }
    return options
}

let home = ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()
let logURL = URL(fileURLWithPath: home + "/Library/Logs/herdr-spring-load.log")
let logLimit: UInt64 = 1_000_000
let logLock = NSLock()

/// Append to the log file, starting over once it passes logLimit bytes.
func log(_ message: String) {
    logLock.lock()
    defer { logLock.unlock() }
    let line = "\(ISO8601DateFormatter().string(from: Date())) \(message)\n"
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

// MARK: Screen and window lookup

/// Cursor in global top-left coordinates (CGWindow and AX use these).
func cursorPoint() -> CGPoint {
    let point = NSEvent.mouseLocation
    let height = NSScreen.screens.first?.frame.maxY ?? 0
    return CGPoint(x: point.x, y: height - point.y)
}

struct WindowHit: Equatable {
    let number: Int
    let pid: pid_t
    let bundle: String
    let bounds: CGRect
}

/// Frontmost normal window under a point.
func windowUnder(_ point: CGPoint) -> WindowHit? {
    let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
    guard let windows = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]]
    else { return nil }
    for info in windows {
        guard let layer = info[kCGWindowLayer as String] as? Int, layer == 0 else { continue }
        if let alpha = info[kCGWindowAlpha as String] as? Double, alpha == 0 { continue }
        guard let boundsInfo = info[kCGWindowBounds as String] as? NSDictionary,
              let bounds = CGRect(dictionaryRepresentation: boundsInfo as CFDictionary),
              bounds.contains(point) else { continue }
        guard let pid = info[kCGWindowOwnerPID as String] as? pid_t,
              let number = info[kCGWindowNumber as String] as? Int else { return nil }
        let bundle = NSRunningApplication(processIdentifier: pid)?.bundleIdentifier ?? ""
        return WindowHit(number: number, pid: pid, bundle: bundle, bounds: bounds)
    }
    return nil
}

// A nonce-bound keybinding probe establishes the invoking client's context.
// No click is permitted from a size/title guess alone.
let probeBase = URL(fileURLWithPath: home + "/Library/Application Support/herdr-spring-load/probes")

struct DragContext {
    let window: WindowHit
    let title: String
    let terminal: String
    let geometry: CellGeometry
    let textArea: AXUIElement
    let ttyPath: String
    let socket: String
    let layout: HitLayout
    let tabs: [TabInfo]
    let spaces: [SpaceInfo]
}

func foregroundClient(_ pid: Int32, tty path: String) -> Bool {
    let fd = open(path, O_RDONLY | O_NOCTTY | O_NONBLOCK)
    guard fd >= 0 else { return false }
    defer { close(fd) }
    let group = tcgetpgrp(fd)
    return group > 0 && group == getpgid(pid)
}

func sessionSocket(_ session: String?) -> String? {
    let configHome = ProcessInfo.processInfo.environment["XDG_CONFIG_HOME"]
    let base = configHome.map { URL(fileURLWithPath: $0).appendingPathComponent("herdr").path }
    guard let normal = socketPath(session: session, home: home) else { return nil }
    return base.map { normal.replacingOccurrences(of: home + "/.config/herdr", with: $0) } ?? normal
}

final class ClickSafety {
    let lock = NSLock()
    var terminal: String?
    func arm(_ value: String?) { lock.lock(); terminal = value; lock.unlock() }
    func release() {
        lock.lock(); let value = terminal; terminal = nil; lock.unlock()
        guard let value, isSafeTerminalID(value) else { return }
        _ = runCommand("/usr/bin/osascript", ["-e", "tell application \"Ghostty\" to send mouse button left button action release to terminal id \"\(value)\""], timeout: 0.3)
    }
}
let clickSafety = ClickSafety()

func ghosttyWindows() throws -> [GhosttyWindow] {
    // JSON, not delimiter-separated untrusted titles. Titles are never source.
    let script = """
    const app = Application('Ghostty');
    JSON.stringify(app.windows().map(w => {
        const t = w.selectedTab();
        return {id:w.id(), name:w.name(), terminals:t.terminals().length,
                terminal:t.focusedTerminal().id()};
    }));
    """
    let result = runCommand("/usr/bin/osascript", ["-l", "JavaScript", "-e", script], timeout: 1)
    guard result.status == 0, let data = result.output.data(using: .utf8),
          let records = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else {
        throw GateError(description: "Ghostty scripting unavailable; enable Automation and macos-applescript")
    }
    return records.compactMap {
        guard let id = $0["id"] as? String, let name = $0["name"] as? String,
              let n = $0["terminals"] as? Int, let terminal = $0["terminal"] as? String else { return nil }
        return GhosttyWindow(id: id, name: name, terminals: n, focusedTerminal: terminal)
    }
}

/// Every terminal id Ghostty still has, in any window or tab.
func ghosttyTerminalIDs() throws -> Set<String> {
    let script = "JSON.stringify(Application('Ghostty').terminals().map(t => t.id()))"
    let result = runCommand("/usr/bin/osascript", ["-l", "JavaScript", "-e", script], timeout: 1)
    guard result.status == 0, let data = result.output.data(using: .utf8),
          let ids = try? JSONSerialization.jsonObject(with: data) as? [String] else {
        throw GateError(description: "Ghostty scripting unavailable; enable Automation and macos-applescript")
    }
    return Set(ids)
}

/// Press and release one key in a Ghostty terminal. The release matters when
/// the host reports key events: herdr would otherwise hold a press lease.
func sendKey(_ key: String, to terminal: String, timeout: TimeInterval, cancelled: () -> Bool) -> Bool {
    guard isSafeTerminalID(terminal) else { return false }
    let source = """
    tell application "Ghostty"
        set t to terminal id "\(terminal)"
        try
            send key "\(key)" action press modifiers "control,option,shift" to t
        end try
        send key "\(key)" action release modifiers "control,option,shift" to t
    end tell
    """
    return runCommand("/usr/bin/osascript", ["-e", source], timeout: timeout, cancelled: cancelled).status == 0
}

// Slots are pinned to terminal IDs. A slot is freed only once its terminal is
// gone from Ghostty (so its herdr client is gone too), and is reused only
// after a quarantine well past the probe's 3 s request validity, with its
// request and reply files deleted. A delayed action from window A therefore
// cannot answer window B's challenge.
let probeSlotCount = 7
let probeQuarantine: TimeInterval = 10
var probeSlots: [String: Int] = [:]
var probeFreedAt: [Int: TimeInterval] = [:]
func probeSlot(_ terminal: String) throws -> Int {
    if let slot = probeSlots[terminal] { return slot }
    let now = ProcessInfo.processInfo.systemUptime
    if probeSlots.count + probeFreedAt.count >= probeSlotCount {
        let live = try ghosttyTerminalIDs()
        for (gone, slot) in probeSlots where !live.contains(gone) {
            probeSlots.removeValue(forKey: gone)
            for name in ["request-\(slot).json", "reply-\(slot).json"] {
                try? FileManager.default.removeItem(at: probeBase.appendingPathComponent(name))
            }
            probeFreedAt[slot] = now
        }
    }
    let used = Set(probeSlots.values)
    let free = (1...probeSlotCount).first { slot in
        !used.contains(slot) && probeFreedAt[slot].map { now - $0 >= probeQuarantine } ?? true
    }
    guard let slot = free else {
        log("all \(probeSlotCount) probe slots are in use by open Ghostty terminals; spring-loading is paused for this one")
        throw GateError(description: "no free probe slot")
    }
    probeFreedAt.removeValue(forKey: slot)
    probeSlots[terminal] = slot
    return slot
}

// Probes run on the serial worker queue; the lock keeps the backoff safe anyway.
var probeBackoff = ProbeBackoff()
let probeBackoffLock = NSLock()

func probe(terminal: String, cancelled: () -> Bool) throws -> [String: Any] {
    guard isSafeTerminalID(terminal), !cancelled() else { throw GateError(description: "cancelled") }
    probeBackoffLock.lock()
    let allowed = probeBackoff.allows(at: ProcessInfo.processInfo.systemUptime)
    probeBackoffLock.unlock()
    guard allowed else { throw GateError(description: "probes paused after unanswered probe keys") }
    try FileManager.default.createDirectory(at: probeBase, withIntermediateDirectories: true,
                                          attributes: [.posixPermissions: 0o700])
    let slot = try probeSlot(terminal)
    let token = UUID().uuidString
    let request: [String: Any] = ["token": token, "time": Date().timeIntervalSince1970]
    try JSONSerialization.data(withJSONObject: request).write(to: probeBase.appendingPathComponent("request-\(slot).json"), options: .atomic)
    guard sendKey("f\(slot)", to: terminal, timeout: 1, cancelled: cancelled) else {
        throw GateError(description: "probe key could not be sent")
    }
    let deadline = Date().addingTimeInterval(2)
    repeat {
        guard !cancelled() else { throw GateError(description: "cancelled") }
        if let data = try? Data(contentsOf: probeBase.appendingPathComponent("reply-\(slot).json")),
           let reply = try? JSONSerialization.jsonObject(with: data) as? [String: Any], reply["token"] as? String == token {
            probeBackoffLock.lock(); probeBackoff.answered(); probeBackoffLock.unlock()
            return reply
        }
        Thread.sleep(forTimeInterval: 0.02)
    } while Date() < deadline
    probeBackoffLock.lock()
    let paused = probeBackoff.miss(at: ProcessInfo.processInfo.systemUptime)
    probeBackoffLock.unlock()
    if paused {
        log("\(probeBackoff.limit) probe keys went unanswered; no probe keys for \(Int(probeBackoff.pause)) s. Is the plugin enabled? Check: herdr plugin list")
    }
    throw GateError(description: "probe timed out; run setup to register the private binding")
}

// MARK: Plugin registration (herdr has no uninstall hook)

let pluginID = "herdr-spring-load"
let supportURL = URL(fileURLWithPath: home + "/Library/Application Support/herdr-spring-load")
let seenMarker = supportURL.appendingPathComponent("registered-seen")

func herdrConfigDir() -> String {
    if let base = ProcessInfo.processInfo.environment["XDG_CONFIG_HOME"], !base.isEmpty { return base + "/herdr" }
    return home + "/.config/herdr"
}

func currentRegistration() -> PluginRegistration {
    let data = try? Data(contentsOf: URL(fileURLWithPath: herdrConfigDir() + "/plugins.json"))
    return pluginRegistration(data, pluginID: pluginID, fileExists: { FileManager.default.fileExists(atPath: $0) })
}

/// The command that removes the helper, its keybindings and LaunchAgent. setup
/// keeps a copy of the scripts in Application Support, because herdr deletes
/// the plugin checkout on uninstall.
func uninstallCommand() -> [String]? {
    guard let data = try? Data(contentsOf: supportURL.appendingPathComponent("state.json")),
          let state = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let python = state["python"] as? String, let script = state["uninstall"] as? String,
          FileManager.default.isExecutableFile(atPath: python), FileManager.default.fileExists(atPath: script) else { return nil }
    return [python, script]
}

func shellQuoted(_ argv: [String]) -> String {
    argv.map { "'" + $0.replacingOccurrences(of: "'", with: "'\\''") + "'" }.joined(separator: " ")
}

func notify(_ message: String) {
    let text = message.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
    _ = runCommand("/usr/bin/osascript", ["-e", "display notification \"\(text)\" with title \"herdr-spring-load\""], timeout: 2)
}

final class RegistrationGate {
    private var reported: PluginRegistration?

    /// Checked when a drag starts: true when the helper may act.
    func permits() -> Bool {
        let state = currentRegistration()
        if state == .registered && !FileManager.default.fileExists(atPath: seenMarker.path) {
            FileManager.default.createFile(atPath: seenMarker.path, contents: nil)
        }
        let action = registrationAction(state, seenRegistered: FileManager.default.fileExists(atPath: seenMarker.path))
        defer { reported = state }
        switch action {
        case .run:
            return true
        case .remove:
            removeSelf()
            return false
        case .pause:
            guard reported != state else { return false }
            let finish = uninstallCommand().map(shellQuoted) ?? "sh helper/uninstall.sh (from the plugin source)"
            let why: String
            switch state {
            case .disabled: why = "the plugin is disabled in herdr"
            case .manifestMissing: why = "herdr's plugin entry points to a manifest that no longer exists"
            case .unreadable: why = "herdr's plugins.json could not be read"
            default: why = "the plugin is not registered with herdr"
            }
            let message = "Spring-loading is paused: \(why). Re-enable it with: herdr plugin enable \(pluginID). Or remove the helper and its keybindings with: \(finish)"
            log(message)
            notify(message)
            return false
        }
    }

    private func removeSelf() {
        guard let command = uninstallCommand() else {
            let message = "herdr-spring-load was uninstalled from herdr, but its removal script is missing. Remove the helper with: launchctl bootout gui/\(getuid())/dev.qcklex.herdr-spring-load, then delete the herdr-spring-load block from your herdr config."
            log(message)
            if reported != .missing { notify(message) }
            return
        }
        log("the plugin was uninstalled from herdr; removing the helper, its keybindings and LaunchAgent")
        // uninstall.py stops this LaunchAgent last, so everything else is done first.
        let result = runCommand(command[0], Array(command.dropFirst()) + ["--self"], timeout: 120)
        if result.status != 0 {
            let message = "herdr-spring-load could not remove itself (\(result.output.suffix(300))). Finish with: \(shellQuoted(command))"
            log(message)
            notify(message)
            return
        }
        log("helper removed")
        exit(0)
    }
}
let registrationGate = RegistrationGate()

final class Watcher {
    let options: Options
    let pasteboard = NSPasteboard(name: .drag)
    var lastChange: Int
    var dragging = false
    var generation = 0
    var epoch = 0
    var busy = false
    /// Drag generation whose tab/space clicks are halted after a misdirected click.
    var clicksHalted = 0
    var retryAfter: TimeInterval = 0
    var firedAt: CGPoint?
    var context: DragContext?
    var dwell: DwellMachine
    var target: HoverTarget = .none
    let worker = DispatchQueue(label: "herdr-spring-load.worker")
    let lock = NSLock()
    var liveGeneration = 0
    var liveEpoch = 0
    var liveWindow: WindowHit?
    var livePoint = CGPoint.zero

    init(options: Options) {
        self.options = options
        lastChange = pasteboard.changeCount
        dwell = DwellMachine(tabDwell: options.tabDwell, paneDwell: options.paneDwell)
    }

    func valid(_ gen: Int, _ actionEpoch: Int? = nil) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return gen != 0 && gen == liveGeneration && (actionEpoch == nil || liveEpoch == actionEpoch)
            && CGEventSource.buttonState(.combinedSessionState, button: .left)
    }

    func invalidate() {
        generation += 1
        epoch += 1
        context = nil
        firedAt = nil
        target = .none
        dwell.reset()
        lock.lock(); liveGeneration = dragging ? generation : 0; liveEpoch = epoch; lock.unlock()
    }

    func tick() {
        let count = pasteboard.changeCount
        let held = NSEvent.pressedMouseButtons & 1 != 0
        if count != lastChange {
            lastChange = count
            let types = pasteboard.types?.map(\.rawValue) ?? []
            let files = types.contains { $0 == "public.file-url" || $0.lowercased().contains("filepromise") || $0.lowercased().contains("promised-file") }
            if held && files && registrationGate.permits() { dragging = true; invalidate() }
        }
        guard dragging else { return }
        if !held {
            dragging = false
            invalidate()
            return // Native Ghostty drag/drop is never consumed or replaced.
        }
        let point = cursorPoint()
        let hit = windowUnder(point)
        lock.lock(); liveWindow = hit; livePoint = point; lock.unlock()
        guard let window = hit, window.bundle == ghosttyBundle else {
            setTarget(.none)
            return
        }
        guard let ctx = context, ctx.window == window else {
            setTarget(.none)
            if !busy && ProcessInfo.processInfo.systemUptime >= retryAfter {
                busy = true
                let gen = generation
                worker.async {
                    do {
                        let ctx = try self.resolve(window, gen)
                        DispatchQueue.main.async {
                            self.busy = false
                            if self.valid(gen), self.liveWindow == window { self.context = ctx }
                        }
                    } catch {
                        if self.options.verbose { log("context unavailable: \(error)") }
                        DispatchQueue.main.async { self.busy = false; self.retryAfter = ProcessInfo.processInfo.systemUptime + 1 }
                    }
                }
            }
            return
        }
        guard let cell = ctx.geometry.cell(at: point) else { setTarget(.none); return }
        // Reading AX IPC on the worker keeps the drag timer responsive.
        // Target map is refreshed after every navigation action.
        let next: HoverTarget
        lock.lock(); let halted = clicksHalted == generation; lock.unlock()
        if halted, cell.column < ctx.layout.sidebarColumns || ctx.layout.tabRow == cell.row {
            next = .none
        } else if cell.column < ctx.layout.sidebarColumns {
            next = sidebarTargets.first { $0.row == cell.row && $0.contains(cell.column) }
                .map { .space(row: $0.row, workspaceID: $0.workspaceID) } ?? .none
        } else {
            next = hoverTarget(cell: cell, hit: ctx.layout)
        }
        setTarget(next)
        if let fired = firedAt {
            if abs(fired.x - point.x) < ctx.geometry.cellWidth && abs(fired.y - point.y) < ctx.geometry.cellHeight { return }
            firedAt = nil
        }
        if let fire = dwell.update(next, at: ProcessInfo.processInfo.systemUptime), !busy {
            busy = true
            firedAt = point
            let gen = generation, ticket = epoch
            worker.async {
                do { try self.act(fire, ctx, gen, ticket) }
                catch { if self.options.verbose { log("action skipped: \(error)") } }
                DispatchQueue.main.async {
                    self.busy = false
                    // Re-probe after each operation so spaces/tabs use their new layout.
                    if self.valid(gen) { self.context = nil; self.sidebarTargets = []; self.dwell.reset() }
                }
            }
        }
    }

    var sidebarTargets: [SpaceSpan] = []
    func setTarget(_ next: HoverTarget) {
        if !DwellMachine.sameSpot(target, next) {
            epoch += 1
            lock.lock(); liveEpoch = epoch; lock.unlock()
        }
        target = next
        if next == .none { _ = dwell.update(.none, at: ProcessInfo.processInfo.systemUptime) }
    }

    func areaFor(_ window: WindowHit) throws -> (AXUIElement, CGRect, String) {
        guard AXIsProcessTrusted() else { throw GateError(description: "Accessibility permission required") }
        let app = AXUIElementCreateApplication(window.pid)
        // AX IPC must not block the worker indefinitely if Ghostty is busy.
        AXUIElementSetMessagingTimeout(app, 0.3)
        let windows = axAttribute(app, kAXWindowsAttribute) as? [AXUIElement] ?? []
        let matches = windows.filter { axFrame($0).map {
            abs($0.minX - window.bounds.minX) <= 2 && abs($0.minY - window.bounds.minY) <= 2
                && abs($0.width - window.bounds.width) <= 2 && abs($0.height - window.bounds.height) <= 2
        } ?? false }
        guard matches.count == 1 else { throw GateError(description: "ambiguous AX window") }
        var areas: [(element: AXUIElement, frame: CGRect)] = []
        collectTextAreas(in: matches[0], depth: 6, into: &areas)
        guard areas.count == 1 else { throw GateError(description: "Ghostty native splits are unsupported") }
        return (areas[0].element, areas[0].frame, axAttribute(matches[0], kAXTitleAttribute) as? String ?? "")
    }

    func resolve(_ window: WindowHit, _ gen: Int) throws -> DragContext {
        let (area, frame, title) = try areaFor(window)
        guard let terminal = terminalForWindow(titled: title, in: try ghosttyWindows()) else {
            throw GateError(description: "no uniquely identified Ghostty terminal")
        }
        // The probe key is typed into the terminal unless a herdr client receives
        // it, so confirm the window runs one before sending anything.
        let ps = runCommand("/bin/ps", ["-axo", "pid=,ppid=,tty=,args="], timeout: 0.6, cancelled: { !self.valid(gen) })
        let entries = parsePS(ps.output)
        let observed = herdrClients(in: entries, under: window.pid).map { candidate in
            (candidate: candidate,
             fits: (try? ttyGrid(path: candidate.ttyPath)).map {
                deriveScalePadding(axWidth: frame.width, axHeight: frame.height, contentPixelsX: $0.xpixel, contentPixelsY: $0.ypixel) != nil
             } == true,
             foreground: foregroundClient(candidate.pid, tty: candidate.ttyPath))
        }
        guard let candidate = probeCandidate(observed) else {
            throw GateError(description: "no single local herdr client fits this window; probe key not sent")
        }
        guard case .local(let session) = candidate.client else { throw GateError(description: "remote client") }
        let reply = try probe(terminal: terminal, cancelled: { !self.valid(gen) })
        guard let socket = reply["socket"] as? String, let raw = reply["layout"] as? [String: Any],
              let tabs = reply["tabs"] as? [String: Any], let spaces = reply["spaces"] as? [String: Any] else {
            throw GateError(description: "invalid client probe")
        }
        // The probe proves the receiving session; it must be the client found above.
        guard sessionSocket(session) == socket else { throw GateError(description: "probe answered from another session") }
        if let requested = options.session, sessionSocket(requested) != socket {
            throw GateError(description: "receiving client belongs to a different session")
        }
        let grid = try ttyGrid(path: candidate.ttyPath)
        guard let derived = deriveScalePadding(axWidth: frame.width, axHeight: frame.height,
                                               contentPixelsX: grid.xpixel, contentPixelsY: grid.ypixel) else {
            throw GateError(description: "client geometry changed")
        }
        let geometry = CellGeometry(frame: frame, columns: grid.columns, rows: grid.rows,
                                    padding: CGSize(width: derived.paddingX, height: derived.paddingY))
        let payload = try JSONSerialization.data(withJSONObject: ["result": raw])
        let layout = try parsePaneLayout(payload, gridColumns: grid.columns, gridRows: grid.rows)
        guard let tabList = parseTabList(try JSONSerialization.data(withJSONObject: ["result": tabs])) else {
            throw GateError(description: "no client tabs")
        }
        guard let spaceList = parseSpaceList(spaces) else { throw GateError(description: "no client spaces") }
        let ctx = DragContext(window: window, title: title, terminal: terminal, geometry: geometry,
                              textArea: area, ttyPath: candidate.ttyPath, socket: socket,
                              layout: hitLayout(layout, gridRows: grid.rows, position: options.tabBar), tabs: tabList, spaces: spaceList)
        let screen = axAttribute(area, kAXValueAttribute) as? String ?? ""
        let targets = spaceSpans(screen: screen, rows: grid.rows, sidebar: ctx.layout.sidebarColumns, spaces: spaceList)
        DispatchQueue.main.async { if self.valid(gen) { self.sidebarTargets = targets } }
        return ctx
    }

    func act(_ target: HoverTarget, _ ctx: DragContext, _ gen: Int, _ ticket: Int) throws {
        let cancelled = { !self.valid(gen, ticket) }
        guard !cancelled() else { return }
        let (area, frame, title) = try areaFor(ctx.window)
        guard frame == ctx.geometry.frame, title == ctx.title,
              terminalForWindow(titled: title, in: try ghosttyWindows()) == ctx.terminal else { return }
        let grid = try ttyGrid(path: ctx.ttyPath)
        guard grid.columns == ctx.geometry.columns, grid.rows == ctx.geometry.rows else { return }
        // A fresh nonce-bound probe prevents using a stale tab/space after manual navigation.
        let fresh = try probe(terminal: ctx.terminal, cancelled: cancelled)
        guard fresh["socket"] as? String == ctx.socket,
              let raw = fresh["layout"] as? [String: Any],
              let _ = raw["layout"] as? [String: Any] else { return }
        let before = try JSONSerialization.data(withJSONObject: ["result": raw])
        let layout = try parsePaneLayout(before, gridColumns: grid.columns, gridRows: grid.rows)
        guard hitLayout(layout, gridRows: grid.rows, position: options.tabBar) == ctx.layout else { return }
        let screen = axAttribute(area, kAXValueAttribute) as? String ?? ""
        lock.lock(); let point = livePoint; let window = liveWindow; lock.unlock()
        guard window == ctx.window, let cell = ctx.geometry.cell(at: point), !cancelled() else { return }
        let click: (Int, Int)?
        let arrival: (String, String) // what must be active after the click
        switch target {
        case .none: return
        case .pane(let pane):
            guard pane != layout.focusedPaneID,
                  ctx.layout.layout.pane(atCell: cell)?.paneID == pane else { return }
            if options.dryRun { log("dry run: focus pane \(pane)"); return }
            let route = try focusRoute(socket: ctx.socket, from: layout.focusedPaneID, to: pane, cancelled: cancelled)
            var expected = layout.focusedPaneID
            for step in route {
                guard !cancelled() else { return }
                let key = ["left":"f8", "right":"f9", "up":"f10", "down":"f11"][step.direction]!
                guard sendKey(key, to: ctx.terminal, timeout: 0.5, cancelled: cancelled) else {
                    throw GateError(description: "pane navigation not delivered")
                }
                let after = try probe(terminal: ctx.terminal, cancelled: cancelled)
                guard after["socket"] as? String == ctx.socket, after["pane"] as? String == step.pane else {
                    log("pane navigation stopped: herdr moved focus to \(after["pane"] as? String ?? "?") instead of \(step.pane)")
                    throw GateError(description: "pane topology changed during navigation")
                }
                expected = step.pane
            }
            if expected == pane { log("focused destination pane through receiving client") }
            return
        case .space(let row, let workspaceID):
            guard cell.row == row, let spaces = parseSpaceList(fresh["spaces"]),
                  spaces.first(where: { $0.workspaceID == workspaceID })?.focused == false,
                  let span = spaceSpans(screen: screen, rows: grid.rows, sidebar: ctx.layout.sidebarColumns, spaces: spaces)
                    .first(where: { $0.row == row && $0.workspaceID == workspaceID && $0.contains(cell.column) }) else { return }
            click = ((span.start + span.end - 1) / 2, row)
            arrival = ("workspace", workspaceID)
        case .tabRow(let firedColumn, let row):
            guard cell.row == row, abs(cell.column - firedColumn) <= 1, let text = rowText(fromScreen: screen, rows: grid.rows, row: row),
                  let tabsRaw = fresh["tabs"] as? [String: Any],
                  let tabs = parseTabList(try JSONSerialization.data(withJSONObject: ["result": tabsRaw])),
                  let span = tabSpans(rowText: text, tabs: tabs, startColumn: ctx.layout.sidebarColumns)
                    .first(where: { $0.contains(cell.column) && $0.contains(firedColumn) }),
                  tabs.first(where: { $0.tabID == span.tabID })?.focused != true else { return }
            click = ((span.start + span.end - 1) / 2, row)
            arrival = ("tab", span.tabID)
        }
        guard let (column, row) = click, !cancelled(),
              windowUnder(CGEvent(source: nil)?.location ?? CGPoint(x: -1, y: -1)) == ctx.window else { return }
        if options.dryRun { log("dry run: navigate \(target)"); return }
        let location = ctx.geometry.surfacePoint((column, row))
        let x = String(format: "%.1f", locale: Locale(identifier: "en_US_POSIX"), location.x)
        let y = String(format: "%.1f", locale: Locale(identifier: "en_US_POSIX"), location.y)
        let source = """
        tell application "Ghostty"
            set t to terminal id "\(ctx.terminal)"
            send mouse position x \(x) y \(y) to t
            try
                send mouse button left button action press to t
                send mouse button left button action release to t
            on error
                send mouse button left button action release to t
            end try
        end tell
        """
        clickSafety.arm(ctx.terminal)
        let result = runCommand("/usr/bin/osascript", ["-e", source], timeout: 0.6, cancelled: cancelled)
        if result.status != 0 { clickSafety.release() } else { clickSafety.arm(nil) }
        if result.status != 0 { throw GateError(description: "navigation not delivered") }
        Thread.sleep(forTimeInterval: 0.15)
        // Herdr does its own hit-testing; prove it landed where the map said.
        let after = try probe(terminal: ctx.terminal, cancelled: { !self.valid(gen) })
        let landed: String?
        if arrival.0 == "workspace" {
            landed = after["workspace"] as? String
        } else {
            landed = ((after["layout"] as? [String: Any])?["layout"] as? [String: Any])?["tab_id"] as? String
        }
        guard after["socket"] as? String == ctx.socket, landed == arrival.1 else {
            log("navigation landed on \(arrival.0) \(landed ?? "?") instead of \(arrival.1); tab and space navigation is off for this drag")
            lock.lock(); clicksHalted = gen; lock.unlock()
            return
        }
        log("spring-loaded navigation delivered")
    }
}

let options = parseOptions(CommandLine.arguments.dropFirst())
if options.validateOnly { exit(0) }
let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let watcher = Watcher(options: options)
// Advisory lock prevents two helpers from racing on the private probe channel.
let support = probeBase.deletingLastPathComponent()
try FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
let lockFD = open(support.appendingPathComponent("helper.lock").path, O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)
guard lockFD >= 0, flock(lockFD, LOCK_EX | LOCK_NB) == 0 else { fail("herdr-spring-load is already running") }
var signals: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT, SIGHUP] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler {
        watcher.dragging = false
        watcher.invalidate()
        commandRegistry.stopAll()
        clickSafety.release()
        close(lockFD)
        exit(0)
    }
    source.resume(); signals.append(source)
}
if !AXIsProcessTrusted() {
    // Adds the helper to System Settings > Privacy > Accessibility and shows
    // macOS's one consent prompt; tick() keeps waiting until it is granted.
    let prompt = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
    _ = AXIsProcessTrustedWithOptions(prompt)
    log("asked macOS for Accessibility; spring-loading starts once it is granted")
}
log("watching file drags; tabs and spaces \(Int(options.tabDwell * 1000)) ms, panes \(Int(options.paneDwell * 1000)) ms")
let timer = Timer(timeInterval: 0.05, repeats: true) { _ in
    MainActor.assumeIsolated { watcher.tick() }
}
RunLoop.main.add(timer, forMode: .common)
RunLoop.main.run()
