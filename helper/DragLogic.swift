// Pure logic for herdr-spring-load, kept free of AppKit so it can be unit
// tested: the hover dwell state machine, exact tab-box and sidebar-space
// matching, cell hit-testing against a herdr layout, herdr client discovery
// from ps output, plugin registration checks, and the bounded command runner.

import CoreGraphics
import Foundation

// MARK: Hover targets and dwell

enum HoverTarget: Equatable {
    case none
    /// A cell in herdr's tab row. Whether it is on a tab label is decided
    /// when the dwell fires, from the row's text.
    case tabRow(column: Int, row: Int)
    case pane(String)
    case space(row: Int, workspaceID: String)
}

/// Spring-loading: a target fires once after the cursor has stayed on it for
/// its dwell time, and re-arms only after the cursor leaves it. In the tab
/// row "staying" means the same row and within one column of where the
/// dwell started.
struct DwellMachine {
    var tabDwell: TimeInterval = 0.6
    var paneDwell: TimeInterval = 0.25

    private(set) var anchor: HoverTarget = .none
    private var since: TimeInterval = 0
    private var fired = false

    init(tabDwell: TimeInterval = 0.6, paneDwell: TimeInterval = 0.25) {
        self.tabDwell = tabDwell
        self.paneDwell = paneDwell
    }

    static func sameSpot(_ a: HoverTarget, _ b: HoverTarget) -> Bool {
        switch (a, b) {
        case let (.tabRow(c1, r1), .tabRow(c2, r2)):
            return r1 == r2 && abs(c1 - c2) <= 1
        case let (.pane(p1), .pane(p2)):
            return p1 == p2
        case let (.space(r1, l1), .space(r2, l2)):
            return r1 == r2 && l1 == l2
        case (.none, .none):
            return true
        default:
            return false
        }
    }

    /// Feed the current target; returns the target to act on when a dwell
    /// completes, otherwise nil.
    mutating func update(_ target: HoverTarget, at now: TimeInterval) -> HoverTarget? {
        if !DwellMachine.sameSpot(anchor, target) {
            anchor = target
            since = now
            fired = false
            return nil
        }
        guard !fired else { return nil }
        let needed: TimeInterval
        switch anchor {
        case .none: return nil
        case .tabRow, .space: needed = tabDwell
        case .pane: needed = paneDwell
        }
        guard now - since >= needed else { return nil }
        fired = true
        return target
    }

    mutating func reset() {
        anchor = .none
        fired = false
    }
}

// MARK: Hit-testing

enum TabBarPosition: String {
    case top, bottom
}

/// A herdr layout ready for hit-testing: pane rects already shifted for the
/// tab bar position, and the grid row of the tab bar (nil when hidden).
struct HitLayout: Equatable {
    let layout: PaneLayoutSnapshot
    let tabRow: Int?

    var focusedPaneID: String { layout.focusedPaneID }
    var sidebarColumns: Int { layout.offsetColumns }
}

/// The chrome rows (grid rows minus herdr's area) belong to the tab bar. On
/// top, panes start below it; at the bottom, panes start at grid row 0.
func hitLayout(_ layout: PaneLayoutSnapshot, gridRows: Int, position: TabBarPosition) -> HitLayout {
    guard layout.offsetRows >= 1 else { return HitLayout(layout: layout, tabRow: nil) }
    switch position {
    case .top:
        return HitLayout(layout: layout, tabRow: layout.offsetRows - 1)
    case .bottom:
        let shifted = PaneLayoutSnapshot(focusedPaneID: layout.focusedPaneID, panes: layout.panes,
                                         offsetColumns: layout.offsetColumns, offsetRows: 0)
        return HitLayout(layout: shifted, tabRow: gridRows - 1)
    }
}

/// What the cursor cell is over.
func hoverTarget(cell: (column: Int, row: Int)?, hit: HitLayout) -> HoverTarget {
    guard let cell = cell else { return .none }
    if let row = hit.tabRow, cell.row == row {
        return cell.column >= hit.sidebarColumns ? .tabRow(column: cell.column, row: row) : .none
    }
    if let pane = hit.layout.pane(atCell: cell) {
        return .pane(pane.paneID)
    }
    return .none
}

// MARK: Tab labels in the tab row

struct TabInfo: Equatable {
    let tabID: String
    let label: String
    let focused: Bool
}

struct TabSpan: Equatable {
    let tabID: String
    /// Grid columns [start, end).
    let start: Int
    let end: Int

    func contains(_ column: Int) -> Bool { start <= column && column < end }
}

func parseTabList(_ data: Data) -> [TabInfo]? {
    guard let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let result = payload["result"] as? [String: Any],
          let tabs = result["tabs"] as? [[String: Any]] else { return nil }
    let sorted = tabs.sorted { ($0["number"] as? Int ?? 0) < ($1["number"] as? Int ?? 0) }
    return sorted.compactMap { tab in
        guard let id = tab["tab_id"] as? String, let label = tab["label"] as? String else { return nil }
        return TabInfo(tabID: id, label: label, focused: tab["focused"] as? Bool ?? false)
    }
}

/// Terminal cell width of a character: 0 for combining marks, 2 for wide
/// East Asian characters and most emoji, otherwise 1.
func cellWidth(_ ch: Character) -> Int {
    guard let scalar = ch.unicodeScalars.first else { return 0 }
    let v = scalar.value
    if scalar.properties.generalCategory == .nonspacingMark
        || scalar.properties.generalCategory == .enclosingMark { return 0 }
    let wide: [ClosedRange<UInt32>] = [
        0x1100...0x115F, 0x2E80...0x303E, 0x3041...0x33FF, 0x3400...0x4DBF,
        0x4E00...0x9FFF, 0xA000...0xA4CF, 0xAC00...0xD7A3, 0xF900...0xFAFF,
        0xFE30...0xFE4F, 0xFF00...0xFF60, 0xFFE0...0xFFE6, 0x1F300...0x1F64F,
        0x1F900...0x1F9FF, 0x20000...0x3FFFD,
    ]
    return wide.contains { $0.contains(v) } ? 2 : 1
}

/// Lay a string out on terminal cells: wide characters take their cell
/// plus a nil spacer.
func cells(of text: String) -> [Character?] {
    var out: [Character?] = []
    for ch in text {
        let width = cellWidth(ch)
        if width == 0 { continue }
        out.append(ch)
        if width == 2 { out.append(nil) }
    }
    return out
}

/// One grid row from the text area's AX value. The value may hold more
/// lines than the grid (scrollback) or fewer (trailing blank lines trimmed).
func rowText(fromScreen value: String, rows: Int, row: Int) -> String? {
    var lines = value.components(separatedBy: "\n")
    if lines.last == "" { lines.removeLast() }
    guard row >= 0, row < rows, !lines.isEmpty else { return nil }
    let index = lines.count > rows ? lines.count - rows + row : row
    return index < lines.count ? lines[index] : nil
}

/// Column spans of the tab labels in the tab row text, matched in tab order
/// starting at `startColumn` (just past the sidebar). Each tab must fill its
/// exact herdr box (the label, optionally with the " Z" zoom suffix, centred
/// in max(8, width + 4) cells, then one separator cell), and the strip must
/// end with the " + " new-tab control. A span covers the label cells only.
/// Any mismatch, overflow or missing tab yields no spans at all.
func tabSpans(rowText: String, tabs: [TabInfo], startColumn: Int) -> [TabSpan] {
    // Herdr 0.9.3 renders consecutive centered tabs, minimum width 8,
    // label width + 4, separated by one cell. Never search rightward for a
    // missing label: that could find zoom/new-tab/plugin control text.
    let row = cells(of: rowText) + [Character(" "), Character(" "), Character(" ")]
    var cursor = max(0, startColumn)
    var spans: [TabSpan] = []
    for tab in tabs {
        let label = cells(of: tab.label)
        guard !label.isEmpty else { return [] }
        var matched: (Int, Int, Int)?
        for shown in [label, label + cells(of: " Z")] {
            let width = max(8, shown.count + 4)
            let left = (width - shown.count) / 2
            let expected: [Character?] = Array(repeating: Character(" "), count: left)
                + shown + Array(repeating: Character(" "), count: width - left - shown.count)
            if cursor + width <= row.count && Array(row[cursor..<(cursor + width)]) == expected {
                matched = (width, left, shown.count); break
            }
        }
        guard let (width, left, count) = matched else { return [] }
        spans.append(TabSpan(tabID: tab.tabID, start: cursor + left, end: cursor + left + count))
        cursor += width + 1
    }
    // Refuse overflow and uncertain strip boundaries. Even a tab named "+"
    // must occupy a full tab box followed by the separate three-cell control.
    guard cursor + 3 <= row.count,
          Array(row[cursor..<(cursor + 3)]) == cells(of: " + ") else { return [] }
    return spans
}

struct FocusStep { let direction: String; let pane: String }
func focusRoute(socket: String, from source: String, to destination: String, cancelled: () -> Bool) throws -> [FocusStep] {
    var queue: [(String, [FocusStep])] = [(source, [])]
    var seen: Set<String> = [source]
    while !queue.isEmpty && seen.count <= 64 {
        let (pane, route) = queue.removeFirst()
        if pane == destination { return route }
        for direction in ["left", "right", "up", "down"] {
            guard !cancelled() else { throw GateError(description: "cancelled") }
            let data = try herdrRequest(socket: socket, method: "pane.neighbor", params: ["pane_id": pane, "direction": direction], timeout: 0.2)
            guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let result = obj["result"] as? [String: Any], let neighbor = result["neighbor"] as? [String: Any],
                  let next = neighbor["neighbor_pane_id"] as? String, !seen.contains(next) else { continue }
            seen.insert(next)
            queue.append((next, route + [FocusStep(direction: direction, pane: next)]))
        }
    }
    throw GateError(description: "destination pane is no longer reachable")
}

struct SpaceInfo: Equatable {
    let workspaceID: String
    let label: String
    let number: Int
    let focused: Bool
}

struct SpaceSpan: Equatable {
    let row: Int
    let workspaceID: String
    let start: Int
    let end: Int
    func contains(_ column: Int) -> Bool { start <= column && column < end }
}

func parseSpaceList(_ value: Any?) -> [SpaceInfo]? {
    guard let list = (value as? [String: Any])?["workspaces"] as? [[String: Any]] else { return nil }
    var spaces: [SpaceInfo] = []
    for item in list {
        guard let id = item["workspace_id"] as? String, let label = item["label"] as? String,
              let number = item["number"] as? Int else { return nil }
        spaces.append(SpaceInfo(workspaceID: id, label: label, number: number, focused: item["focused"] as? Bool ?? false))
    }
    return spaces.sorted { $0.number < $1.number }
}

/// Label spans of the workspaces in herdr's sidebar, read as one exact
/// sequence of herdr 0.9.3's default layout: " spaces", one blank row, then
/// every workspace in number order as " <status> <label>" padded with
/// blanks, each followed only by three-space continuation rows (branch, git
/// status) or blank gap rows. Any other shape (scrolled list, worktree
/// groups, custom tokens, truncated labels, a scrollbar) yields no targets:
/// a label that merely appears somewhere never authorizes a click.
func spaceSpans(screen: String, rows: Int, sidebar: Int, spaces: [SpaceInfo]) -> [SpaceSpan] {
    let width = sidebar - 1 // the last sidebar column is the border
    guard width >= 5, !spaces.isEmpty, Set(spaces.map(\.number)).count == spaces.count else { return [] }
    let blank = Character(" ")
    func line(_ y: Int) -> [Character?]? {
        guard let text = rowText(fromScreen: screen, rows: rows, row: y) else { return nil }
        let row = cells(of: text)
        guard row.count > width else { return nil }
        return Array(row[0..<width])
    }
    func padded(_ text: [Character?]) -> [Character?]? {
        text.count <= width ? text + Array(repeating: blank, count: width - text.count) : nil
    }
    guard let header = padded(cells(of: " spaces")), line(0) == header,
          let empty = padded([]), line(1) == empty else { return [] }
    let icons: Set<Character> = ["●", "○", "·", "×", "◐", "✓"]
    var y = 2
    var result: [SpaceSpan] = []
    for (index, space) in spaces.enumerated() {
        let label = cells(of: space.label)
        guard !label.isEmpty, let row = line(y), let icon = row[1], icons.contains(icon),
              let expected = padded([blank, icon, blank] + label), row == expected else { return [] }
        result.append(SpaceSpan(row: y, workspaceID: space.workspaceID, start: 3, end: 3 + label.count))
        y += 1
        // Continuation rows of this entry, then gap rows before the next one.
        while let row = line(y), row[0] == blank, row[1] == blank, row[2] == blank, row[3] != blank { y += 1 }
        if index + 1 < spaces.count {
            while let row = line(y), row == empty { y += 1 }
        } else if let row = line(y), row != empty {
            return []
        }
    }
    return result
}

// MARK: herdr clients and sessions

enum HerdrClient: Equatable {
    case local(session: String?)
    case remote
}

/// Classify a process's argv: a herdr client attached to a local session,
/// a remote client, or nil for anything else (server, CLI calls).
func parseHerdrClient(_ argv: [String]) -> HerdrClient? {
    guard let first = argv.first, (first as NSString).lastPathComponent == "herdr" else { return nil }
    var session: String?
    var rest: [String] = []
    var i = 1
    while i < argv.count {
        let arg = argv[i]
        if arg == "--session", i + 1 < argv.count {
            session = argv[i + 1]
            i += 2
            continue
        }
        if arg.hasPrefix("--session=") {
            session = String(arg.dropFirst("--session=".count))
        } else if arg == "--remote" || arg.hasPrefix("--remote=")
                    || arg == "--machine" || arg.hasPrefix("--machine=") {
            return .remote
        } else {
            rest.append(arg)
        }
        i += 1
    }
    if rest.isEmpty { return .local(session: session) }
    if rest.count == 3, rest[0] == "session", rest[1] == "attach" { return .local(session: rest[2]) }
    return nil
}

struct ProcessEntry: Equatable {
    let pid: Int32
    let ppid: Int32
    let tty: String
    let argv: [String]
}

/// Parse `ps -axo pid=,ppid=,tty=,args=` output.
func parsePS(_ output: String) -> [ProcessEntry] {
    output.split(separator: "\n").compactMap { line in
        let fields = line.split(separator: " ", omittingEmptySubsequences: true).map(String.init)
        guard fields.count >= 4, let pid = Int32(fields[0]), let ppid = Int32(fields[1]) else { return nil }
        return ProcessEntry(pid: pid, ppid: ppid, tty: fields[2], argv: Array(fields[3...]))
    }
}

struct ClientCandidate: Equatable {
    let pid: Int32
    let ttyPath: String
    let client: HerdrClient
}

/// herdr clients running on a terminal under `ancestor` (the Ghostty pid).
func herdrClients(in processes: [ProcessEntry], under ancestor: Int32) -> [ClientCandidate] {
    let parent = Dictionary(processes.map { ($0.pid, $0.ppid) }, uniquingKeysWith: { a, _ in a })
    func descends(_ pid: Int32) -> Bool {
        var current = pid
        for _ in 0..<64 {
            guard let up = parent[current], up > 1 else { return false }
            if up == ancestor { return true }
            current = up
        }
        return false
    }
    return processes.compactMap { entry in
        guard entry.tty != "??", !entry.tty.isEmpty,
              let client = parseHerdrClient(entry.argv), descends(entry.pid) else { return nil }
        let tty = entry.tty.hasPrefix("/dev/") ? entry.tty : "/dev/" + entry.tty
        return ClientCandidate(pid: entry.pid, ttyPath: tty, client: client)
    }
}

/// Which client may receive the probe key. Only a herdr client process can
/// turn the key into a probe; anywhere else (a shell, ssh, a remote client)
/// the key would be typed into the terminal. So the probe needs exactly one
/// herdr client under this Ghostty, in the foreground of its tty, whose pixel
/// size fits the hovered window, and that client must be local.
func probeCandidate(_ candidates: [(candidate: ClientCandidate, fits: Bool, foreground: Bool)]) -> ClientCandidate? {
    let plausible = candidates.filter { $0.fits && $0.foreground }
    guard plausible.count == 1, case .local = plausible[0].candidate.client else { return nil }
    return plausible[0].candidate
}

// MARK: Plugin registration

/// The plugin's state in herdr's registry (plugins.json in herdr's config dir).
enum PluginRegistration: Equatable {
    case registered
    case disabled
    case manifestMissing
    case missing
    case unreadable
}

func pluginRegistration(_ data: Data?, pluginID: String, fileExists: (String) -> Bool) -> PluginRegistration {
    guard let data else { return .missing }
    guard let list = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return .unreadable }
    guard let entry = list.first(where: { $0["plugin_id"] as? String == pluginID }) else { return .missing }
    guard entry["enabled"] as? Bool != false else { return .disabled }
    guard let manifest = entry["manifest_path"] as? String, fileExists(manifest) else { return .manifestMissing }
    return .registered
}

enum RegistrationAction: Equatable {
    case run
    case pause
    case remove
}

/// herdr has no uninstall hook. Once the plugin has been seen registered and
/// its registry entry is gone, the helper removes itself; before that (the
/// build step of herdr plugin install runs before registration) and for a
/// disabled plugin or a moved manifest it only goes inert.
func registrationAction(_ state: PluginRegistration, seenRegistered: Bool) -> RegistrationAction {
    switch state {
    case .registered: return .run
    case .missing: return seenRegistered ? .remove : .pause
    case .disabled, .manifestMissing, .unreadable: return .pause
    }
}

/// Fail closed when probes stop being answered: after `limit` consecutive
/// misses, no probe key is sent for `pause` seconds.
struct ProbeBackoff {
    let limit: Int
    let pause: TimeInterval
    private(set) var misses = 0
    private(set) var pausedUntil: TimeInterval = 0

    init(limit: Int = 3, pause: TimeInterval = 60) {
        self.limit = limit
        self.pause = pause
    }

    func allows(at now: TimeInterval) -> Bool { now >= pausedUntil }

    /// Returns true when this miss starts a pause.
    mutating func miss(at now: TimeInterval) -> Bool {
        misses += 1
        guard misses >= limit else { return false }
        misses = 0
        pausedUntil = now + pause
        return true
    }

    mutating func answered() { misses = 0 }
}

/// Socket of a local session: the default one, or a named session's.
func socketPath(session: String?, home: String) -> String? {
    let base = home + "/.config/herdr"
    guard let name = session, name != "default" else { return base + "/herdr.sock" }
    let allowed = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    guard !name.isEmpty, name != ".", name != "..",
          name.unicodeScalars.allSatisfy({ allowed.contains($0) }) else { return nil }
    return base + "/sessions/" + name + "/herdr.sock"
}

/// Ghostty terminal ids go into AppleScript source, so accept only plain ids.
func isSafeTerminalID(_ id: String) -> Bool {
    let allowed = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    return !id.isEmpty && id.count <= 128 && id.unicodeScalars.allSatisfy { allowed.contains($0) }
}

/// Ghostty windows from the AppleScript listing, one per line:
/// window id, name, terminal count of the selected tab, focused terminal id,
/// separated by ASCII unit separators.
struct GhosttyWindow: Equatable {
    let id: String
    let name: String
    let terminals: Int
    let focusedTerminal: String
}

func parseGhosttyWindows(_ output: String) -> [GhosttyWindow] {
    output.split(separator: "\n").compactMap { line in
        let parts = line.components(separatedBy: "\u{1F}")
        guard parts.count == 4, let count = Int(parts[2]) else { return nil }
        return GhosttyWindow(id: parts[0], name: parts[1], terminals: count, focusedTerminal: parts[3])
    }
}

/// The Ghostty terminal to click: the only window whose title matches the
/// window under the cursor, and only when its selected tab has one terminal.
func terminalForWindow(titled title: String, in windows: [GhosttyWindow]) -> String? {
    let matches = windows.filter { $0.name == title }
    guard matches.count == 1, matches[0].terminals == 1,
          isSafeTerminalID(matches[0].focusedTerminal) else { return nil }
    return matches[0].focusedTerminal
}

// MARK: Bounded commands

struct CommandResult {
    var status: Int32
    var output: String
    var timedOut: Bool
}

final class CommandRegistry: @unchecked Sendable {
    let lock = NSLock()
    var processes: [Int32: Process] = [:]
    func add(_ process: Process) { lock.lock(); processes[process.processIdentifier] = process; lock.unlock() }
    func remove(_ process: Process) { lock.lock(); processes.removeValue(forKey: process.processIdentifier); lock.unlock() }
    func stopAll() {
        lock.lock(); let active = Array(processes.values); lock.unlock()
        for process in active where process.isRunning { process.terminate() }
        let deadline = Date().addingTimeInterval(0.3)
        while Date() < deadline && active.contains(where: { $0.isRunning }) { Thread.sleep(forTimeInterval: 0.01) }
        for process in active where process.isRunning { kill(process.processIdentifier, SIGKILL) }
        for process in active { process.waitUntilExit() }
    }
}
let commandRegistry = CommandRegistry()

/// Run a command and wait at most `timeout` seconds. A command that overruns
/// is sent SIGTERM, then SIGKILL, and is always reaped. Output goes through a
/// temp file so a child that keeps a pipe open cannot block the caller.
func runCommand(_ path: String, _ args: [String], timeout: TimeInterval, cancelled: () -> Bool = { false }) -> CommandResult {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: path)
    process.arguments = args
    var env = ProcessInfo.processInfo.environment
    for key in ["HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID", "HERDR_SOCKET_PATH"] {
        env.removeValue(forKey: key)
    }
    process.environment = env
    let outURL = FileManager.default.temporaryDirectory
        .appendingPathComponent("herdr-spring-load.\(UUID().uuidString).out")
    FileManager.default.createFile(atPath: outURL.path, contents: nil,
                                   attributes: [.posixPermissions: 0o600])
    defer { try? FileManager.default.removeItem(at: outURL) }
    guard let outHandle = try? FileHandle(forWritingTo: outURL) else {
        return CommandResult(status: -1, output: "cannot create temp file", timedOut: false)
    }
    process.standardOutput = outHandle
    process.standardError = outHandle
    process.standardInput = FileHandle.nullDevice
    let done = DispatchSemaphore(value: 0)
    process.terminationHandler = { _ in done.signal() }
    do {
        try process.run()
    } catch {
        try? outHandle.close()
        return CommandResult(status: -1, output: "\(error)", timedOut: false)
    }
    commandRegistry.add(process)
    defer { commandRegistry.remove(process) }
    var timedOut = false
    var completed = false
    let commandDeadline = Date().addingTimeInterval(timeout)
    while !cancelled() && Date() < commandDeadline {
        if done.wait(timeout: .now() + 0.02) == .success { completed = true; break }
    }
    if !completed {
        timedOut = true
        process.terminate()
        if done.wait(timeout: .now() + 0.5) == .timedOut {
            kill(process.processIdentifier, SIGKILL)
            done.wait()
        }
    }
    try? outHandle.close()
    let data = (try? Data(contentsOf: outURL)) ?? Data()
    let status = timedOut ? -1 : process.terminationStatus
    return CommandResult(status: status, output: String(decoding: data, as: UTF8.self), timedOut: timedOut)
}

// MARK: herdr socket

/// One newline-delimited JSON request over a herdr Unix socket, with a
/// deadline on connect, send and receive. Returns the raw response line.
func herdrRequest(socket path: String, method: String, params: [String: Any],
                  timeout: TimeInterval) throws -> Data {
    let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else { throw GateError(description: "socket() failed") }
    defer { close(fd) }
    var tv = timeval(tv_sec: Int(timeout), tv_usec: Int32((timeout - floor(timeout)) * 1_000_000))
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
    var one: Int32 = 1
    setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))
    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    let bytes = Array(path.utf8)
    guard bytes.count < MemoryLayout.size(ofValue: addr.sun_path) else {
        throw GateError(description: "socket path too long")
    }
    withUnsafeMutableBytes(of: &addr.sun_path) { raw in
        raw.copyBytes(from: bytes)
        raw[bytes.count] = 0
    }
    _ = fcntl(fd, F_SETFL, O_NONBLOCK)
    let connected = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
        }
    }
    if connected != 0 {
        guard errno == EINPROGRESS else { throw GateError(description: "cannot connect to socket") }
        var ready = pollfd(fd: fd, events: Int16(POLLOUT), revents: 0)
        guard poll(&ready, 1, Int32(timeout * 1000)) > 0 else { throw GateError(description: "connect timed out") }
        var error: Int32 = 0
        var size = socklen_t(MemoryLayout<Int32>.size)
        guard getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &size) == 0, error == 0 else { throw GateError(description: "connect failed") }
    }
    _ = fcntl(fd, F_SETFL, 0)
    let request: [String: Any] = ["id": "herdr-spring-load", "method": method, "params": params]
    var payload = try JSONSerialization.data(withJSONObject: request)
    payload.append(0x0A)
    let sent = payload.withUnsafeBytes { send(fd, $0.baseAddress, $0.count, 0) }
    guard sent == payload.count else { throw GateError(description: "send failed") }
    var response = Data()
    var buffer = [UInt8](repeating: 0, count: 65536)
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        let n = recv(fd, &buffer, buffer.count, 0)
        if n <= 0 { break }
        response.append(contentsOf: buffer[0..<n])
        guard response.count <= 4_000_000 else { throw GateError(description: "response too large") }
        if let newline = response.firstIndex(of: 0x0A) {
            return response.prefix(upTo: newline)
        }
    }
    throw GateError(description: "no response from herdr for \(method)")
}

/// The `error` object of a herdr response, if any.
func herdrError(_ data: Data) -> String? {
    guard let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let error = payload["error"] else { return nil }
    return "\(error)"
}
