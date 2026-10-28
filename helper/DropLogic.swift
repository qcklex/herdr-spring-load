// Pure logic shared by the watcher and its tests: running commands with a
// deadline, and deciding whether a drop really landed in a herdr pane.

import Foundation

let imageExtensions: Set<String> = [
    "png", "jpg", "jpeg", "gif", "heic", "heif", "webp", "tif", "tiff", "bmp",
]

struct CommandResult {
    var status: Int32
    var output: String
    var timedOut: Bool
}

/// Run a command and wait at most `timeout` seconds. A command that overruns
/// is sent SIGTERM, then SIGKILL, and is always reaped. Output goes through a
/// temp file so a child that keeps a pipe open cannot block the caller.
func runCommand(_ path: String, _ args: [String], timeout: TimeInterval) -> CommandResult {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: path)
    process.arguments = args
    var env = ProcessInfo.processInfo.environment
    // Inside a herdr pane these would make `pane current` answer for the
    // caller instead of the focused pane.
    for key in ["HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID"] {
        env.removeValue(forKey: key)
    }
    process.environment = env
    let outURL = FileManager.default.temporaryDirectory
        .appendingPathComponent("herdr-spring-load.\(UUID().uuidString).out")
    FileManager.default.createFile(atPath: outURL.path, contents: nil)
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
    var timedOut = false
    if done.wait(timeout: .now() + timeout) == .timedOut {
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

/// pane_id from `herdr pane current` JSON output.
func focusedPaneID(fromJSON text: String) -> String? {
    guard let data = text.data(using: .utf8),
          let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let result = object["result"] as? [String: Any],
          let pane = result["pane"] as? [String: Any]
    else { return nil }
    return pane["pane_id"] as? String
}

func occurrences(of needle: String, in haystack: String) -> Int {
    guard !needle.isEmpty else { return 0 }
    return haystack.components(separatedBy: needle).count - 1
}

/// Screen text with shell escaping removed, so `My\ Shot.png` and
/// `'My Shot.png'` both read as `My Shot.png`.
func unescaped(_ text: String) -> String {
    text.replacingOccurrences(of: "\\", with: "")
        .replacingOccurrences(of: "'", with: "")
        .replacingOccurrences(of: "\"", with: "")
}

/// Image placeholders such as `[Image #2]` that agent TUIs show for a drop.
func imagePlaceholders(in text: String) -> Set<String> {
    guard let regex = try? NSRegularExpression(pattern: #"\[Image #\d+\]"#) else { return [] }
    let range = NSRange(text.startIndex..., in: text)
    return Set(regex.matches(in: text, range: range).compactMap {
        Range($0.range, in: text).map { String(text[$0]) }
    })
}

/// True when the pane text after a drop shows something the drop added: a
/// dropped file's name appears more often than before, or a new image
/// placeholder appeared.
func dropLanded(before: String, after: String, paths: [String]) -> Bool {
    let plainBefore = unescaped(before)
    let plainAfter = unescaped(after)
    for path in paths {
        let name = (path as NSString).lastPathComponent
        if occurrences(of: name, in: plainAfter) > occurrences(of: name, in: plainBefore) {
            return true
        }
    }
    return !imagePlaceholders(in: after).subtracting(imagePlaceholders(in: before)).isEmpty
}
