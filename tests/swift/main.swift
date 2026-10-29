import Foundation
import CoreGraphics
var failures = 0
func check(_ ok: Bool, _ name: String) { print("\(ok ? "ok" : "FAIL") \(name)"); if !ok { failures += 1 } }
func row(_ labels: [String], prefix: String = "") -> String {
    var result = prefix
    for label in labels {
        let n = cells(of: label).count, width = max(8, n + 4), left = (width - n) / 2
        result += String(repeating: " ", count: left) + label + String(repeating: " ", count: width - left - n) + " "
    }
    return result + " + "
}
func tabs(_ labels: [String]) -> [TabInfo] { labels.enumerated().map { TabInfo(tabID: "t\($0.offset)", label: $0.element, focused: $0.offset == 0) } }
let labels = ["terminal", "files"]
let spans = tabSpans(rowText: row(labels), tabs: tabs(labels), startColumn: 0)
check(spans.count == 2, "actual centered tab geometry")
check(!spans.contains { $0.contains(row(labels).count - 2) }, "new-tab excluded")
check(tabSpans(rowText: "  alpha  +  [zoom]", tabs: tabs(["alpha", "zoom"]), startColumn: 0).isEmpty, "missing tab cannot match zoom control")
check(tabSpans(rowText: "  alpha  +", tabs: tabs(["alpha", "+"]), startColumn: 0).isEmpty, "hidden '+' tab cannot match new-tab control")
check(tabSpans(rowText: row(["a"]) + " [close] herdr-bar", tabs: tabs(["a", "close"]), startColumn: 0).isEmpty, "close/plugin text excluded")
check(tabSpans(rowText: row(labels), tabs: tabs(["terminal", "missing"]), startColumn: 0).isEmpty, "truncated/missing tabs fail closed")
check(tabSpans(rowText: row(["+", "zoom", "日本"]), tabs: tabs(["+", "zoom", "日本"]), startColumn: 0).count == 3, "actual '+' and wide labels work in full tab boxes")
check(tabSpans(rowText: String(row(["1"]).dropLast()), tabs: tabs(["1"]), startColumn: 0).count == 1, "trimmed final blank is tolerated")
check(tabSpans(rowText: " < … clipped >  + ", tabs: tabs(labels), startColumn: 0).isEmpty, "uncertain overflow controls excluded")
// Sidebar of herdr 0.9.3 as rendered in a 26-column sidebar (verified in an isolated session).
func side(_ text: String) -> String { text.padding(toLength: 25, withPad: " ", startingAt: 0) + "│pane" }
func sidebar(_ lines: [String]) -> String { lines.map(side).joined(separator: "\n") }
let infos = [SpaceInfo(workspaceID: "w1", label: "project", number: 1, focused: true),
             SpaceInfo(workspaceID: "w2", label: "assets", number: 2, focused: false)]
let plain = sidebar([" spaces", "", " · project", " · assets", "", " new                 menu"])
let spaces = spaceSpans(screen: plain, rows: 6, sidebar: 26, spaces: infos)
check(spaces == [SpaceSpan(row: 2, workspaceID: "w1", start: 3, end: 10), SpaceSpan(row: 3, workspaceID: "w2", start: 3, end: 9)], "default sidebar maps rows to workspace ids")
check(spaceSpans(screen: sidebar([" spaces", "", " ● project", "   main", " ✓ assets", "   feat ↑1", ""]), rows: 7, sidebar: 26, spaces: infos).map(\.row) == [2, 4], "branch continuation rows are skipped")
check(spaceSpans(screen: sidebar([" spaces", "", " · project", "", " · assets", ""]), rows: 6, sidebar: 26, spaces: infos).map(\.row) == [2, 4], "row gaps are tolerated")
check(spaceSpans(screen: sidebar([" spaces", "", " · assets", " · project", ""]), rows: 5, sidebar: 26, spaces: infos).isEmpty, "out-of-order labels are refused")
check(spaceSpans(screen: sidebar([" spaces", "", " · project", ""]), rows: 4, sidebar: 26, spaces: infos).isEmpty, "a missing (scrolled or hidden) workspace refuses all")
check(spaceSpans(screen: sidebar([" spaces", "", " · project", " · assets-copy", ""]), rows: 5, sidebar: 26, spaces: infos).isEmpty, "partial workspace names do not match")
check(spaceSpans(screen: sidebar([" spaces", "", " · project", "   ├─ assets", ""]), rows: 5, sidebar: 26, spaces: infos).isEmpty, "worktree groups are refused")
check(spaceSpans(screen: sidebar([" spaces", "", " · project        ▾", " · assets", ""]), rows: 5, sidebar: 26, spaces: infos).isEmpty, "group toggles are refused")
check(spaceSpans(screen: sidebar([" spaces", "", " · project", " · assets", " · assets", ""]), rows: 6, sidebar: 26, spaces: infos).isEmpty, "extra rows after the list are refused")
check(spaceSpans(screen: sidebar(["", " · project", " · assets", ""]), rows: 4, sidebar: 26, spaces: infos).isEmpty, "a sidebar without its header is refused")
check(spaceSpans(screen: sidebar([" spaces", "", " · project", " · assets", "", " agents", " · assets"]), rows: 7, sidebar: 26, spaces: infos).allSatisfy { $0.row < 4 }, "agent rows never become space targets")
check(spaceSpans(screen: plain, rows: 6, sidebar: 0, spaces: infos).isEmpty, "collapsed sidebar cannot click other content")
let twins = [SpaceInfo(workspaceID: "w1", label: "same", number: 1, focused: true), SpaceInfo(workspaceID: "w2", label: "same", number: 2, focused: false)]
check(spaceSpans(screen: sidebar([" spaces", "", " · same", " · same", ""]), rows: 5, sidebar: 26, spaces: twins).map(\.workspaceID) == ["w1", "w2"], "duplicate labels resolve by position, not by word")
let probed: [String: Any] = ["workspaces": [["workspace_id": "w2", "label": "assets", "number": 2, "focused": false],
                                            ["workspace_id": "w1", "label": "project", "number": 1, "focused": true]]]
check(parseSpaceList(probed) == infos, "probe workspace list sorts by number")
check(parseSpaceList(["workspaces": [["workspace_id": "w1"]]]) == nil, "malformed workspace list is refused")
var dwell = DwellMachine(tabDwell: 0.6, paneDwell: 0.25)
let space = HoverTarget.space(row: 3, workspaceID: "w2")
check(dwell.update(space, at: 0) == nil, "space dwell begins while held")
check(dwell.update(space, at: 0.59) == nil, "space stays closed before deadline")
check(dwell.update(space, at: 0.6) == space, "space opens before file release")
check(dwell.update(space, at: 1.5) == nil, "space opens only once per dwell")
_ = dwell.update(.tabRow(column: 21, row: 0), at: 2)
check(dwell.update(.tabRow(column: 21, row: 0), at: 2.61) == .tabRow(column: 21, row: 0), "same drag continues into a tab")
_ = dwell.update(.pane("w2:p2"), at: 3)
check(dwell.update(.pane("w2:p2"), at: 3.26) == .pane("w2:p2"), "same drag focuses destination pane")
dwell.reset()
check(dwell.update(.none, at: 4) == nil, "release cancels dwell without generating drop input")
let g = CellGeometry(frame: CGRect(x: 100, y: 100, width: 810, height: 610), columns: 80, rows: 30, padding: CGSize(width: 5, height: 5))
check(g.cell(at: CGPoint(x: 106, y: 106))! == (0, 0), "screen coordinates map to grid")
check(g.cell(at: CGPoint(x: 101, y: 101)) == nil, "padding ignored")
check(deriveScalePadding(axWidth: 810, axHeight: 610, contentPixelsX: 1600, contentPixelsY: 1200)?.scale == 2, "Retina geometry")
check(parseHerdrClient(["herdr", "--remote", "host"]) == .remote, "remote clients skipped")
check(parseHerdrClient(["herdr", "--session", "work"]) == .local(session: "work"), "named sessions classified")
check(socketPath(session: "../work", home: "/tmp") == nil, "session traversal refused")
check(tabSpans(rowText: row(["terminal Z", "files"]), tabs: tabs(labels), startColumn: 0).count == 2, "zoom suffix is included in exact tab boxes")
check(!isSafeTerminalID("x\" & do shell script \"y"), "terminal ID injection refused")
let window = GhosttyWindow(id: "w", name: "quote\"\n\u{1f}", terminals: 1, focusedTerminal: "AB-CD")
check(terminalForWindow(titled: window.name, in: [window]) == "AB-CD", "titles stay data, including record delimiters")
check(terminalForWindow(titled: window.name, in: [window, window]) == nil, "ambiguous window refused")
let start = Date()
let timeout = runCommand("/bin/sleep", ["10"], timeout: 0.1)
check(timeout.timedOut && Date().timeIntervalSince(start) < 2, "command timeout reaps child")
let cancelled = runCommand("/bin/sleep", ["10"], timeout: 5, cancelled: { true })
check(cancelled.timedOut, "cancelled drag terminates command")
let argv = runCommand("/usr/bin/printf", ["%s", "$(touch /tmp/never-created); \"quoted\""], timeout: 1)
check(argv.output == "$(touch /tmp/never-created); \"quoted\"", "command arguments are never shell evaluated")
// H1: plugin registration in herdr's plugins.json.
let exists: (String) -> Bool = { $0 == "/plugins/herdr-spring-load/herdr-plugin.toml" }
func registry(_ entries: [[String: Any]]) -> Data { try! JSONSerialization.data(withJSONObject: entries) }
let entry: [String: Any] = ["plugin_id": "herdr-spring-load", "enabled": true, "manifest_path": "/plugins/herdr-spring-load/herdr-plugin.toml"]
check(pluginRegistration(registry([entry]), pluginID: "herdr-spring-load", fileExists: exists) == .registered, "registered plugin runs")
check(pluginRegistration(registry([entry.merging(["enabled": false]) { $1 }]), pluginID: "herdr-spring-load", fileExists: exists) == .disabled, "disabled plugin detected")
check(pluginRegistration(registry([entry.merging(["manifest_path": "/moved/herdr-plugin.toml"]) { $1 }]), pluginID: "herdr-spring-load", fileExists: exists) == .manifestMissing, "moved manifest detected")
check(pluginRegistration(registry([["plugin_id": "other", "enabled": true, "manifest_path": "/x"]]), pluginID: "herdr-spring-load", fileExists: exists) == .missing, "uninstalled plugin detected")
check(pluginRegistration(nil, pluginID: "herdr-spring-load", fileExists: exists) == .missing, "no registry file means not installed")
check(pluginRegistration(Data("{not json".utf8), pluginID: "herdr-spring-load", fileExists: exists) == .unreadable, "unreadable registry never triggers removal")
check(registrationAction(.missing, seenRegistered: true) == .remove, "uninstalled after registration: self-remove")
check(registrationAction(.missing, seenRegistered: false) == .pause, "not yet registered (install build step): only pause")
check(registrationAction(.disabled, seenRegistered: true) == .pause, "disabled: inert, not removed")
check(registrationAction(.manifestMissing, seenRegistered: true) == .pause, "moved manifest: inert, not removed")
check(registrationAction(.unreadable, seenRegistered: true) == .pause, "unreadable registry: inert, not removed")
check(registrationAction(.registered, seenRegistered: false) == .run, "registered: runs")
if CommandLine.arguments.count == 3 {
    // Registry written by the real herdr (see test_helper.py).
    let data = FileManager.default.contents(atPath: CommandLine.arguments[1])
    print("registry \(pluginRegistration(data, pluginID: "herdr-spring-load", fileExists: { FileManager.default.fileExists(atPath: $0) }))")
}

// M8: the probe key goes only to a window confirmed to run a local herdr client.
let local = ClientCandidate(pid: 10, ttyPath: "/dev/ttys001", client: .local(session: nil))
let other = ClientCandidate(pid: 11, ttyPath: "/dev/ttys002", client: .local(session: "work"))
let remote = ClientCandidate(pid: 12, ttyPath: "/dev/ttys003", client: .remote)
check(probeCandidate([(local, true, true)]) == local, "one fitting foreground local client receives the probe")
check(probeCandidate([]) == nil, "plain shell window: no herdr client, no probe key")
check(probeCandidate([(local, true, false)]) == nil, "herdr suspended behind a shell (not foreground): no probe key")
check(probeCandidate([(local, false, true)]) == nil, "herdr in a window of another size: no probe key")
check(probeCandidate([(remote, true, true)]) == nil, "remote client (custom commands cleared): no probe key")
check(probeCandidate([(local, true, true), (other, true, true)]) == nil, "two candidate windows: ambiguous, no probe key")
check(probeCandidate([(local, true, true), (remote, true, true)]) == nil, "remote window of the same size: ambiguous, no probe key")
check(probeCandidate([(local, true, true), (other, false, true), (remote, true, false)]) == local, "non-fitting and background clients are ignored")
check(parseHerdrClient(["ssh", "host", "herdr"]) == nil, "ssh running herdr remotely is not a local client")

// H1: unanswered probes back off instead of injecting keys forever.
var backoff = ProbeBackoff(limit: 3, pause: 60)
check(backoff.allows(at: 0) && !backoff.miss(at: 0) && !backoff.miss(at: 1), "first misses keep probing")
check(backoff.miss(at: 2) && !backoff.allows(at: 30), "third miss pauses probe keys")
check(backoff.allows(at: 62), "probing resumes after the pause")
backoff.answered()
check(!backoff.miss(at: 70), "an answer resets the miss count")
exit(failures == 0 ? 0 : 1)
