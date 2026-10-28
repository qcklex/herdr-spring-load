// Tests for helper/DropLogic.swift. Built and run by tests/test_helper.py:
//   swiftc helper/DropLogic.swift tests/swift/main.swift -o droplogic-tests

import Foundation

var failures = 0

func check(_ condition: Bool, _ name: String) {
    if condition {
        print("ok   \(name)")
    } else {
        print("FAIL \(name)")
        failures += 1
    }
}

// Finding 1: the popup opens only when herdr's focused pane shows the drop.

let prompt = "~/proj $ "
check(dropLanded(before: prompt, after: prompt + #"/Users/me/My\ Shot.png"#,
                 paths: ["/Users/me/My Shot.png"]),
      "escaped path pasted into a shell pane counts as landed")
check(dropLanded(before: prompt, after: prompt + "'/Users/me/a b.heic'",
                 paths: ["/Users/me/a b.heic"]),
      "quoted HEIC path counts as landed")
check(dropLanded(before: "> [Image #1]", after: "> [Image #1] [Image #2]",
                 paths: ["/Users/me/x.png"]),
      "new agent image placeholder counts as landed")
check(!dropLanded(before: prompt, after: prompt,
                  paths: ["/Users/me/x.png"]),
      "unchanged pane (drop went to another session or window) does not count")
check(!dropLanded(before: "> [Image #1]", after: "> [Image #1]",
                  paths: ["/Users/me/x.png"]),
      "existing placeholder alone does not count")
check(!dropLanded(before: "ls\nx.png\n", after: "ls\nx.png\nmore output\n",
                  paths: ["/Users/me/x.png"]),
      "file name already on screen before the drag does not count")
check(focusedPaneID(fromJSON: #"{"id":"c","result":{"pane":{"pane_id":"wY:p1"},"type":"pane_current"}}"#) == "wY:p1",
      "pane id parsed from pane current output")
check(focusedPaneID(fromJSON: "error: no server running") == nil,
      "no pane id when herdr is not reachable")

// Finding 4: a stuck command is killed at its deadline instead of blocking.

let started = Date()
let slow = runCommand("/bin/sleep", ["30"], timeout: 0.5)
let elapsed = Date().timeIntervalSince(started)
check(slow.timedOut && slow.status == -1, "stuck command reports a timeout")
check(elapsed < 3, "stuck command returns near its deadline (\(String(format: "%.2f", elapsed))s)")

// A child that ignores SIGTERM and keeps the output pipe open still returns.
let stubborn = runCommand("/bin/sh", ["-c", "trap '' TERM; sleep 30"], timeout: 0.3)
check(stubborn.timedOut && Date().timeIntervalSince(started) < 6, "child ignoring SIGTERM is killed")

let quick = runCommand("/bin/echo", ["hello"], timeout: 5)
check(quick.status == 0 && !quick.timedOut && quick.output == "hello\n", "normal command output is returned")

let missing = runCommand("/nonexistent/herdr", ["pane", "current"], timeout: 1)
check(missing.status == -1 && !missing.timedOut, "missing binary fails fast")

exit(failures == 0 ? 0 : 1)
