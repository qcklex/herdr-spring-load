# Sensor contract (version 1)

herdr-spring-load 0.3.0 is split in two:

- **The core** (`spring_load/`, Python 3 standard library only, the same code on
  macOS, Linux and Windows). It holds everything that is about herdr: the
  replicated herdr 0.9.3 tab-bar and sidebar renderer, click calibration, the
  exactly-one-local-client gate, dwell timing, and the only actions the plugin
  ever takes: herdr's public `workspace.focus`, `tab.focus` and `pane.focus`.
- **A sensor per OS** (`sensors/<os>/`). This is a small process that only *observes*:
  the pointer, whether a drag is held, the window under the pointer, and the
  herdr client processes behind that window. It prints newline-delimited JSON
  on stdout. A sensor never acts: no key injection, no synthetic clicks, no
  focus or raise calls, no pasteboard or selection reads beyond "a drag
  started", and no OS permission prompt of any kind.

The plugin never handles the drop. herdr and the terminal take the file once
the right pane is focused; a sensor must not read, change or consume drag data.

## 1. Transport

- The core starts the sensor as a child process. It passes no arguments
  except those in section 6, and leaves stdin open as a pipe.
- The sensor writes UTF-8 JSON objects to stdout, one per line, `\n`
  terminated. There are no pretty-printed or multi-line objects, and no other
  output on stdout. Diagnostics go to stderr (the core logs them) or to a
  `status` event.
- **Lifetime.** The sensor exits with status 0 when its stdin reaches EOF
  (the core closed it or died). It must also exit if writing to stdout fails
  (EPIPE).
- **Rates.** While a drag is held, send `drag_move` at most every 30 ms. Send
  one whenever the position or the window changes, and at least every 250 ms
  even when nothing changes. When no drag is held, send nothing except
  `click` and `status` events.
- **Flushing.** Flush after every line.
- **Size.** A line must be under 64 KiB. The core drops longer lines.

## 2. Common fields

Every event has these fields:

| field | type | meaning |
|---|---|---|
| `type` | string | event type, from section 3 |
| `t` | number | seconds on a monotonic clock owned by the sensor. Never decreases. Only differences matter. |

Unknown fields must be ignored by the core, so a sensor may add fields.
Unknown `type`s are ignored too. Missing required fields make the core drop
that event (and log it once).

### Coordinates

`x`, `y` and every `bounds` use **one global screen space per sensor**,
declared in `hello.coords`:

| `coords` | OS | origin | unit |
|---|---|---|---|
| `"macos-points"` | macOS | top-left of the main display, y grows down (CoreGraphics global space) | points |
| `"x11-root"` | Linux X11 | top-left of the root window | pixels |
| `"windows-virtual"` | Windows | top-left of the virtual screen, y grows down | physical pixels (the sensor is per-monitor DPI aware) |

Within a sensor, pointer points and window bounds must be in the same space.
Numbers may be fractional.

### Window object

`window` is either `null` (no top-level window under the pointer, or one
that cannot be described) or:

```json
{
  "id": "8231",
  "pid": 665,
  "app": "com.mitchellh.ghostty",
  "bounds": {"x": 0, "y": 25, "width": 1920, "height": 1164},
  "scale": 2.0,
  "peers": [{"x": 0, "y": 25, "width": 1920, "height": 1164}],
  "clients": [ ... ]
}
```

| field | type | meaning |
|---|---|---|
| `id` | string | opaque window id, stable while the window exists (CGWindowID, X11 window id, HWND as decimal) |
| `pid` | integer | pid of the process that owns the window |
| `app` | string or null | bundle id, WM_CLASS or exe name. Logging and calibration keys only; never needed for correctness |
| `bounds` | rect | outer frame of the window in the sensor's space, titlebar included when the OS reports it that way |
| `scale` | number or null | backing pixels per coordinate unit for this window's display (2.0 on Retina, 1.0 on X11, null if unknown) |
| `peers` | rect array | bounds of the owner's **other** on-screen top-level windows (same `pid`, normal layer, visible, not minimised). The core uses this for the "two same-size windows" ambiguity rule. `[]` when there are none. |
| `sessions` | integer or null, optional | how many terminal sessions (distinct controlling ttys) the owner app runs: below `pid`, plus its session servers when clients were found through them. When it exceeds `1 + len(peers)`, the app has tabs or splits and herdr may be in a hidden one, so the core refuses and says why. The Windows sensor's one-session rule is the same idea. null or absent: not checked. |
| `clients` | client array | herdr client processes shown in this window, from section 2.1: those that descend from `pid`; when there are none, those the sensor can tie to the window otherwise (see below) |

**Clients that do not descend from the window owner.** Some terminals host
their sessions in a separate server (iTerm2's session server, WezTerm's mux),
and herdr may run inside tmux or screen, whose servers are daemons. A sensor
may then list clients it can tie to the window in a way that cannot pick a
client of another window, and marks each with an extra `"via"` string. The
macOS sensor does two things, only when nothing descends from the owner:

- `"tmux"`: a tmux client runs below the owner; its own tmux server's
  `list-clients` (read-only) names the pane tty it shows, and a herdr client
  on that tty is listed.
- `"responsible"`: herdr clients whose macOS responsible process is the window
  owner (an app), except those inside a tmux or screen server.

Anything ambiguous (screen, other servers, several candidates) stays
unlisted or is refused by the core's fit, same-size-peer and one-client rules,
so the window stays inert.

The core must treat the window found by the sensor as the topmost normal
window under the pointer. The sensor skips desktop, menu-bar, dock, overlay,
fully transparent and off-screen windows.

### 2.1 Client object

A herdr *client* is any process whose executable base name is `herdr` (or
`herdr.exe`) and that has a controlling terminal, at any depth below the window
owner `pid` in the process tree. The sensor reports every one it finds, and
does **not** classify them: the core parses `argv` (local, `--session`,
`--remote`, server or CLI) and applies the gates.

```json
{"pid": 56533, "argv": ["herdr"], "tty": "/dev/ttys078",
 "rows": 68, "cols": 239, "xpixel": 1916, "ypixel": 1160, "foreground": true}
```

| field | type | meaning |
|---|---|---|
| `pid` | integer | client pid |
| `argv` | string array | full argv, unmodified. Empty array if it cannot be read |
| `tty` | string or null | controlling tty path, null on Windows or when unknown |
| `rows`, `cols` | integer or null | grid size from `TIOCGWINSZ` on the tty (or the console API), null if unknown |
| `xpixel`, `ypixel` | integer | pixel size of the text grid from the same call, in the units of `bounds` or in backing pixels. 0 when the terminal does not report it, or when unknown; the core then never acts on that client. Windows classic consoles use visible columns and rows times the font cell measured by `GetCurrentConsoleFontEx`, reconciled with DPI and `GetClientRect`; Windows Terminal and other ConPTY terminals report 0 and stay inert |
| `grid_origin` | object `{"x": number, "y": number}`, optional | classic Windows console text origin relative to the window bounds, in physical pixels, measured with `ClientToScreen`. Both coordinates must be finite and nonnegative. The core accepts only a whole-cell text grid fully contained in the window; native frame and scrollbar widths are excluded. The sensor refreshes geometry when the frame, client area or DPI changes. Other sensors omit this field and keep interval-based calibration. |
| `foreground` | boolean or null | true when the client's process group is the foreground group of its tty (`tcgetpgrp`), null when unknown |
| `started` | number or null | optional. Process start time in seconds since the Unix epoch (macOS `kinfo_proc.p_starttime`, Linux `/proc/<pid>/stat` field 22 plus boot time, Windows `GetProcessTimes`). herdr's client reads `config.toml` when it starts, so the core trusts its replica of that config only when the file is older than the client, or after a calibration click confirmed it. When it is missing, the core asks `ps` where it can. |
| `env` | object or null | optional. The values of these variables in the client's environment, when the sensor can read them: `HERDR_SESSION`, `HERDR_SOCKET_PATH`, `HERDR_CLIENT_SOCKET_PATH`, `HERDR_CONFIG_PATH`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `HOME` (and on Windows `APPDATA`, `LOCALAPPDATA`, `USERPROFILE`). Keys that are unset are left out. `null` or a missing field means "could not read". herdr picks its session and directories from these, so the core binds a client to a session only when they are known. A client whose `env` is unknown is never acted on, and counts as attached to every session for the one-client gate. Only these keys may be sent; never the rest of the environment. |

`clients` is `[]` when none are found, and `null` when the sensor cannot walk
the process tree at all.

Reading the tty size needs `open(tty, O_RDONLY | O_NOCTTY | O_NONBLOCK)` plus
`ioctl(TIOCGWINSZ)`. This works for any process of the same user and needs no
permission. A sensor must never write to the tty.

Client lookup can be costly (process listing). Do it once when a drag starts
and again when the window `id` under the pointer changes. Repeat it at most
once per second for an unchanged window. The core never assumes the lists are
fresher than that.

## 3. Events

### `hello` (exactly once, first line)

```json
{"type": "hello", "t": 0.0, "contract": 1, "sensor": "macos", "version": "0.3.0",
 "coords": "macos-points", "capabilities": ["drag", "click", "window", "clients"]}
```

- `contract` must be `1`. If the first line is not a valid `hello` with
  `contract == 1`, the core stops the sensor, logs why and stays inert.
- `capabilities` lists what the sensor can report. If `"drag"` is missing,
  the core stays inert. If `"click"` is missing, calibration is off, so only
  targets that need no calibration (panes) can be used. If `"clients"` is
  missing, the core cannot bind a window to a client and stays inert.

### `drag_start`

```json
{"type": "drag_start", "t": 12.50, "x": 820.0, "y": 400.0, "kind": "files", "window": { ... },
 "all_clients": [ ... ]}
```

Sent when the OS shows a drag with the primary button held:

- **macOS:** `NSPasteboard(name: .drag).changeCount` increased while
  `NSEvent.pressedMouseButtons & 1` is set.
- **X11:** the owner of `XdndSelection` changed while Button1 is in the
  pointer mask.
- **Windows:** a heuristic, documented by that sensor.

`all_clients` (optional, client array or null) lists every herdr client
process of this user on the machine, wherever its window is. The core counts
the clients attached to a session from it (the exactly-one-client gate). When
it is missing or null, the core lists processes itself where it can (`ps` on
macOS and Linux) and otherwise stays inert.

`kind` is `"files"` when the drag offers file URLs or file promises (macOS:
reading the pasteboard `types` is allowed, its contents never are),
`"other"` when it offers something else, and `"unknown"` when the sensor
cannot tell. The core acts on `"files"` and `"unknown"`.

### `drag_move`

```json
{"type": "drag_move", "t": 12.53, "x": 830.0, "y": 402.0, "window": { ... }}
```

### `drag_end`

```json
{"type": "drag_end", "t": 13.90, "x": 900.0, "y": 410.0, "reason": "released"}
```

`reason` is `"released"` (the button came up, so the OS drops) or
`"cancelled"` (the sensor lost track of it, for example on sleep or when a
display is reconfigured). After either one the core cancels any pending
action. A sensor must send `drag_end` before a new `drag_start`.

### `click`

```json
{"type": "click", "t": 40.12, "x": 120.0, "y": 61.0, "window": { ... }}
```

An ordinary primary-button click by the user, outside any drag: press and
release within 8 coordinate units of each other and within 500 ms. `x`/`y`
is the press point. It is used only for calibration: the core pairs it with
the herdr focus event that follows (`workspace.focused`, `tab.focused`,
`pane.focused`) and learns where the grid sits inside that window. Polling
the button state is enough; no event tap or input-monitoring permission is
allowed. A sensor that cannot see clicks without a permission omits the
`"click"` capability.

### `status`

```json
{"type": "status", "t": 1.0, "level": "warn", "message": "XFixes missing; drag detection off"}
```

`level` is `"info"`, `"warn"` or `"error"`. The core shows the latest
`warn` or `error` in the plugin's status action.

## 4. What the core does with it (informative)

1. Validate `hello`, then wait for `drag_start`.
2. For each `drag_move` with a window, pick the client: one local client
   (by `argv`) whose grid fits `bounds` (section 2.2 of the design: leftover
   of 1.5 cells or less, at scale 1 or `scale`) and that is in the
   foreground. If the window has a same-size peer, or more than one client
   fits, do nothing.
3. Count the local clients attached to that session over all windows. Only
   when exactly one is attached may the core call `workspace.focus`,
   `tab.focus` or `pane.focus`. These are server-wide in herdr 0.9.3.
   Otherwise do nothing and report why in `status`.
4. Map the point to a cell (calibration), the cell to a target (the replicated
   renderer), and dwell on it. Then make the one API call, check the result,
   and re-read the state before the next one.
5. On `drag_end`, cancel everything. The drop is the terminal's.

## 5. The core's sensor-loading interface

`spring_load/sensor.py`:

```python
SENSOR_ENV = 'SPRING_LOAD_SENSOR'   # JSON array argv; overrides discovery (tests, dev)

def platform_key() -> str:
    """'macos', 'linux' or 'windows' (from sys.platform)."""

def sensor_command(support_dir: Path) -> list[str] | None:
    """argv of the sensor to start: $SPRING_LOAD_SENSOR if set, otherwise
    <support_dir>/sensors/herdr-spring-load-sensor-<platform_key()>[.exe]
    when it exists and is executable; None when there is none."""

class SensorProcess:
    """Starts the sensor, reads stdout on a thread, validates every line.
    Invalid lines after the hello are dropped and logged once per reason.
    Iterating yields parsed events (dicts) in order. The first yielded event is
    the hello. Raises SensorError on a bad hello or when the sensor exits.
    close() closes stdin, waits 1 s, then kills."""
    def __init__(self, argv: list[str], env: dict | None = None): ...
    def next(self, timeout: float | None = None) -> dict | None:
        """The next valid event (the hello first); None on timeout."""
    def __iter__(self): ...
    def close(self) -> None: ...

def parse_event(line: bytes | str) -> dict | None:
    """One validated event, or None for an unknown type or an invalid line."""
```

`spring_load/contract.py` holds the validation (`parse_event`,
`validate_hello`, `validate_window`, `validate_client`). Sensor tests can
import it to check their own output:

```
python3 -m spring_load.contract < recorded-output.ndjson   # exit 1 on the first invalid line
```

### Building and installing a sensor

Each `sensors/<os>/` directory has a `build.py` with:

```python
def build(out: Path) -> None:
    """Build the sensor binary at `out` (stdlib only, may call the platform
    toolchain). Raise RuntimeError with a user-facing message on failure."""
```

Setup imports `sensors/<platform_key()>/build.py`, builds into a temporary
directory, runs the result with `--check` (the sensor prints one `hello` and
exits 0, with no other side effect), and only then installs it to
`<support_dir>/sensors/`. A sensor may be a script, in which case `build`
copies it and `out` is a small launcher.

## 6. Command-line flags every sensor accepts

| flag | meaning |
|---|---|
| `--check` | print the `hello` line, then exit 0. Starts no polling and touches nothing |
| `--verbose` | extra diagnostics on stderr |

## 7. Fake sensor (tests)

`sensors/fake/fake_sensor.py SCRIPT.json` replays a scripted session:

```json
{"hello": {"sensor": "fake", "coords": "macos-points"},
 "events": [
   {"after": 0.05, "type": "drag_start", "x": 10, "y": 10, "kind": "files", "window": null},
   {"after": 0.05, "type": "drag_move", "x": 300, "y": 40, "window": {"...": "..."}},
   {"after": 0.70, "type": "drag_end", "x": 300, "y": 40, "reason": "released"}
 ],
 "exit": "eof"}
```

- `after` is the delay in seconds since the previous event. `t` is filled in
  from the fake sensor's own monotonic clock.
- `"exit": "eof"` (the default) waits for stdin EOF, as a real sensor does.
  `"exit": "now"` exits right after the last event.
- `$FAKE_SENSOR_SCRIPT` may be used instead of the argument.

Tests start it with `SPRING_LOAD_SENSOR='["python3", ".../fake_sensor.py", "script.json"]'`.
