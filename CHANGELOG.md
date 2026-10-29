# Changelog

## [0.3.0] - Unreleased

Spring-loading with no OS permission, in any terminal. The helper now only
observes, and selects through herdr's own API.

### Added
- A sensor per OS that observes the drag, the pointer, the window under it
  and that window's herdr clients, and prints NDJSON (docs/SENSOR.md).
- macOS sensor, with no permission. It reads the pointer and mouse buttons,
  the drag pasteboard's change count and type names (never its contents),
  window bounds without window names, and herdr client processes with their
  tty size, start time and a short list of environment variables. It finds
  herdr clients under the terminal's own processes, inside tmux, and under
  the terminal's session server (macOS's responsible process); this partly
  lifts the "below the window's process" limit. It reports how many terminal
  sessions the window's app runs, so tabs and splits turn spring-loading
  off.
- A shared core in standard-library Python: a replica of herdr 0.9.3's
  sidebar, tab bar and pane layout (checked against golden screens, and
  acting only on herdr versions it was checked against), calibration from
  ordinary clicks and herdr's focus events, and herdr's public
  `workspace.focus`, `tab.focus` and `pane.focus` as the only actions.
- The core reaches herdr over its named pipe on Windows.
- Linux sensor for X11 (stdlib Python, packed as a zipapp), installed as a
  systemd user unit. Install needs python3 only. On Hyprland it is detected
  but inert: it sees windows and clients, but no drag. Other Wayland
  compositors are not supported.
- Windows sensor (experimental; the API, the gate and the grid fit run on
  real Windows in CI, a real drag has not): per-monitor DPI aware, a drag
  heuristic from the mouse capture and the cursor, the console size through
  a short-lived probe, a classic console window's text area as the pixel
  size, and the client environment from the process's PEB. Windows Terminal
  and other ConPTY terminals report no pixel size, so they stay inert. It is packed
  behind pip's script launcher and starts from a per-user Run key entry;
  the plugin entries are `status-windows`, `setup-windows` and
  `remove-helper-windows`. Install runs `python -B setup.py`.
- The manifest declares macos, linux and windows, with per-platform build
  steps, actions and setup panes.
- The status action says when the helper is not running (the core holds a
  lock while it runs, so only one helper runs per user).
- The status action reports whether the helper is running, what it is
  doing, the last reason it refused to act, which kinds of target are off
  and why, how many window shapes are calibrated, and the last navigation.
- Dwell times as setup options: `--tab-dwell-ms` (Spaces and tabs, default
  600) and `--pane-dwell-ms` (default 250).

### Changed
- Navigation acts only when exactly one local client is attached to the
  session; otherwise nothing happens and the status action says why.
- Tabs and Spaces need an ordinary click per window size first: a different
  tab once, or a different Space twice. Panes work right away.
- After a herdr config change, clicks confirm which config a running client
  shows: a click confirms only when it lands where the stored calibration
  agrees, and it never moves that calibration. Clicks confirm tabs and
  panes only; Spaces stay off until the client restarts.
- Run herdr alone in its terminal window: an app with more sessions than
  visible windows (tabs, splits) is refused, because herdr in a hidden tab
  could be navigated.
- Setup removes 0.2.0's private keybindings from the herdr config, saving
  `config.toml.spring-load-0.2.bak` first, and deletes 0.2.0's signing
  keychain.
- Self-removal after `herdr plugin uninstall` needs two checks at least 3 s
  apart and the registered checkout gone.

### Removed
- Accessibility and Automation use, AppleScript, key injection and
  synthetic clicks.
- The private probe actions and keybindings. Setup no longer edits the herdr
  config; an install of 0.2.0 has its keybindings removed again.
- The local signing keychain (deleted on upgrade).

## [0.2.0] - Unreleased

This version replaces the plugin completely. herdr-spring-load is now
spring-loaded drag navigation, and the image preview from 0.1.0 is gone.

### Added
- Spring-loaded navigation while a file is held over a Ghostty window running
  herdr:
  - pause over a space name in the sidebar to open that space;
  - pause over a tab label to switch to that tab;
  - pause over a pane to focus it.

  Ghostty's own drop then puts the path into the focused pane.
- Install and play: `herdr plugin install qcklex/herdr-spring-load` runs a
  build step that compiles, signs and installs the helper, adds the private
  keybindings (after backing up the herdr config), starts the LaunchAgent and
  reloads every running herdr session. Any failure rolls everything back.
- Private probe actions `probe-1` to `probe-7`, bound to `ctrl+alt+shift+F1`
  to `F7`. They tell the helper which herdr session, workspace, tab and pane
  the client in a given Ghostty window shows.
- Private aliases `ctrl+alt+shift+F8` to `F11` on herdr's own
  `focus_pane_*` shortcuts, so pane focus changes only for the client under
  the pointer.
- `status` action, `setup` pane and `remove-helper` action.
  `helper/uninstall.sh --purge` also deletes the signing keychain and support
  files.
- The helper removes itself on the next file drag after
  `herdr plugin uninstall`, and pauses while the plugin is disabled.
- Local code signing identity, so the Accessibility grant is meant to survive
  updates.

### Changed
- `min_herdr_version` is now 0.9.3.
- The helper (LaunchAgent `dev.qcklex.herdr-spring-load`) now needs
  Accessibility, and Automation for Ghostty.
- The manifest description and README now describe what the code does.

### Removed
- Image preview popup (`viewer` pane), `preview-last` action, HEIC
  conversion and Quick Look fallback.
- Screenshot tools (`tools/annotate.py`, `tools/make_test_images.py`) and the
  image-preview docs.

## [0.1.0] - 2026-10-28

First release, published as herdr-image-drop and renamed to
herdr-spring-load the same day. Its listing already described spring-loaded
navigation, which this version did not have.

### Added
- Image preview on drop: a macOS helper, installed by hand, watched the drag
  pasteboard. When image files were dropped onto the focused herdr pane, it
  opened a herdr popup that drew them with Kitty graphics (`j`/`k` to step
  through, HEIC converted with `sips`), with a Quick Look fallback.
- `preview-last` action to reopen the last previewed images.
