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

**Once per window size:** Spaces and tabs switch only after you have clicked
a different tab or Space once in that window, without dragging anything. The
plugin cannot see where the terminal draws herdr inside its window
(titlebar, native tabs, padding); that click shows it. Panes are large
enough to work right away.

## How it works

The plugin only selects; herdr and the terminal do everything else.

- A small **sensor** per OS only observes, without any permission: whether
  a drag is held (on macOS the drag pasteboard's change count and the mouse
  buttons, on X11 the XDND selection owner; never the dragged data), the
  pointer position, the window under the pointer (its frame and owning
  process), and the herdr client processes behind that window (the process
  tree and each client's terminal size).
- A Python **core** (standard library only) replicates how herdr 0.9.3 lays
  out its sidebar, tab bar and panes, maps the pointer to a Space, tab or
  pane, and after the dwell time calls herdr's public `workspace.focus`,
  `tab.focus` or `pane.focus`.
- herdr's focus calls move every client attached to that session, so the
  core acts only when exactly one local client is attached. Otherwise it
  does nothing and says why in the status action.

See [DESIGN.md](DESIGN.md) for the details and
[docs/SENSOR.md](docs/SENSOR.md) for the sensor contract.

## Requirements

- herdr 0.9.3. The plugin acts only on herdr versions its replica was
  checked against, and does nothing on others.
- Python 3.11 or newer (the core reads herdr's TOML config with `tomllib`).
- Per OS:

| OS | Status | Also needs | Sensor |
|---|---|---|---|
| macOS | supported | Xcode Command Line Tools (`xcode-select --install`), to compile the sensor | Swift, no permission |
| Linux | supported on X11 | a systemd user session (`systemctl --user`); nothing to compile | Python zipapp. Hyprland: windows and clients only, so it stays inert; other Wayland compositors: inert ([sensors/linux/README.md](sensors/linux/README.md)) |
| Windows | **experimental**, not yet run on Windows | `python` on PATH with pip's launcher (`python -m ensurepip`) | Python behind pip's script launcher ([sensors/windows/README.md](sensors/windows/README.md)) |

On macOS and Linux, setup looks for `python3.14` down to `python3.11`, then
`python3`, `/opt/homebrew/bin/python3` and `/usr/local/bin/python3`. The
`/usr/bin/python3` that ships with macOS is older; `brew install python`
provides a newer one. On Windows herdr runs setup with the first `python`
on PATH.

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

| OS | Sensor build | Support folder | Login service | Log |
|---|---|---|---|---|
| macOS | `xcrun swiftc` | `~/Library/Application Support/herdr-spring-load` | LaunchAgent `dev.qcklex.herdr-spring-load` | `~/Library/Logs/herdr-spring-load.log` |
| Linux | zipapp, no compiler | `$XDG_DATA_HOME/herdr-spring-load` (default `~/.local/share/...`) | systemd user unit `dev.qcklex.herdr-spring-load.service`, wanted by `default.target` and `graphical-session.target` | in the support folder |
| Windows (experimental) | zipapp behind pip's launcher | `%LOCALAPPDATA%\herdr-spring-load` | value `herdr-spring-load` under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, run with `pythonw` (no console window); setup also starts it | in the support folder |

On Linux the helper takes the display (`DISPLAY`, `WAYLAND_DISPLAY`) from
the systemd user manager, which desktops fill in at login. If the manager
has none when you install, setup passes on the one it sees.

If any step fails, everything setup changed is put back and the previous
install keeps running. Setup asks for no permission and signs nothing.

**Upgrading from 0.2.0.** Setup also removes what 0.2.0 left behind: the
private keybindings in the herdr config (it saves
`config.toml.spring-load-0.2.bak` first and reloads running sessions), the
signing keychain and the terminal-specific helper. 0.2.0's entries under
Accessibility and Automation in System Settings are no longer used; you can
remove them.

## Actions and the setup pane

| Entry | Kind | What it does |
|---|---|---|
| Spring-load helper status | action `status` | Reports whether the helper is running, what it is doing, which targets are off and why, how many window shapes are calibrated, and the last navigation. Changes nothing. |
| Set up herdr-spring-load | pane `setup` | Runs the same setup as install again, in an overlay that waits for Enter. |
| Remove herdr-spring-load helper | action `remove-helper` | Removes the helper. See [Uninstall](#uninstall). |

herdr rejects two entries with the same id, even for different platforms, so
on Windows these are `status-windows`, `setup-windows` and
`remove-helper-windows`, with "experimental" in their titles. From a shell:

```sh
herdr plugin action invoke herdr-spring-load.status            # macOS, Linux
herdr plugin action invoke herdr-spring-load.status-windows    # Windows
```

The log is moved to `.log.old` at about 1 MB.

## Uninstall

herdr has no uninstall hook for plugins, so remove the helper first:

```sh
herdr plugin action invoke herdr-spring-load.remove-helper     # Windows: remove-helper-windows
herdr plugin uninstall herdr-spring-load
```

The first command stops the helper and deletes its login service (the
LaunchAgent, the systemd unit and its `enable` links, or the Run key value),
the sensor, the core copy and the support files. From a clone,
`sh helper/uninstall.sh` (Windows: `python -B uninstall.py`) does the same.

If you run `herdr plugin uninstall` first, the helper notices at the next
file drags: when herdr no longer lists the plugin on two checks at least 3 s
apart, and the checkout herdr had registered is gone, it removes itself the
same way. While the plugin is disabled, it pauses.

## Known limits

- **One client per session.** With two clients attached to the same herdr
  session (two windows, or a local and a remote one), nothing happens: a
  focus call would move both.
- **Finding the client (macOS).** The sensor finds herdr clients that run under the
  terminal app's own processes, inside tmux (through the window's tmux
  client), and in terminals whose sessions run in a separate server process
  (through macOS's responsible process). GNU screen, and other session
  servers it cannot map to a window, stay inert. Two windows of the same
  size in one terminal app are ambiguous, and the plugin does nothing in
  either.
- **Spaces:** herdr's sidebar must be visible. A scrolled Space list, rows
  with text herdr computes at runtime (hostname, time, command output), or
  labels whose width the replica cannot be sure of give no Space targets.
  For 5 s after a Space's directory or git branch changes, its rows are not
  used.
- **Tabs:** tab switching is off while the tab row overflows and scrolls.
  The new-tab and other controls are never targets.
- **Config changes.** When herdr's config changed after a client started,
  the plugin cannot tell which config that client shows. A click on a
  different tab confirms tabs and panes, and a click on a Space below the
  first confirms Spaces. Restarting the client also works.
- **A click that does not fit.** If herdr focuses a tab or Space where the
  calibration says it is not drawn, spring-loading stops for that window
  until you click a different tab or Space again and that click agrees.
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

It covers the replica against golden screens recorded from herdr 0.9.3, cell
geometry and calibration, click attribution from herdr's focus events, the
one-client gate, dwell timing, the herdr API over Unix sockets and Windows
named pipes, the sensor contract, the macOS sensor (built and run through
its test hooks: client lookup through the process tree, tmux and the
responsible process), and install, update, uninstall, self-removal, rollback
and 0.2.0 migration in a scratch HOME with stubbed `launchctl`, `xcrun` and
herdr. The Linux install (real sensor zipapp, stub `systemctl`), the Windows
Run key service (fake `winreg`), and both merged sensors' own unit tests run
in the same suite on macOS. One test checks that no shipped file asks for a permission or injects
input.

```sh
HERDR_TEST_ISOLATED=1 python3 -m unittest tests.test_isolated_herdr -v
```

This starts a real herdr 0.9.3 server and client in a pseudo-terminal, with
a throwaway HOME and config, and drives the core with a fake sensor. A click
calibrates; then held drags open a Space, a tab and a pane through herdr's
API, and the terminal's native drop still lands in that pane. With a second
client attached, nothing happens.

Not verified yet:

- a real Finder drag over a real terminal window, end to end;
- that reading the drag pasteboard's change count never shows macOS's
  pasteboard privacy prompt (it reads no contents, so it should not);
- `herdr plugin install` from GitHub end to end.

## Demo

The GIF is an illustration, not a screen recording. Regenerate it and
`docs/spring-load-demo.png` with `python3 tools/make_demo.py` (requires
Pillow).

## License

MIT, see [LICENSE](LICENSE).
