# herdr-spring-load

Spring-loaded drag and drop for herdr. While you hold a file over a terminal
window running herdr, pause over a Space in the sidebar to open it, over a
tab to switch to it, or over a pane to focus it. Then release, and the
terminal drops the file into that pane as usual.

It needs no OS permission, sends no keys or clicks to any app, and does not
edit the herdr config. It is not tied to one terminal.

![Illustrated demo: holding a file, hovering a tab, then a space in the sidebar, then a pane, and releasing it there](docs/spring-load-demo.gif)

*Illustration drawn by `tools/make_demo.py`, not a screen recording.*

## How to use it

Like spring-loaded folders in Finder, you keep holding the file while you
navigate:

1. Drag a file from Finder (or any app that drags files) over a terminal
   window running herdr, and keep the mouse button down.
2. Pause over a Space in the sidebar for 0.6 s to open that Space.
3. Pause over a tab label in the tab row for 0.6 s to switch to that tab.
4. Pause over a pane for 0.25 s to focus it.
5. Release. The terminal pastes the file into the focused pane, exactly as it
   would without this plugin.

You can open a Space, then a tab, then a pane in one drag. The plugin never
handles the drop itself.

**Once per window size:** before tabs and Spaces switch, click a different
tab once, or a different Space twice, without dragging anything. See
[Calibration](#calibration). Panes work right away.

## Platforms

| Platform | Status |
|---|---|
| macOS | Supported |
| Linux, X11 session | Supported. Covered by a container end-to-end test with xterm and a GTK drag source. |
| Linux, Hyprland | Detected but inert: Hyprland's IPC gives the pointer and the windows, but no drag signal ([sensors/linux/README.md](sensors/linux/README.md)) |
| Linux, GNOME, KDE or other Wayland compositors | Not supported (see [Known limits](#known-limits)) |
| Windows | Experimental: unit tested with faked Win32 only, not yet run on Windows ([sensors/windows/README.md](sensors/windows/README.md)) |

## Requirements

- herdr 0.9.3. The plugin acts only on herdr versions its replica was
  checked against, and does nothing on others.
- Python 3.11 or newer (the core reads herdr's TOML config with `tomllib`).
- A terminal that reports its size in pixels to the programs in it
  (`TIOCGWINSZ` `ws_xpixel` and `ws_ypixel`). Most do. Without it nothing
  works, and the status says "no herdr client in this window matches its
  size (or the terminal reports no pixel size)".
- Per OS:

| OS | Also needs |
|---|---|
| macOS | Xcode Command Line Tools (`xcode-select --install`), to compile the sensor |
| Linux | an X11 session and a systemd user session (`systemctl --user`); nothing to compile |
| Windows (experimental) | `python` on PATH with pip's launcher (`python -m ensurepip`) |

On macOS and Linux, setup looks for `python3.14` down to `python3.11`, then
`python3`, `/opt/homebrew/bin/python3` and `/usr/local/bin/python3`. The
`/usr/bin/python3` that ships with macOS is older; `brew install python`
provides a newer one. On Windows herdr runs setup with the first `python`
on PATH.

## How it works

The plugin only selects; herdr and the terminal do everything else.

- A small **sensor** per OS only observes, without any permission: whether
  a drag is held (on macOS the drag pasteboard's change count and type
  names, never its contents, and the mouse buttons; on X11 the XDND
  selection owner), the pointer position, the window under the pointer (its
  frame and owning process), and the herdr client processes behind that
  window (the process tree and each client's terminal size).
- A Python **core** (standard library only) replicates how herdr 0.9.3 lays
  out its sidebar, tab bar and panes, maps the pointer to a Space, tab or
  pane, and after the dwell time calls herdr's public `workspace.focus`,
  `tab.focus` or `pane.focus`. Those three calls are the only changes it
  ever makes to herdr.

See [DESIGN.md](DESIGN.md) for the details and
[docs/SENSOR.md](docs/SENSOR.md) for the sensor contract.

## The one-window rule

**Spring-loading acts only while exactly one herdr window is attached to
that herdr session.**

The reason is herdr itself. In herdr 0.9.3, the public `workspace.focus`,
`tab.focus` and `pane.focus` calls move *every* client attached to the
session, not only the one under the pointer. With two windows on the same
session, a drag over one window would also switch the other.

So before every action, the plugin counts the local herdr clients attached
to the session behind the window under the pointer:

- **One client:** it acts.
- **Two or more:** it does nothing, and its status says why.
- **Different sessions:** each window is its own session
  (`herdr --session <name>`), so each is counted on its own and both work.

Related rules:

- **Tabs and splits.** Run herdr alone in its terminal window: no terminal
  tabs, no splits. herdr in a hidden tab fits the window just as well as the
  one you see, so when the terminal app runs more sessions than it shows
  windows, spring-loading is off in that app, and the status says why. The
  same holds for windows minimised or on another desktop.
- **Windows of the same size.** If two windows of the same terminal app have
  the same size, the plugin cannot tell which one shows herdr, so it does
  nothing in either. Resize one of them.

## Calibration

The plugin knows the size of the terminal window and the size of herdr's
grid in cells. It does not know exactly where the grid starts inside the
window, because the title bar, the terminal's own tab bar and its padding
all vary.

- **Panes** are large enough that this does not matter, so pane focus works
  from the first drag.
- **Tabs and Spaces** are one row high, so the plugin needs to learn the
  offset first.

To teach it, click a herdr tab or Space the normal way, with nothing held.
Click one that is **not** already selected: herdr reports nothing when focus
does not change.

- The plugin matches your click with herdr's focus events. The clicked
  target's position pins down the offset.
- A tab click near the middle of a tab usually counts at once. A Space click
  usually needs a second one: before calibration a click could also be on
  the Space next to it, so the first click stays a candidate until a second
  one agrees.
- The offset is kept per terminal app and window size, in
  `calibration.json` in the support folder. After you resize the window or
  change the terminal's font size, click again.

## Install

```sh
herdr plugin install qcklex/herdr-spring-load
```

herdr shows its install preview, including the build command for your OS
(`sh scripts/build.sh` on macOS and Linux, `python -B setup.py` on
Windows), and asks you to confirm. That build step builds this OS's sensor,
checks that it speaks the sensor contract, copies the core to the support
folder and starts the helper (`python3 -m spring_load run`) as a per-user
login service:

| OS | Sensor build | Login service |
|---|---|---|
| macOS | `xcrun swiftc` | LaunchAgent `dev.qcklex.herdr-spring-load` |
| Linux | zipapp, no compiler | systemd user unit `dev.qcklex.herdr-spring-load.service`, wanted by `default.target` and `graphical-session.target` |
| Windows (experimental) | zipapp behind pip's launcher | value `herdr-spring-load` under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, run with `pythonw` (no console window); setup also starts it |

On Linux the helper takes the display (`DISPLAY`, `WAYLAND_DISPLAY`) from
the systemd user manager, which desktops fill in at login. If the manager
has none when you install, setup passes on the one it sees.

If any step fails, everything setup changed is put back and the previous
install keeps running. Setup asks for no permission and signs nothing. On
macOS, nothing is added to System Settings > Privacy & Security.

**Upgrading from 0.2.0.** Setup also removes what 0.2.0 left behind: the
private keybindings in the herdr config (it saves
`config.toml.spring-load-0.2.bak` first and reloads running sessions), the
signing keychain and the terminal-specific helper. 0.2.0's entries under
Accessibility and Automation in System Settings are no longer used; you can
remove them.

## Status and settings

| Entry | Kind | What it does |
|---|---|---|
| Spring-load helper status | action `status` | Reports whether the helper is running, what it is doing, the last reason it refused to act, which kinds of target are off and why, how many window shapes are calibrated, and the last navigation. Changes nothing. |
| Set up herdr-spring-load | pane `setup` | Runs the same setup as install again, in an overlay that waits for Enter. |
| Remove herdr-spring-load helper | action `remove-helper` | Removes the helper. See [Uninstall](#uninstall). |

herdr rejects two entries with the same id, even for different platforms, so
on Windows these are `status-windows`, `setup-windows` and
`remove-helper-windows`, with "experimental" in their titles. From a shell:

```sh
herdr plugin action invoke herdr-spring-load.status            # macOS, Linux
herdr plugin action invoke herdr-spring-load.status-windows    # Windows
```

Dwell times are setup options: `--tab-dwell-ms` (Spaces and tabs, default
600) and `--pane-dwell-ms` (default 250), each 50 to 5000. From a clone, for
example: `sh scripts/build.sh --tab-dwell-ms 800`.

Where the plugin keeps its files:

| OS | Support folder | Log |
|---|---|---|
| macOS | `~/Library/Application Support/herdr-spring-load` | `~/Library/Logs/herdr-spring-load.log` |
| Linux | `$XDG_DATA_HOME/herdr-spring-load` (default `~/.local/share/herdr-spring-load`) | `herdr-spring-load.log` in the support folder |
| Windows | `%LOCALAPPDATA%\herdr-spring-load` | `herdr-spring-load.log` in the support folder |

The log moves to `.log.old` at about 1 MB.

## Uninstall

herdr has no uninstall hook for plugins, so remove the helper first:

```sh
herdr plugin action invoke herdr-spring-load.remove-helper     # Windows: remove-helper-windows
herdr plugin uninstall herdr-spring-load
```

The first command stops the helper and deletes its login service (the
LaunchAgent, the systemd unit and its `enable` links, or the Run key value)
and the whole support folder: the sensor, the core copy, `calibration.json`
and the status files. On Linux and Windows the log is in that folder and
goes with it. On macOS the logs are kept: `~/Library/Logs/herdr-spring-load.log`,
its `.log.old` and `herdr-spring-load.stderr.log`. From a clone,
`sh helper/uninstall.sh` (Windows: `python -B uninstall.py`) does the same.

If you run `herdr plugin uninstall` first, the helper notices at the next
file drags: when herdr no longer lists the plugin on two checks at least 3 s
apart, and the checkout herdr had registered is gone, it removes itself the
same way. While the plugin is disabled, it pauses.

## Known limits

**Sessions and windows**

- **Clients the plugin cannot see.** With two clients attached to the same
  session that the plugin can see, nothing happens. A client it cannot see
  (on another machine, under another user account, in a container, or
  through a forwarded socket) is not counted, so do not attach one while
  dragging.
- **Remote windows.** `herdr --remote` windows are never acted on.
- **Tabs and splits.** See [the one-window rule](#the-one-window-rule).
- **Finding the client, macOS.** The sensor finds herdr clients that run
  under the terminal app's own processes, inside tmux (through the window's
  tmux client), and in terminals whose sessions run in a separate server
  process (through macOS's responsible process). GNU screen, and other
  session servers it cannot map to a window, stay inert.
- **Finding the client, Linux.** The window must carry `_NET_WM_PID`.
  Terminals that run every window in one process (gnome-terminal-server,
  single-instance kitty or WezTerm) rely on the size rule to tell windows
  apart. `ssh -X` windows are inert.

**Layout**

- **Spaces:** herdr's sidebar must be visible. A scrolled Space list, rows
  with text herdr computes at runtime (hostname, time, command output), or
  labels whose width the replica cannot be sure of give no Space targets.
  While the sidebar lists remote machines, Space targets are off. For 5 s
  after a Space's directory or git branch changes, its rows are not used.
- **Tabs:** tab switching is off while the tab row overflows and scrolls.
  The new-tab and other controls are never targets.
- **Config changes.** When herdr's config changed after a client started,
  the plugin cannot tell which config that client shows, and refuses until
  clicks confirm it: click a different tab twice for tabs and panes, and a
  Space below the first one twice for Spaces. A click confirms only when it
  lands where every stored calibration agrees, so it can take another
  click. Restarting the client also works.
- **A click that does not fit.** If herdr focuses a tab or Space where the
  calibration says it is not drawn, spring-loading stops for that window
  until you click a different tab or Space again and that click agrees.

**Platforms**

- **Wayland.** Wayland gives an ordinary program no global pointer, no
  window list and no view of other apps' drags. GNOME and KDE would need a
  Shell extension or a KWin script, and reading `/dev/input` would need the
  `input` group, which is a permission. So there is no spring-loading on
  Wayland; use an X11 session. On Hyprland the sensor sees windows and
  clients but no drag, so it stays inert.
- **Windows** is experimental. Windows has no system-wide drag signal, so
  the sensor infers a drag from the mouse capture and the cursor.

**Drags and other tools**

- Drags that are not files are ignored on macOS (and on X11 when the source
  publishes its types).
- A cancelled drag does not undo the navigation already done.
- Do not run it next to another drag helper such as herdr-focus-pane's drag
  assist or herdr-drag-to-tab; they would move focus during the same drag.
  Setup warns when it finds a herdr-focus-pane LaunchAgent.

## Test evidence

Run on macOS 26 (Darwin 25.4) with herdr 0.9.3 and Python 3.14, from the
repository root:

```sh
python3 -m unittest discover -s tests
```

Result: 207 tests, 1 skipped (the opt-in isolated test below). They
cover:

- the replica against golden screens recorded from herdr 0.9.3, cell
  geometry and calibration, click attribution from herdr's focus events,
  config confirmation, the one-client gate, and dwell timing;
- the herdr API over Unix sockets, and over Windows named pipes with a faked
  pipe (not yet run on Windows);
- the sensor contract;
- the macOS sensor, built and run through its test hooks: client lookup
  through the process tree, tmux and the responsible process, the session
  count, and a slow tmux server;
- `tests.test_linux_sensor`: the Linux sensor with a fake `/proc`, Hyprland
  IPC against a fake socket, and the build;
- `tests.test_windows_sensor`: the Windows sensor with faked Win32,
  including the PEB environment read, `all_clients` and the gate;
- install, update, uninstall, self-removal, rollback and 0.2.0 migration in
  a scratch HOME with stubbed `launchctl`, `xcrun` and herdr; the Linux
  install with the real sensor zipapp and a stub `systemctl`; the Windows
  Run key service with a fake `winreg`; and a check that no shipped file
  asks for a permission or injects input.

```sh
HERDR_TEST_ISOLATED=1 python3 -m unittest tests.test_isolated_herdr -v
```

This starts a real herdr 0.9.3 server and client in a pseudo-terminal, with
a throwaway HOME and config, and drives the core with a fake sensor. A click
calibrates; then held drags open a Space, a tab and a pane through herdr's
API, and the terminal's native drop still lands in that pane. With a second
client attached, nothing happens.

On Linux, `sh tests/linux/run.sh` runs a real XDND drag in a Debian
container (Xvfb, openbox, xterm running herdr 0.9.3, a GTK drag source) and
compares the sensor's output with `ps` and xdotool.

Not verified yet:

- a real Finder drag over a real terminal window, end to end;
- that reading the drag pasteboard never shows macOS's pasteboard privacy
  prompt (it reads its change count and type names, never its contents, so
  it should not);
- any run on real Windows;
- Hyprland on a real session;
- the responsible-process fallback with a real iTerm2 (the tmux fallback is
  tested with a real tmux when one is installed);
- `herdr plugin install` from GitHub end to end.

## Demo

The GIF is an illustration, not a screen recording. Regenerate it and
`docs/spring-load-demo.png` with `python3 tools/make_demo.py` (requires
Pillow).

## License

MIT, see [LICENSE](LICENSE).
