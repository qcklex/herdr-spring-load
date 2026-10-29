// herdr-spring-load sensor for macOS (contract 1, docs/SENSOR.md).
//
// Observes only, and needs no macOS permission:
//   - the drag pasteboard's changeCount and type names (never its contents),
//   - NSEvent.pressedMouseButtons and NSEvent.mouseLocation (polled, no event tap),
//   - CGWindowListCopyWindowInfo bounds, layer and owner pid (no window names,
//     so no Screen Recording),
//   - the process table (sysctl KERN_PROC_ALL, KERN_PROCARGS2) and TIOCGWINSZ on
//     the clients' ttys, opened read-only and never written.
// It posts no events, injects no keys, raises nothing and never touches the drop.

import AppKit
import CoreGraphics
import Darwin
import Foundation

let sensorVersion = "0.3.0"
let pollInterval = 0.02
let moveMinInterval = 0.03
let moveMaxInterval = 0.25
let clientRefresh = 1.0
let clickSlop = 8.0
let clickTime = 0.5
let envKeys: Set<String> = ["HERDR_SESSION", "HERDR_SOCKET_PATH", "HERDR_CLIENT_SOCKET_PATH", "HERDR_CONFIG_PATH",
                            "XDG_CONFIG_HOME", "XDG_STATE_HOME", "HOME"]
let fileTypes: Set<String> = ["public.file-url", "NSFilenamesPboardType", "com.apple.pasteboard.promised-file-url",
                              "com.apple.NSFilePromiseItemMetaData", "Apple files promise pasteboard type",
                              "com.apple.pasteboard.promised-file-content-type"]

var verbose = false
let started = ProcessInfo.processInfo.systemUptime
var clock: () -> Double = { ProcessInfo.processInfo.systemUptime - started }

func now() -> Double { clock() }

func debug(_ message: String) {
    if verbose { FileHandle.standardError.write(("herdr-spring-load-sensor: " + message + "\n").data(using: .utf8)!) }
}

let outputLock = NSLock()

func emit(_ event: [String: Any]) {
    var event = event
    event["t"] = now()
    guard var data = try? JSONSerialization.data(withJSONObject: event, options: [.withoutEscapingSlashes]),
          data.count < 64 * 1024 else { return }
    data.append(0x0a)
    outputLock.lock()
    defer { outputLock.unlock() }
    let ok = data.withUnsafeBytes { raw -> Bool in
        var offset = 0
        while offset < raw.count {
            let n = write(1, raw.baseAddress! + offset, raw.count - offset)
            if n < 0 { if errno == EINTR { continue }; return false }
            offset += n
        }
        return true
    }
    if !ok { exit(0) }
}

func hello() -> [String: Any] {
    ["type": "hello", "contract": 1, "sensor": "macos", "version": sensorVersion, "coords": "macos-points",
     "capabilities": ["drag", "click", "window", "clients"]]
}

// MARK: process table

struct Proc {
    let pid: Int32
    let ppid: Int32
    let uid: uid_t
    let comm: String
    let tdev: dev_t
    let pgid: pid_t
    let tpgid: pid_t
    let started: Double
}

func processTable() -> [Proc]? {
    var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_ALL, 0]
    var size = 0
    for _ in 0..<4 {
        if sysctl(&mib, 3, nil, &size, nil, 0) != 0 { return nil }
        size += size / 8 + 16 * MemoryLayout<kinfo_proc>.stride
        var procs = [kinfo_proc](repeating: kinfo_proc(), count: size / MemoryLayout<kinfo_proc>.stride)
        let result = procs.withUnsafeMutableBytes { sysctl(&mib, 3, $0.baseAddress, &size, nil, 0) }
        if result == 0 {
            return procs.prefix(size / MemoryLayout<kinfo_proc>.stride).map { info in
                var proc = info.kp_proc
                let comm = withUnsafeBytes(of: &proc.p_comm) { raw in
                    String(decoding: raw.prefix(while: { $0 != 0 }), as: UTF8.self)
                }
                return Proc(pid: info.kp_proc.p_pid, ppid: info.kp_eproc.e_ppid, uid: info.kp_eproc.e_ucred.cr_uid,
                            comm: comm, tdev: info.kp_eproc.e_tdev, pgid: info.kp_eproc.e_pgid,
                            tpgid: info.kp_eproc.e_tpgid,
                            started: Double(info.kp_proc.p_un.__p_starttime.tv_sec)
                                + Double(info.kp_proc.p_un.__p_starttime.tv_usec) / 1e6)
            }
        }
        if errno != ENOMEM { return nil }
    }
    return nil
}

/// argv and the allowed environment keys from KERN_PROCARGS2 (same-user processes only).
func arguments(_ pid: Int32) -> (argv: [String], env: [String: String]?) {
    var mib: [Int32] = [CTL_KERN, KERN_ARGMAX]
    var argmax: Int32 = 0
    var size = MemoryLayout<Int32>.size
    guard sysctl(&mib, 2, &argmax, &size, nil, 0) == 0, argmax > 0 else { return ([], nil) }
    var buffer = [UInt8](repeating: 0, count: Int(argmax))
    mib = [CTL_KERN, KERN_PROCARGS2, pid]
    size = buffer.count
    guard buffer.withUnsafeMutableBytes({ sysctl(&mib, 3, $0.baseAddress, &size, nil, 0) }) == 0,
          size > MemoryLayout<Int32>.size else { return ([], nil) }
    let argc = buffer.withUnsafeBytes { $0.loadUnaligned(as: Int32.self) }
    var index = MemoryLayout<Int32>.size
    while index < size && buffer[index] != 0 { index += 1 }  // exec path
    while index < size && buffer[index] == 0 { index += 1 }  // padding
    var strings: [String] = []
    while index < size {
        let start = index
        while index < size && buffer[index] != 0 { index += 1 }
        if index == start { break }
        strings.append(String(decoding: buffer[start..<index], as: UTF8.self))
        index += 1
    }
    let count = Int(max(argc, 0))
    guard strings.count >= count else { return (strings, nil) }
    var env: [String: String] = [:]
    for entry in strings[count...] {
        guard let eq = entry.firstIndex(of: "=") else { continue }
        let key = String(entry[..<eq])
        if envKeys.contains(key) { env[key] = String(entry[entry.index(after: eq)...]) }
    }
    return (Array(strings[..<count]), env)
}

let tiocgwinsz: UInt = 0x40087468  // _IOR('t', 104, struct winsize)

func client(_ proc: Proc) -> [String: Any]? {
    guard proc.tdev != -1, let name = devname(proc.tdev, S_IFCHR) else { return nil }
    let tty = "/dev/" + String(cString: name)
    let (argv, env) = arguments(proc.pid)
    var entry: [String: Any] = ["pid": Int(proc.pid), "argv": argv, "tty": tty, "rows": NSNull(), "cols": NSNull(),
                                "xpixel": 0, "ypixel": 0, "foreground": proc.tpgid > 0 && proc.tpgid == proc.pgid, "started": proc.started,
                                "env": env.map { $0 as Any } ?? NSNull()]
    let fd = open(tty, O_RDONLY | O_NOCTTY | O_NONBLOCK)
    if fd >= 0 {
        var size = winsize()
        if ioctl(fd, tiocgwinsz, &size) == 0 {
            entry["rows"] = Int(size.ws_row)
            entry["cols"] = Int(size.ws_col)
            entry["xpixel"] = Int(size.ws_xpixel)
            entry["ypixel"] = Int(size.ws_ypixel)
        }
        close(fd)
    }
    return entry
}

func isHerdr(_ proc: Proc) -> Bool { proc.comm == "herdr" }

/// herdr clients with a tty at any depth below `root`; nil when the table can't be read.
func clientsBelow(_ root: Int32, in table: [Proc]?) -> [[String: Any]]? {
    guard let table = table else { return nil }
    var children: [Int32: [Proc]] = [:]
    for proc in table { children[proc.ppid, default: []].append(proc) }
    var found: [[String: Any]] = []
    var stack = children[root] ?? []
    var seen = Set<Int32>()
    while let proc = stack.popLast() {
        guard proc.pid > 0, seen.insert(proc.pid).inserted else { continue }
        if isHerdr(proc), let entry = client(proc) { found.append(entry) }
        stack.append(contentsOf: children[proc.pid] ?? [])
    }
    return found
}

func allClients(in table: [Proc]?) -> [[String: Any]]? {
    guard let table = table else { return nil }
    let uid = getuid()
    return table.filter { $0.uid == uid && isHerdr($0) }.compactMap(client)
}

// MARK: windows

func rect(_ r: CGRect) -> [String: Any] {
    ["x": Double(r.origin.x), "y": Double(r.origin.y), "width": Double(r.size.width), "height": Double(r.size.height)]
}

func scale(at point: CGPoint) -> Double? {
    var display: CGDirectDisplayID = 0
    var count: UInt32 = 0
    guard CGGetDisplaysWithPoint(point, 1, &display, &count) == .success, count == 1,
          let mode = CGDisplayCopyDisplayMode(display), mode.width > 0 else { return nil }
    return Double(mode.pixelWidth) / Double(mode.width)
}

struct WindowInfo {
    let id: UInt32
    let pid: Int32
    let bounds: CGRect
}

func normalWindows() -> [WindowInfo] {
    guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
            as? [[String: Any]] else { return [] }
    let me = getpid()
    return list.compactMap { info in
        guard (info[kCGWindowLayer as String] as? Int) == 0,
              ((info[kCGWindowAlpha as String] as? Double) ?? 1) > 0,
              let pid = info[kCGWindowOwnerPID as String] as? Int32, pid != me,
              let number = info[kCGWindowNumber as String] as? UInt32,
              let dict = info[kCGWindowBounds as String] as? NSDictionary,
              let bounds = CGRect(dictionaryRepresentation: dict as CFDictionary),
              bounds.width > 1, bounds.height > 1 else { return nil }
        return WindowInfo(id: number, pid: pid, bounds: bounds)
    }
}

final class ClientCache {
    var windowId: UInt32 = 0
    var stamp = -Double.infinity
    var value: [[String: Any]]? = nil

    func clients(for window: WindowInfo, force: Bool) -> [[String: Any]]? {
        let t = now()
        if force || window.id != windowId || t - stamp >= clientRefresh {
            value = clientsBelow(window.pid, in: processTable())
            windowId = window.id
            stamp = t
        }
        return value
    }
}

let cache = ClientCache()
var appNames: [Int32: String] = [:]

func appName(_ pid: Int32) -> Any {
    if let name = appNames[pid] { return name }
    guard let app = NSRunningApplication(processIdentifier: pid) else { return NSNull() }
    let name = app.bundleIdentifier ?? app.executableURL?.lastPathComponent
    if let name = name { appNames[pid] = name }
    return name ?? NSNull()
}

/// (description, id) of the topmost normal window under `point`.
func window(at point: CGPoint, forceClients: Bool = false) -> ([String: Any]?, UInt32?) {
    let windows = normalWindows()
    guard let hit = windows.first(where: { $0.bounds.contains(point) }) else { return (nil, nil) }
    let peers = windows.filter { $0.pid == hit.pid && $0.id != hit.id }.map { rect($0.bounds) }
    let description: [String: Any] = [
        "id": String(hit.id), "pid": Int(hit.pid), "app": appName(hit.pid), "bounds": rect(hit.bounds),
        "scale": scale(at: CGPoint(x: hit.bounds.midX, y: hit.bounds.midY)).map { $0 as Any } ?? NSNull(),
        "peers": peers,
        "clients": cache.clients(for: hit, force: forceClients).map { $0 as Any } ?? NSNull()]
    return (description, hit.id)
}

// MARK: pointer and drag

/// Pointer in CoreGraphics global space (top-left of the main display, y down).
func pointer() -> CGPoint {
    let location = NSEvent.mouseLocation
    let mainHeight = CGDisplayBounds(CGMainDisplayID()).height
    return CGPoint(x: location.x, y: mainHeight - location.y)
}

func dragKind(_ pasteboard: NSPasteboard) -> String {
    guard let types = pasteboard.types else { return "unknown" }
    if types.isEmpty { return "unknown" }
    return types.contains(where: { fileTypes.contains($0.rawValue) }) ? "files" : "other"
}

struct Sample {
    let t: Double
    let down: Bool
    let point: CGPoint
    let changeCount: Int
}

/// Where the sensor's inputs come from: the system, or a scripted replay (--simulate).
struct Inputs {
    var sample: () -> Sample
    var window: (CGPoint, Bool) -> ([String: Any]?, UInt32?)
    var kind: () -> String
    var allClients: () -> [[String: Any]]?
}

let dragPasteboard = NSPasteboard(name: .drag)
let systemInputs = Inputs(
    sample: { Sample(t: now(), down: NSEvent.pressedMouseButtons & 1 != 0, point: pointer(),
                     changeCount: dragPasteboard.changeCount) },
    window: { window(at: $0, forceClients: $1) },
    kind: { dragKind(dragPasteboard) },
    allClients: { allClients(in: processTable()) })

final class Sensor {
    let inputs: Inputs
    var changeCount: Int? = nil
    var dragging = false
    var lastMove = -Double.infinity
    var lastPoint = CGPoint(x: -1e9, y: -1e9)
    var lastWindow: UInt32? = nil
    var press: (point: CGPoint, t: Double, window: [String: Any]?)? = nil
    var wasDown = false
    var lastTick: Double? = nil

    init(_ inputs: Inputs) { self.inputs = inputs }

    func tick() {
        let sample = inputs.sample()
        let t = sample.t, down = sample.down, point = sample.point
        defer { changeCount = sample.changeCount; wasDown = down; lastTick = t }
        guard let previousCount = changeCount else { return }  // first sample sets the baseline
        if dragging, let last = lastTick, t - last > 2 {
            // Asleep or stalled: whatever happened in between, we lost track.
            end(point, reason: "cancelled")
            press = nil
            return
        }
        if dragging {
            if !down {
                end(point, reason: "released")
            } else {
                move(point, t: t)
            }
        } else if down && sample.changeCount != previousCount {
            press = nil
            dragging = true
            let (description, id) = inputs.window(point, true)
            lastWindow = id
            lastPoint = point
            lastMove = t
            debug("drag started at \(point)")
            emit(["type": "drag_start", "x": Double(point.x), "y": Double(point.y), "kind": inputs.kind(),
                  "window": description ?? NSNull(), "all_clients": inputs.allClients().map { $0 as Any } ?? NSNull()])
        } else if down && !wasDown {
            press = (point, t, inputs.window(point, false).0)
        } else if !down && wasDown, let start = press {
            press = nil
            if t - start.t <= clickTime && hypot(point.x - start.point.x, point.y - start.point.y) <= clickSlop {
                emit(["type": "click", "x": Double(start.point.x), "y": Double(start.point.y),
                      "window": start.window ?? NSNull()])
            }
        } else if down, let start = press, t - start.t > clickTime {
            press = nil
        }
    }

    func move(_ point: CGPoint, t: Double) {
        let elapsed = t - lastMove
        guard elapsed >= moveMinInterval else { return }
        let (description, id) = inputs.window(point, false)
        guard point != lastPoint || id != lastWindow || elapsed >= moveMaxInterval else { return }
        lastPoint = point
        lastWindow = id
        lastMove = t
        emit(["type": "drag_move", "x": Double(point.x), "y": Double(point.y), "window": description ?? NSNull()])
    }

    func end(_ point: CGPoint, reason: String) {
        dragging = false
        debug("drag ended (\(reason))")
        emit(["type": "drag_end", "x": Double(point.x), "y": Double(point.y), "reason": reason])
    }
}

/// --simulate: replay samples from stdin, one JSON object per line:
/// {"t": 0.02, "down": true, "x": 10, "y": 20, "count": 3, "window": "7"}; "window" names a fake window id.
func simulate() -> Never {
    var current: [String: Any] = [:]
    let inputs = Inputs(
        sample: {
            Sample(t: current["t"] as? Double ?? 0, down: current["down"] as? Bool ?? false,
                   point: CGPoint(x: current["x"] as? Double ?? 0, y: current["y"] as? Double ?? 0),
                   changeCount: current["count"] as? Int ?? 0)
        },
        window: { _, _ in
            guard let id = current["window"] as? String, let number = UInt32(id) else { return (nil, nil) }
            return (["id": id, "pid": 1, "app": NSNull(), "bounds": rect(CGRect(x: 0, y: 0, width: 800, height: 600)),
                     "scale": 2.0, "peers": [], "clients": []], number)
        },
        kind: { current["kind"] as? String ?? "files" },
        allClients: { [] })
    let sensor = Sensor(inputs)
    while let line = readLine() {
        guard let data = line.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
        current = object
        clock = { current["t"] as? Double ?? 0 }
        sensor.tick()
    }
    exit(0)
}

// MARK: main

signal(SIGPIPE, SIG_IGN)
let args = Array(CommandLine.arguments.dropFirst())
verbose = args.contains("--verbose")

if args.contains("--check") {
    emit(hello())
    exit(0)
}
// Test hooks: print what the sensor would report, then exit.
if let i = args.firstIndex(of: "--clients-below"), i + 1 < args.count, let pid = Int32(args[i + 1]) {
    emit(["type": "status", "level": "info", "message": "clients-below",
          "clients": clientsBelow(pid, in: processTable()).map { $0 as Any } ?? NSNull()])
    exit(0)
}
if let i = args.firstIndex(of: "--window-at"), i + 2 < args.count, let x = Double(args[i + 1]), let y = Double(args[i + 2]) {
    emit(["type": "status", "level": "info", "message": "window-at",
          "window": window(at: CGPoint(x: x, y: y), forceClients: true).0 ?? NSNull()])
    exit(0)
}
if args.contains("--all-clients") {
    emit(["type": "status", "level": "info", "message": "all-clients",
          "clients": allClients(in: processTable()).map { $0 as Any } ?? NSNull()])
    exit(0)
}

if args.contains("--simulate") {
    emit(hello())
    simulate()
}

emit(hello())
Thread.detachNewThread {
    var byte: UInt8 = 0
    while true {
        let n = read(0, &byte, 1)
        if n == 0 || (n < 0 && errno != EINTR) { exit(0) }
    }
}
let sensor = Sensor(systemInputs)
let timer = Timer(timeInterval: pollInterval, repeats: true) { _ in sensor.tick() }
RunLoop.main.add(timer, forMode: .common)
RunLoop.main.run()
