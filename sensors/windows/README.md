# Windows sensor (experimental)

The herdr-spring-load sensor for Windows, speaking contract 1 of
`docs/SENSOR.md`. herdr on Windows is a beta (ConPTY), and this sensor has not
run on Windows yet: everything here is unit tested on macOS with faked Win32.

Python 3.11+ standard library only (`ctypes` with user32, kernel32, dwmapi and
ntdll). It observes and never acts: no hooks, no injected input, no reading of
drag data, no elevation, no permission prompt. The only memory it reads from
another process is the environment block of a herdr process (see below).

| File | Role |
|---|---|
| `sensor.py` | Poll loop and NDJSON output; `--check`, `--verbose` |
| `logic.py` | Pure logic: drag and click detection, window choice, client discovery |
| `win32.py` | The only module that calls Win32 (`RealWin32`), plus the console probe |
| `structs.py` | ctypes structures with fixed-width fields |
| `build.py` | Packs the sensor into `herdr-spring-load-sensor-windows.exe` |
| `run_sensor.py` | Runs the sensor from a checkout |

## Coordinates

`coords` is `windows-virtual`. The sensor makes itself per-monitor DPI aware
(V2) before anything else, so the cursor and window frames are both physical
pixels in the virtual screen. If that fails, the hello leaves out `drag` and
`click`, and a status error explains why. Window `bounds` are the DWM extended
frame bounds (the visible frame, without the invisible resize border), falling
back to `GetWindowRect`. `scale` is `GetDpiForWindow / 96`.

## Drag heuristic

Windows has no global drag pasteboard, and the dragged data belongs to the
source and target only, so `kind` is always `"unknown"`. A drag starts when
all of these hold:

1. The primary button is held (`GetAsyncKeyState`; swapped buttons honoured).
2. The pointer has moved past the system drag threshold (`SM_CXDRAG`,
   `SM_CYDRAG`) since the press.
3. Either the thread of the pressed window holds the mouse capture with a
   hidden window (`GetGUIThreadInfo`; OLE's `DoDragDrop` captures the mouse
   with a hidden tracking window), or the cursor is not one of the system
   `IDC_*` cursors (`GetCursorInfo`) while over a different top-level window
   than the one pressed.

A text selection or window move inside one window fails rule 3 and is never
a drag. `drag_end` is `released` when the button comes up, and `cancelled`
when Esc is pressed or the hidden capture disappears while the button is
still held. After a cancel nothing starts until the button is released.

A click is a press and release within 8 pixels and 500 ms that did not
become a drag.

No drop-target overlay is used. OLE finds the drop target under the cursor,
so a click-through overlay never sees the drag, and one that does would also
take ordinary clicks and the drop itself.

## Window and clients

The window is `WindowFromPoint` plus `GetAncestor(GA_ROOT)`. When that window
is not a normal one (shell surfaces, the drag image, tool, transparent,
cloaked, minimised, fully transparent or off-screen windows), the sensor
walks the z-order with `EnumWindows` instead. `peers` are the owner's other
normal windows.

Clients come from a `CreateToolhelp32Snapshot` process list: every
`herdr.exe` below the window's process. Each link must have a parent created
no later than its child (`GetProcessTimes`), because Windows reuses pids; an
unknown time breaks the chain. A classic console window may report the
attached process itself as its owner, so for `ConsoleWindowClass` the owner
counts too. `argv` comes from `NtQueryInformationProcess`
(`ProcessCommandLineInformation`), split with Windows' own rules. `tty` and
`foreground`'s tty meaning do not exist on Windows. `rows` and `cols` come from
`GetConsoleScreenBufferInfo` in a short-lived child (`--console-probe PID`,
started detached), because attaching to another console would detach the
sensor from its own. A herdr process with no console (the server) is not a
client.

Each client also carries:

- `started`: the process creation time from `GetProcessTimes`, in seconds
  since the Unix epoch.
- `env`: the contract's environment keys only (`HERDR_*`, `XDG_CONFIG_HOME`,
  `XDG_STATE_HOME`, `HOME`, `APPDATA`, `LOCALAPPDATA`, `USERPROFILE`). They are
  read from the process's PEB: `NtQueryInformationProcess`
  (`ProcessBasicInformation`) gives the PEB, whose `ProcessParameters` hold the
  environment block and its size, and `ReadProcessMemory` copies the block.
  Names are matched without case, as Windows does. This needs
  `PROCESS_QUERY_INFORMATION | PROCESS_VM_READ`, which a non-elevated process
  of the same user grants. Nothing else is read from the block.

`env` is `null` whenever the read is not certain: the process cannot be
opened (elevated, another user, gone), the sensor or the target is 32-bit,
or the block is oversized (over 1 MiB), odd-sized, short or torn (no
terminating empty entry). A client without argv also gets `null`. The core
never acts on a client with an unknown `env`, and counts it as attached to
every session, so every failure is a refusal.

`drag_start` carries `all_clients`: every `herdr.exe` in the sensor's logon
session (`ProcessIdToSessionId`; a process whose session is unknown is kept),
with `argv`, `env` and `started`, and `rows`, `cols` and `foreground` null.
The core's exactly-one-client gate counts them, and ignores the server and
CLI commands by their argv. A process whose argv cannot be read is reported
as a bare `herdr.exe` with `env` null, so it counts against the gate. If the
process list cannot be read at all, `all_clients` is `null` and the core
refuses.

Lookups happen when a drag starts and when the window under the pointer
changes; clicks reuse a lookup for up to one second.

## Windows Terminal and other tabbed terminals: fail safe

One Windows Terminal process hosts every tab and pane of its windows, and
nothing in Win32 says which tab is visible. Hidden tabs keep their console
size, so a client that fits the window may not be the one on screen. The
sensor therefore sets `foreground` to true only when:

- exactly one herdr client is below the window's process, and
- that process hosts exactly one session: one console host (`OpenConsole.exe`
  or `conhost.exe`) as a direct child, or a classic console window.

Otherwise every client is reported with `foreground: false`, so the core
refuses, and the sensor sends one `status` warning per window: run herdr
alone in its window (Windows Terminal: one tab, no splits). A terminal with
no recognised console host is refused the same way.

A herdr started outside the terminal and handed to it as the default terminal
does not descend from the terminal process, so it is not found at all.

## Building

`build.py` writes a zip application of `sensors/windows` behind pip's Windows
GUI script launcher (`w64.exe`, or `w64-arm.exe` on ARM64) and a
`#!"...\pythonw.exe"` line, the layout pip uses for its own scripts. The
launcher comes from the installed pip, or from the pip wheel that `ensurepip`
bundles. Without either, `build` raises a `RuntimeError` that says so.

## Not verified yet (needs a Windows machine)

- That `DoDragDrop` holds the capture with a hidden window, for Explorer and
  browser drags, on Windows 10 and 11; and which cursors those drags show.
- That `GetGUIThreadInfo`, `GetCursorInfo` and `GetAsyncKeyState` work when
  the source is elevated (UIPI).
- The process chain from Windows Terminal, WezTerm and Alacritty to
  `herdr.exe`, and that each tab or pane starts one console host.
- What `GetWindowThreadProcessId` reports for a classic console window.
- The PEB offsets (`ProcessParameters` at 0x20, `Environment` at 0x80,
  `EnvironmentSize` at 0x3F0) and the environment read on Windows 10 and 11,
  x64 and ARM64.
- That attaching to a ConPTY client's console from the probe returns its
  size without side effects.
- That the GUI launcher passes the redirected stdin and stdout through, so
  the core reads the NDJSON with no console window.
