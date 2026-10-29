# herdr-spring-load: design

Goal: while a file drag is held over a Ghostty window running herdr, pausing
over a sidebar space opens it, over a tab switches to it, and over a pane
focuses it. Ghostty's native drop then pastes the path into the focused pane.
The plugin never intercepts, consumes or replaces the drop.

This replaces the 0.1.0 design (image preview on drop). Source references are
to herdr v0.9.3.

## 1. Constraints from herdr and macOS

- **No drag events.** herdr has no drag, drop or hover event and does not
  publish where it draws tabs or sidebar entries. A native helper outside
  herdr has to watch the drag and the pointer.
- **A drag owns the mouse.** While Finder runs a drag, the terminal receives
  no mouse-move or click events, so herdr cannot hit-test the pointer. The
  helper has to.
- **Several clients, one server.** Several Ghostty windows, each attached to
  the same or a different herdr session, can be on screen at once. Every
  action must reach the client under the pointer and no other.
- **Startup hooks are one-shot,** and plugins must not leave detached
  processes. The long-running watcher is therefore a user LaunchAgent that
  the build step installs, not part of the plugin's runtime commands.
- **No uninstall hook.** `herdr plugin uninstall` removes the registry entry
  and deletes the checkout, and runs nothing from the plugin.
- **Permissions.** Reading another app's window and text needs
  Accessibility. Driving Ghostty through AppleScript needs Automation consent
  for Ghostty. Polling `NSEvent` and the drag pasteboard needs neither, so no
  Input Monitoring.

## 2. Parts

| Part | Where | Role |
|---|---|---|
| Helper | `helper/main.swift`, `helper/DragLogic.swift`, `helper/FocusPaneVendor.swift` | LaunchAgent `dev.qcklex.herdr-spring-load`: watches drags, hit-tests, sends navigation |
| Probe actions | `probe.py`, actions `probe-1` to `probe-7` | Run inside herdr when the receiving client gets a private key, and report that client's context |
| Setup | `scripts/build.sh`, `setup.py` | The manifest `[[build]]` step and the `setup` pane: build, sign, install, add keybindings, start the LaunchAgent |
| Removal | `helper/uninstall.sh`, `uninstall.py` | The `remove-helper` action, and self-removal by the helper |
| Status | `scripts/status.sh`, action `status` | Read-only: is the helper running |

`scripts/build.sh` finds a Python 3.11 or newer (setup parses TOML with
`tomllib`) and runs `setup.py`, or `uninstall.py` with `--uninstall`.
`helper/install.sh` is the manual path from a clone: setup plus
`herdr plugin link`.

`DragLogic.swift` holds the pure logic (dwell, hit-testing, tab and space
spans, client selection, registry parsing, bounded commands) so it can be
unit tested without AppKit. `FocusPaneVendor.swift` is vendored from
herdr-focus-pane (MIT): cell geometry, `pane.layout` parsing, tty size and the
AX text-area lookup.

## 3. Drag detection

A 50 ms main-thread timer compares `NSPasteboard(name: .drag).changeCount`.
A change while the left button is held, carrying `public.file-url` or a file
promise, starts a drag generation, provided the registration check in
section 9 allows it. Release ends it. Any change of window, target or
generation increments an epoch. Work queued on the worker thread checks
generation, epoch and the live button state before acting, so a slow step
never fires after the drag ended or moved on. The release is left entirely
to Ghostty.

## 4. Finding the client: probe slots and nonce

Geometry alone cannot prove which herdr client and session a Ghostty window
shows, and titles are not trustworthy. So the helper asks the client itself.

1. **Window.** The pointer's window comes from `CGWindowListCopyWindowInfo`
   and must belong to `com.mitchellh.ghostty`. Exactly one AX window must
   match its bounds, and it must hold exactly one terminal text area, so
   Ghostty splits are refused.
2. **Terminal.** Ghostty's AppleScript, run as JXA and read back as JSON,
   lists the windows. The window must be the only one with that title, and its
   selected tab must have exactly one terminal. That terminal id is the only
   target for keys and clicks.
3. **Client check.** The probe key would be typed into the terminal if no
   herdr client received it. So, before sending anything, the helper lists
   processes with `ps` and needs exactly one herdr client under the Ghostty
   process that is in the foreground of its tty and whose tty pixel size fits
   the AX frame. That client must be local. Otherwise nothing is sent. This
   is why two herdr windows of the same size get no navigation: all Ghostty
   windows share one process, and both clients fit.
4. **Probe.** The helper writes `probes/request-N.json` with a fresh random
   token and a timestamp, then sends `ctrl+alt+shift+F<N>` (press and
   release) to that one terminal. Setup binds that chord to the action
   `probe-N`.
5. **Reply.** herdr runs `probe-N` in the context of the client that received
   the key. A keybinding-invoked plugin action carries the receiving client's
   own focused workspace, tab and pane (`src/client/shell/actions.rs`, where
   the command-invoke params are built from that client's snapshot).
   `probe.py` checks that the request is under 3 s old and reads
   `pane.layout`, `tab.list` and `workspace.list`. It re-checks that the token
   is still current, then atomically writes `reply-N.json` (mode 0600) with
   the token, socket path and context.
6. **Match.** The helper accepts only a reply carrying its token, from the
   session socket of the client found in step 3. That client's tty gives the
   cell grid (`TIOCGWINSZ`); it supplies geometry only, never identity.

**Why seven pinned slots.** A probe action can run late. If every window
shared one request file, a late action from window A could answer window B's
challenge. Each Ghostty terminal is pinned to its own slot (`request-N`,
`reply-N`, key `F<N>`). A slot is freed only when its terminal no longer
exists in Ghostty, and is reused only after a 10 s quarantine (well past the
3 s request validity) with its files deleted. herdr's legacy key parser
accepts F1 to F11 with modifiers, which is why the chords are not higher
function keys. When all seven slots are taken, a new terminal waits.

**Backoff.** After three unanswered probe keys in a row, no probe key is
sent for 60 s, and the log suggests checking `herdr plugin list`.

## 5. Hit-testing

The pointer converts to a terminal cell with the vendored `CellGeometry`
(scale and padding derived from the AX frame and the tty pixel size). Then:

- **Sidebar** (columns left of the pane area): see section 6.
- **Tab row** (the row from `pane.layout` and the tab bar position recorded
  at setup): a hover target is any cell in it. Whether that cell is a tab
  label is decided when the dwell fires (section 6).
- **Pane area:** the pane rect containing the cell, from `pane.layout`.

A `DwellMachine` fires a target once after it has stayed put for its dwell
time: 600 ms for tabs and spaces, 250 ms for panes (`--tab-dwell-ms`,
`--pane-dwell-ms`). In the tab row, "stayed put" means the same row within
one column. A target re-arms only after the pointer leaves it. After any
action the context is dropped and probed again, so the next target is read
from the new layout.

## 6. Strict tab boxes and sidebar rows

A wrong click in the tab row can create a tab, toggle zoom or run a plugin
entry, and a wrong click in the sidebar can open the wrong thing. So clicks
are allowed only where the screen text proves a label is drawn there.

**Tabs.** `tabSpans` rebuilds herdr 0.9.3's tab strip exactly, left to right
from the end of the sidebar. Each tab is a box `max(8, label width + 4)`
cells wide, label centered, one blank cell between boxes. Each label is also
tried with the ` Z` zoom marker after it. Each box is compared cell by cell
with the AX row text, using wide-character cell widths. The strip must then
end in the three-cell ` + ` new-tab control. Any mismatch, truncation or
overflow returns no spans at all: tabs after an uncertain one are never
guessed, and the matcher never searches rightward for a label, where it
could find control text.

**Spaces.** `spaceSpans` reads the sidebar as one exact sequence of herdr
0.9.3's default layout: a ` spaces` header, a blank row, then every
workspace from `workspace.list` in number order as one ` <status> <label>`
row, each followed only by indented continuation rows (branch, git status)
or blank rows. Any other shape (a scrolled list, worktree groups, custom
rows, a truncated label, a scrollbar) yields no targets. A label that merely
appears somewhere never authorizes a click.

When the dwell fires, the helper re-checks everything against a fresh probe
before clicking:
- same AX frame, title, terminal and grid;
- same hit layout;
- the cell is still inside the span, and for tabs the span also contains the
  column where the dwell fired;
- the tab or space is not already focused;
- the window under the real cursor is still this one.

The click goes to the middle of the label, as a press and release sent with
Ghostty's `send mouse` commands to that terminal id, so herdr does its own
hit-testing for that client only. If the script fails between press and
release, a release is always sent. After 150 ms another probe checks that
herdr landed on the expected tab or workspace. If not, tab and space clicks
stop for the rest of that drag.

## 7. Panes: native direction aliases, no public `pane.focus`

herdr 0.9.3's public socket `pane.focus` acts on the session, not on one
client (`src/app/api.rs`, `handle_pane_focus`). A per-client focus exists
only on the client-shell endpoint (`src/server/headless/client_views.rs`,
where `PaneFocus` maps to `focus_shell_client_on_tab` for that client id),
and plugins cannot reach that endpoint. Calling the public `pane.focus` would
therefore move focus for other clients attached to the same session, not
just the window under the pointer. The helper never calls it.

Instead, setup appends private chords to herdr's own focus-direction
shortcuts:

| Direction | Chord | Kept original |
|---|---|---|
| left | `ctrl+alt+shift+F8` | `focus_pane_left` (default `prefix+h`) |
| right | `ctrl+alt+shift+F9` | `focus_pane_right` (default `prefix+l`) |
| up | `ctrl+alt+shift+F10` | `focus_pane_up` (default `prefix+k`) |
| down | `ctrl+alt+shift+F11` | `focus_pane_down` (default `prefix+j`) |

A key sent to one Ghostty terminal is handled by that terminal's herdr
client, so focus changes for that client only. The route from the focused
pane to the target is a breadth-first search over read-only `pane.neighbor`
calls (at most 64 panes). Each step sends one direction chord and probes
again. The step must land on the expected pane, or navigation stops.

## 8. Setup: install and play

`herdr plugin install` shows a preview and asks for confirmation, then runs
the manifest's `[[build]]` command, `sh scripts/build.sh`, in a temporary
checkout with stdin closed and the herdr runtime variables removed. herdr
checks that the manifest is unchanged, moves the checkout into
`<herdr config dir>/plugins/github/` and registers the plugin. So setup must
not record checkout paths, cannot prompt, and runs before the plugin is
registered.

`setup.py` runs as one transaction. Every side effect registers its undo,
and any failure, including SIGTERM or SIGHUP, rolls everything back:

1. Parse the config with `tomllib` and prepare the edit. Refuse on any
   conflict: one of the eleven private chords bound elsewhere (compared
   canonically: case, order, `control`/`option`/`meta` aliases),
   `ui.mouse_capture = false`, or an edit that would change any setting
   besides the managed ones.
2. Run `herdr config check` on the current and the edited config, and refuse
   if the edit adds a diagnostic.
3. Find `swiftc` through `xcode-select` and `xcrun`, so a missing toolchain
   fails cleanly instead of opening the install dialog.
4. Take `setup.lock`. Compile the helper into a temporary directory and run
   `--validate`.
5. Sign it (section 10) and verify the signature against its designated
   requirement.
6. Only now stop the previous helper of this label, and wait until launchd
   has torn it down. Swap the binary in.
7. Copy `setup.py`, `uninstall.py` and `probe.py` to
   `Application Support/herdr-spring-load/lib`, and write `state.json`: the
   Python used, the removal script, the config path, and which focus fields
   and `[keys]` table setup created.
8. If the config changed during the build, abort. Otherwise back it up
   (keeping the three newest backups) and write it atomically. A symlinked
   config is written through the link.
9. Write the LaunchAgent plist and bootstrap it, retrying while launchd
   still reports the old job.
10. Reload the config through `server.reload_config` on the default socket
    and every named session's socket that exists. A stale socket is skipped.
    A server that rejects the config, or reports a disabled binding, fails
    setup. On rollback, those sessions are reloaded again once the old config
    is back.

The `setup` pane runs the same script with `--pause`, so its output stays
readable until Enter.

## 9. Removal and the missing uninstall hook

`uninstall.py` works out the restored config before touching anything,
using `state.json`: it removes the probe block and the four focus chords,
deletes focus fields and a `[keys]` table that setup created, and writes a
user's own shortcut back in its original form. Then, in one transaction, it
backs up and writes the config, reloads every running session, and removes
the binary, plist, state and lib copies. It stops the LaunchAgent last.
`--purge` also deletes the signing keychain and the whole support folder.

Because herdr deletes the checkout on uninstall, the helper keeps its own
copy of the removal scripts and reads herdr's `plugins.json` whenever a file
drag starts:

| Registry state | Action |
|---|---|
| registered | Run, and record that it was seen registered |
| missing, after it was seen registered | Run `lib/uninstall.py --self`, then exit |
| missing, never seen registered | Pause (the build step runs before herdr registers the plugin) |
| disabled, manifest missing, or unreadable | Pause |

Pausing logs a message and shows one notification with
`herdr plugin enable herdr-spring-load` and the exact removal command. Setup
resets the "seen" marker on every install.

## 10. Signing

macOS ties the Accessibility grant to a code signature. An ad hoc signature
changes with every build, so every update would need a new grant. Setup
therefore creates, once, a self-signed code signing certificate
(`herdr-spring-load local signing`) in a private keychain at
`Application Support/herdr-spring-load/signing/signing.keychain-db`, never in
the login keychain. Each build is signed with identifier
`dev.qcklex.herdr-spring-load`, so the designated requirement (identifier
plus certificate leaf) stays the same across rebuilds.

The keychain is locked except while signing. `codesign` finds identities
only through the user's keychain search list, so the keychain is added to
the list for the signing call and always taken off again. A run that was
killed earlier is cleaned up at the next setup. Unlocking reads the keychain
password on stdin. Only the one-time keychain creation passes it as a
command-line argument to `security`.

The password is stored in a file next to the keychain, mode 0600, because
the build step has no terminal. A program already running as the user could
read it and sign itself as the helper. Avoiding that needs either a prompt on
every update or a prebuilt binary signed with an Apple Developer ID.

## 11. Bounded work and cleanup

- Every external command (`osascript`, `ps`) runs through `runCommand`. It
  has a deadline and a cancellation check, sends SIGTERM then SIGKILL on
  overrun, and always reaps. Output goes through a temp file, so a child that
  holds the pipe open cannot block the helper.
- herdr socket requests have connect, send and receive deadlines. AX
  messaging has a 0.3 s timeout.
- An advisory lock (`helper.lock`) allows only one helper per user.
- On SIGTERM, SIGINT or SIGHUP the helper cancels the drag, stops running
  children, releases a possibly pressed Ghostty mouse button and exits.
- The log rotates to `.log.old` at about 1 MB.

## 12. Verification

The exact commands and results are in the README's "Test evidence"
section. In short: 48 unit and flow tests (Swift logic compiled and run
without the watcher, `probe.py` against fake sockets, setup and removal in a
scratch HOME with a stubbed toolchain), plus an opt-in isolated test that
drives a real herdr 0.9.3 server and client in a pseudo-terminal through
probe, space click, tab click, direction key and a pasted path.

Not yet verified: the macOS Accessibility and Automation path, Ghostty
delivering the helper's AppleScript keys and clicks, a real Finder drag, the
real keychain and `codesign` path with the grant surviving a rebuild, and
`herdr plugin install` from GitHub end to end.
