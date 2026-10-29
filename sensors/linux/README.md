# Linux sensor

This sensor implements the sensor contract in `docs/SENSOR.md`. It is Python 3 stdlib only (ctypes for Xlib), needs no root and asks for no permission. `build.py` packs it into one executable zipapp, `herdr-spring-load-sensor-linux`. At start it picks a backend from the session:

| session | backend | capabilities |
|---|---|---|
| X11 (`DISPLAY`, no `WAYLAND_DISPLAY`) | `x11.py` | drag, click, window, clients |
| Wayland, Hyprland | `hyprland.py` | window, clients (no drag: inert) |
| Wayland, any other compositor | none | none (inert, with a status message) |

## X11

| need | how |
|---|---|
| pointer and buttons | `XQueryPointer` on the root, plus XInput2 raw button events. Without XI2, presses shorter than one 20 ms poll (touchpad taps) would be missed. Raw events need no permission on X11. |
| drag start | XFixes `SetSelectionOwnerNotify` on `XdndSelection`. Every XDND source (GTK, Qt, Electron, dragon) takes that selection when a drag begins. An owner left over from an earlier drag does not count: the button has to be held. |
| drag end | Button 1 released (`released`). `cancelled` when the source drops the selection while the button is still held. |
| drag kind | `files` or `other` from the source's `XdndTypeList`, when the source publishes one; otherwise `unknown`. GTK publishes it only with more than three types. No drag data is ever read. |
| window | The topmost mapped, non-override-redirect child of the root at the pointer, so drag icons, menus and tooltips are skipped. Then the `WM_STATE` client window inside it, its `_NET_WM_PID`, `WM_CLASS` and root-relative geometry. |
| peers | `_NET_CLIENT_LIST`, keeping only windows with the same `_NET_WM_PID` that are viewable, not hidden and on the current desktop |
| clients | `/proc/*/stat` below the window's pid. Every `herdr` with a controlling tty, with `TIOCGWINSZ` read through a read-only open of its `/dev/pts/N`, and `foreground` from `pgrp == tpgid` |

`bounds` is the client window, without the window manager's decorations. The decorated frame is the extra field `frame`.

Limits:
- `_NET_WM_PID` is advisory. A window without it is reported as `null`.
- Terminals that run every window in one server process (gnome-terminal-server, or kitty or WezTerm with a single instance) put all their herdr clients under one pid. The core's size rule then has to tell them apart.
- A remote X window (`ssh -X`) carries a pid from another host. It usually matches no local herdr client, so the core stays inert.
- Under XWayland, X11 sees only XWayland windows. The sensor therefore treats any session with `WAYLAND_DISPLAY` as Wayland.

## Wayland

Wayland gives a third-party client no global pointer, no window list, no view of other clients' drag-and-drop and no input injection. A generic Wayland sensor is impossible. What remains is per compositor:

| compositor | pointer | windows and pid | drag signal | status |
|---|---|---|---|---|
| Hyprland | IPC `j/cursorpos` | IPC `j/clients`, `j/monitors` | none | `hyprland.py`: window and clients only, so the core stays inert |
| sway and other wlroots | none in IPC | `swaymsg -t get_tree` | none | not done |
| KDE Plasma | a KWin script (`workspace.cursorPos`) | a KWin script (`windowList()`, `pid`, `frameGeometry`) | not exposed to scripts | needs an installed KWin script plus D-Bus: skipped |
| GNOME | a Shell extension (`global.get_pointer()`) | a Shell extension (window actors, `get_pid()`) | in-shell grabs only, not client DnD | needs a Shell extension per GNOME release: skipped |

Reading button state from evdev (`/dev/input`) would need the `input` group, which is a real permission. It still cannot tell a file drag from a text selection held over a tab, so no backend uses it.

The one route that works on Wayland is terminal-native: kitty's DnD protocol reports drag moves in cells to the app. That needs herdr itself to support it.

## Tests

- **Unit tests:** `python3 -m unittest tests.test_linux_sensor`. They run on macOS too and cover:
  - the tracker state machine;
  - a fake `/proc`;
  - tty decoding and the client cache;
  - Hyprland against a fake IPC socket;
  - the build, `--check` and stdin EOF.

  All output is checked with `spring_load.contract`.
- **Real drag:** `sh tests/linux/run.sh`. It runs in a Debian container with:
  - Xvfb and openbox;
  - xterm running the herdr 0.9.3 linux release binary (sha256-pinned);
  - a GTK3 file drag source.

  xdotool drives a real XDND drag through XTest, then a click. `tests/linux/check_drag.py` compares the sensor's output with ground truth from `ps` and xdotool. Set `DOCKER_CONTEXT` to choose the engine, for example a colima profile.
