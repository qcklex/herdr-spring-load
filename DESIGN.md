# herdr-spring-load: design

Goal: while a file drag is held over a terminal window running herdr,
pausing over a sidebar Space opens it, over a tab switches to it, and over a
pane focuses it. The terminal's native drop then delivers the file to the
focused pane. The plugin never reads, intercepts or replaces the drop.

Constraints for 0.3.0: no OS permission of any kind, no key injection or
synthetic clicks, no herdr config edits, any terminal, and a structure that
takes Linux and Windows sensors. Source references are to herdr v0.9.3.

## 1. Constraints from herdr and the OS

- **No drag events.** herdr has no drag, drop or hover event. While another
  app runs a drag, the terminal gets no mouse events, so herdr cannot
  hit-test the pointer. Something outside herdr has to.
- **Focus calls are per session.** herdr 0.9.3's public `workspace.focus`,
  `tab.focus` and `pane.focus` change the session's focus, which every
  attached client follows. So the plugin acts only when exactly one local
  client is attached to the session.
- **No published geometry.** herdr does not publish where it draws tabs or
  sidebar rows, and `layouts[].area` x and y are always 0. The plugin
  replicates the layout.
- **No uninstall hook,** and plugins must not leave detached processes. The
  watcher is a login service that the build step installs.
- **Zero permission.** On macOS, `NSPasteboard(name: .drag).changeCount`,
  `NSEvent.pressedMouseButtons`, `NSEvent.mouseLocation`, the window list's
  bounds and owner pid (no titles), `sysctl`/`proc_pidpath` for the process
  tree and `TIOCGWINSZ` on a client's tty need no grant.

## 2. Parts

| Part | Where | Role |
|---|---|---|
| Sensor | `sensors/<os>/` | Observes drags, clicks, the window under the pointer and its herdr clients; prints NDJSON ([docs/SENSOR.md](docs/SENSOR.md)). Never acts. |
| Core | `spring_load/` | Standard-library Python, same code on every OS: replica, geometry, calibration, gate, dwell, focus calls, status, self-removal |
| Setup | `scripts/build.sh`, `setup.py`, `installer.py`, `legacy.py` | The `[[build]]` step and the `setup` pane: build and check the sensor, install the core, start the service, migrate 0.2.0 |
| Removal | `helper/uninstall.sh`, `uninstall.py` | The `remove-helper` action, and self-removal |

Core modules:

| Module | Role |
|---|---|
| `sensor.py`, `contract.py` | Start the sensor, validate its events |
| `clients.py` | Bind a sensor client to a herdr session socket and config (from its env); count attached clients |
| `render.py` | Replica of herdr 0.9.3's sidebar, tab bar and pane layout, with a version allowlist |
| `geometry.py` | Window frame and tty size to a cell grid with uncertain origin; calibration |
| `herdr_api.py` | herdr's API over a Unix socket, or its named pipe on Windows |
| `registration.py` | Is the plugin still registered with herdr |
| `engine.py` | The loop that ties these together |

## 3. From a pointer to a target

1. **Window and clients.** The sensor reports the window under the pointer
   (frame, owner pid) and the herdr clients that window may show: their pid,
   tty, size in cells and pixels, env, and process start time.
2. **Binding.** A client's env gives its session socket and config path.
   A client whose env is unknown is never bound, and counts as attached.
3. **Gate.** The session must have exactly one attached client. Otherwise:
   nothing, and the status action says why.
4. **Grid.** The tty's pixel size and the window frame give the cell size.
   Where the grid sits in the frame (titlebar, native tab bar, padding) is
   unknown, so the origin is an interval, and a point maps to a range of
   cells. A target counts only when every cell in that range belongs to it.
   Two windows of the terminal app with the same size are ambiguous.
5. **Model.** herdr's state (`workspace.list`, `tab.list`, `pane.layout`)
   plus its config and client preferences go through the replica, which
   returns exact cell rectangles for Spaces, tabs and panes, or refuses a
   kind with a reason: a skipped navigation is acceptable, a wrong one is
   not.
6. **Dwell.** A target fires once after the pointer has stayed on it for
   0.6 s (Spaces, tabs) or 0.25 s (panes), and re-arms after the pointer
   leaves it. A target that is already focused is not offered, and after a
   focus call the core re-reads herdr's state before the next target.

## 4. Calibration from ordinary clicks

Spaces and tabs are one row high, so they need the grid origin to within
half a row. The plugin learns it from the user's own clicks, never from
synthetic ones.

- The sensor reports clicks (button up, no drag held) with their position.
- The core subscribes to herdr's focus events for every session at startup.
  On a focus change herdr emits workspace, tab and pane events together; the
  click's level is the shallowest one whose id changed. herdr focuses a
  Space or tab on mouse up, so the event may arrive before the click is
  reported: events from 0.5 s before to 0.4 s after the click count.
- A click is attributed only when the changes form one burst with one
  distinct id at that level. A click on an already-focused target emits
  nothing and teaches nothing. Clicks within 1 s after a drag are ignored.
- The attributed target's rectangle bounds the origin. Calibration is kept
  per window shape (app, frame size, grid) in a JSON file.
- **First calibration.** When the click's cell range could also cover
  another target of the same kind, the result stays a candidate until a
  second click agrees.
- **Disagreement.** A click that contradicts the stored origin becomes a
  candidate and replaces it only when a second click agrees. Until then, and
  after a click no origin can explain, the window is stale: no navigation
  there until a later tab or Space click agrees.

## 5. Which config does the client show

herdr loads its config when a client starts and on reload, with no event for
either. If the config file is older than the client process, the client
shows it. Otherwise the plugin cannot tell, and refuses until an ordinary
click confirms it, per kind: a click on a different tab confirms tabs and
panes; a click on a Space below the first confirms Space rows (the first row
sits where every config puts it). The first calibration never confirms
anything. A changed config file refuses again.

## 6. Sidebar rows and git

Space rows can show the git branch and status of the Space's directory.
herdr caches those, so right after a `cd` or a branch switch the replica
may draw something herdr has not yet. The core records each Space's
directory when it subscribes, and refuses a Space's rows for 5 s after the
directory or its git HEAD changes.

## 7. Setup

`herdr plugin install` runs `sh scripts/build.sh` in a temporary checkout
before registering the plugin, with stdin closed. `build.sh` finds Python
3.11 or newer and runs `setup.py`, which is one transaction (every step
registers its undo; a failure or SIGTERM rolls everything back):

1. Take `setup.lock`. If 0.2.0's keybindings are in the config, work out the
   restored config first and refuse if that is not possible.
2. Build the sensor (`sensors/<os>/build.py`; on macOS `xcrun swiftc` after
   checking `xcode-select -p`) and run its `--check`, which must print one
   valid `hello`.
3. Stage the core and the removal scripts, and check that they import with
   the chosen Python.
4. Only now stop the previous service, and swap in `sensors/` and `lib/`.
5. Write `state.json`, apply 0.2.0's config restoration if needed, write the
   LaunchAgent and start it.
6. After commit, delete 0.2.0's signing keychain if present.

No permission, no signing, no config edit (beyond undoing 0.2.0's). The
service is behind a small registry in `installer.py`, so a systemd user unit
or a Windows equivalent can be added next to `Launchd`.

## 8. Removal and the missing uninstall hook

`uninstall.py` stops the service and deletes its definition, the sensor, the
core copy and the support files, restoring 0.2.0's config edits if they are
still there. Because herdr deletes the checkout on uninstall, the core reads
herdr's `plugins.json` when a drag starts:

| Registry state | Action |
|---|---|
| registered | Run, and record that it was seen registered |
| missing on two checks at least 3 s apart, after it was seen registered, and the recorded checkout gone | Run `lib/uninstall.py --self`, then exit |
| missing, never seen registered | Pause (the build step runs before herdr registers the plugin) |
| disabled, manifest missing, or unreadable | Pause |

## 9. Verification

See the README's "Test evidence". In short: unit and golden tests for the
replica, geometry, calibration, attribution, gate and dwell; the macOS
sensor built and run through its test hooks; install, removal and migration
in a scratch HOME; and an opt-in test that drives a real herdr 0.9.3 server
and client through calibration, Space, tab and pane focus, a native drop,
and the two-client refusal.
