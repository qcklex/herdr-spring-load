# herdr-spring-load

Spring-loaded drag and drop for herdr on macOS. While you hold a file over a
Ghostty window running herdr, pause over a space in the sidebar to open it,
over a tab to switch to it, or over a pane to focus it. Then release, and
Ghostty drops the file path into that pane as usual.

![Illustrated demo: holding a file, hovering a tab, then a space in the sidebar, then a pane, and releasing it there](docs/spring-load-demo.gif)

*Illustration drawn by `tools/make_demo.py`, not a screen recording.*

## How to use it

Like spring-loaded folders in Finder, you keep holding the file while you
navigate:

1. Drag a file from Finder (or any app that drags files) over a Ghostty window
   running herdr, and keep the mouse button down.
2. Pause over a space name in the sidebar for 0.6 s to open that space.
3. Pause over a tab label in the tab row for 0.6 s to switch to that tab.
4. Pause over a pane for 0.25 s to focus it.
5. Release. Ghostty pastes the file path into the focused pane, exactly as it
   would without this plugin.

You can open a space, then a tab, then a pane in one drag. The plugin never
handles the drop itself.

## Requirements

- macOS, with Ghostty 1.3 or newer. Ghostty's AppleScript support must be on.
  It is on by default, so only check that your Ghostty config does not set
  `macos-applescript = false`.
- herdr 0.9.3 or newer, with mouse capture on (`ui.mouse_capture`, on by
  default).
- Xcode Command Line Tools (`xcode-select --install`), used to compile the
  helper.
- Python 3.11 or newer. The `/usr/bin/python3` that ships with macOS is older;
  `brew install python` provides a newer one. Setup looks for `python3.14`
  down to `python3.11`, then `python3`, `/opt/homebrew/bin/python3` and
  `/usr/local/bin/python3`.

## Install

```sh
herdr plugin install qcklex/herdr-spring-load
```

herdr shows its install preview, including the one build command
(`sh scripts/build.sh`), and asks you to confirm. That build step does the
whole setup:

- compiles the helper with `swiftc` and checks that the binary runs;
- signs it with a local certificate kept in its own keychain, so macOS
  permissions can survive updates (see
  [Signing and security](#signing-and-security));
- installs it in `~/Library/Application Support/herdr-spring-load`;
- adds the helper's private keybindings to the herdr config, after saving a
  backup (see [What setup changes](#what-setup-changes));
- starts the helper as the per-user LaunchAgent
  `dev.qcklex.herdr-spring-load`, so it also starts at login;
- reloads the config in every running herdr session, the default one and any
  named ones. Sessions that are not running pick the keybindings up when they
  start. No herdr server has to be running during install.

If any step fails, everything setup changed is put back, herdr reports the
failure, and the plugin is not installed. Setup also stops before changing
anything when Python 3.11, the command line tools or herdr's own config check
is not satisfied, or when the config has a conflict (see below).

### First use: two macOS permissions

macOS asks twice, and only you can answer:

1. **Accessibility.** Right after install, the helper asks macOS for
   Accessibility, and macOS adds `herdr-spring-load` to System Settings >
   Privacy & Security > Accessibility. Turn it on. The helper uses it to read
   the Ghostty window's geometry and the text of herdr's tab row and sidebar.
   Until it is on, the helper does nothing.
2. **Automation for Ghostty.** The first time a file drag reaches a Ghostty
   window, macOS asks for permission to control Ghostty. Allow it. The helper
   uses Ghostty's AppleScript to send its private keys to that one terminal
   and to click a tab or space label there.

Input Monitoring and Screen Recording are not needed.

### What setup changes

- The herdr config: `$HERDR_CONFIG_PATH`, otherwise
  `$XDG_CONFIG_HOME/herdr/config.toml` or `~/.config/herdr/config.toml`. A
  symlinked config stays a symlink. First a backup is saved next to it as
  `config.toml.spring-load-<time>.bak`; the three newest are kept. Then:
  - a marked block binds `ctrl+alt+shift+F1` to `F7` to the plugin's private
    `probe-1` to `probe-7` actions;
  - `ctrl+alt+shift+F8` to `F11` are added to `focus_pane_left`, `right`,
    `up` and `down`. Your existing focus shortcuts are kept.
- Setup refuses, and changes nothing, if any of those eleven chords is
  already bound to something else, if `ui.mouse_capture` is off, if
  `herdr config check` reports a new problem with the edited config, or if
  the edit would change any other setting.
- The tab bar position (`tab_bar_position`) is read from the config during
  setup. If you move the tab bar later, run setup again.

## Actions and the setup pane

The plugin adds these to herdr's plugin menu:

| Entry | Kind | What it does |
|---|---|---|
| Spring-load helper status | action `status` | Reports whether the helper is running. Changes nothing. |
| Set up herdr-spring-load | pane `setup` | Runs the same setup as install again, in an overlay that waits for Enter. Use it to repair the install or after moving the tab bar. |
| Remove herdr-spring-load helper and keybindings | action `remove-helper` | Removes the helper. See [Uninstall](#uninstall). |
| Spring-load client probe 1 to 7 | actions `probe-1` to `probe-7` | Private. Bound to the probe keys; not meant to be run by hand. |

From a shell:

```sh
herdr plugin action invoke herdr-spring-load.status
```

Logs: `~/Library/Logs/herdr-spring-load.log` (moved to `.log.old` at about
1 MB), and `~/Library/Logs/herdr-spring-load.stderr.log` for the LaunchAgent.

## Uninstall

herdr has no uninstall hook for plugins, so remove the helper first:

1. Run the plugin action "Remove herdr-spring-load helper and keybindings",
   or from a shell:

   ```sh
   herdr plugin action invoke herdr-spring-load.remove-helper
   ```

   It stops the helper and deletes its LaunchAgent and binary. It takes out
   of the herdr config exactly what setup added: the probe block and the four
   focus chords. A focus shortcut that setup had to create is removed again,
   and a shortcut you had keeps the form you wrote it in. The config is
   backed up first (`config.toml.spring-load-uninstall.bak`) and reloaded in
   every running session. If anything fails, nothing is removed.
2. Then unregister the plugin:

   ```sh
   herdr plugin uninstall herdr-spring-load
   ```

3. Optionally, remove `herdr-spring-load` from Accessibility and Automation
   in System Settings > Privacy & Security.

The signing keychain and the logs are kept, so a reinstall keeps the macOS
grants. To also delete the keychain and everything in
`~/Library/Application Support/herdr-spring-load`, run this instead of
step 1, while the plugin is still installed:

```sh
sh ~/.config/herdr/plugins/github/herdr-spring-load-1b5077dac181/helper/uninstall.sh --purge
```

That is where herdr 0.9.3 keeps the installed checkout (under
`$XDG_CONFIG_HOME/herdr` if you set it). From a clone, run
`sh helper/uninstall.sh --purge`. macOS then asks for Accessibility again on
a later install.

### If you run `herdr plugin uninstall` first

herdr deletes the plugin's checkout but cannot tell the helper. The helper
keeps its own copy of the removal scripts, and checks herdr's plugin list
whenever a file drag starts:

- If it has seen the plugin registered before and the entry is now gone, it
  runs the same removal as `remove-helper` on that drag, then exits.
- If it never saw the plugin registered (you uninstalled before your first
  file drag), if the plugin is disabled, or if herdr's entry points to a
  manifest that no longer exists, it does nothing during drags. It writes to
  its log, and shows a notification once, with
  `herdr plugin enable herdr-spring-load` and the exact command that removes
  it.

Until one of those happens, the LaunchAgent stays loaded and the private
chords stay in the herdr config.

### Upgrading

Run the install command again. Setup builds and signs the new helper first,
and only then stops the old one and swaps it in. If the plugin was linked
from a local clone, run `herdr plugin unlink herdr-spring-load` first.

0.1.0 was a different tool, an image previewer. Setup replaces its helper,
which used the same LaunchAgent label. If you installed 0.1.0 under its
first name, herdr-image-drop, remove that agent once:

```sh
launchctl bootout "gui/$(id -u)/dev.qcklex.herdr-image-drop"
rm -f ~/Library/LaunchAgents/dev.qcklex.herdr-image-drop.plist
```

## Known limits

- **Not yet tried on a real drag.** See [Test evidence](#test-evidence) for
  what is and is not verified.
- **Ghostty only.** Windows split into several Ghostty terminals are not
  supported. Two Ghostty windows with the same title are ambiguous, and the
  helper does nothing in either.
- **Two herdr windows of the same size do nothing.** Before it sends its
  probe key, the helper needs exactly one local herdr client under Ghostty,
  in the foreground of its terminal, whose pixel size fits the window under
  the cursor. With two herdr windows of the same size, or one next to a
  remote herdr window of the same size, it does nothing rather than risk
  typing into the wrong one.
- **Local clients only.** `herdr --remote` clients are skipped.
- **Spaces:** only herdr's default sidebar layout works: the "spaces"
  header, then one label row per space, all visible and drawn in full. A
  scrolled space list, worktree groups, custom sidebar rows, truncated labels
  or a collapsed sidebar give no space targets.
- **Tabs:** a tab switches only when every tab label is drawn in full,
  followed by the new-tab control. If labels overflow or one is truncated,
  tab switching is off for that row. The new-tab, zoom and plugin controls
  are never clicked.
- **Panes:** focus moves one neighbour at a time with herdr's own
  focus-direction keys, so it passes through the panes in between. If herdr
  lands somewhere unexpected, navigation stops.
- If a tab or space click lands somewhere other than expected, tab and space
  switching stop for the rest of that drag.
- Up to seven Ghostty terminals can be in use at a time. Another one waits
  until one of them is closed.
- If three probe keys in a row get no answer (for example while the plugin
  is disabled), the helper sends none for 60 seconds.
- A cancelled drag does not undo the navigation already done.
- Do not run it next to another drag helper such as herdr-focus-pane's drag
  assist or herdr-drag-to-tab; they would move focus during the same drag.
  Setup warns when it finds a herdr-focus-pane LaunchAgent.

## Signing and security

Every build is signed with the same self-signed local certificate, under the
identifier `dev.qcklex.herdr-spring-load`. macOS ties the Accessibility grant
to that signature rather than to one binary, so the grant is meant to survive
rebuilds and updates. This has not been verified on a real update yet.

The certificate's private key lives in its own keychain in
`~/Library/Application Support/herdr-spring-load/signing`, never in your
login keychain. The keychain is locked except while signing, and is on the
keychain search list only during the signing call. Its password is in a file
next to it, readable only by your user, because herdr runs the build step
without a terminal and setup cannot ask you for it.

This is a trade-off: a program already running as your user could use that
key to sign itself as the helper and inherit its permissions. Avoiding that
would mean a password prompt on every update, or a prebuilt binary signed
with an Apple Developer ID.

## Test evidence

Run on macOS 26 (Darwin 25.4) with herdr 0.9.3, Ghostty 1.3.1 and Python
3.14, from the repository root:

```sh
python3 -m unittest discover -s tests
```

Result: 48 tests, 47 pass and 1 is skipped (the opt-in test below). They
cover:

- the Swift logic, compiled with `swiftc` and run without the watcher: dwell
  timing, tab boxes, sidebar space spans, cell geometry, choosing the herdr
  client to probe, probe backoff, the bounded command runner, and reading
  the plugin list exactly as herdr 0.9.3 writes it;
- the helper building and passing `--validate` without starting;
- `probe.py` against fake herdr sockets (nonce, expiry, receiving client);
- setup's config rewriting, conflict checks and `herdr config check`;
- install, update, uninstall, `--purge`, self-removal and rollback in a
  scratch HOME, with stubs for `launchctl`, `security`, `codesign`, `swiftc`
  and herdr, including the build step run the way `herdr plugin install`
  runs it.

```sh
HERDR_TEST_ISOLATED=1 python3 -m unittest discover -s tests -p test_isolated_herdr.py -v
```

Result: pass. It starts a real herdr 0.9.3 server and client in a
pseudo-terminal, with a throwaway HOME and config, and never touches a normal
herdr session. In that client, the private probe key gets a probe reply; a
click on a space name and then on a tab label switches that client; the
private right-focus key focuses the destination pane; and a pasted file path
lands in that pane.

Not verified yet:

- the macOS side: the helper running as a LaunchAgent, the Accessibility and
  Automation prompts, and reading Ghostty through Accessibility;
- Ghostty delivering the helper's AppleScript keys and clicks (the isolated
  test writes the equivalent key and mouse sequences straight into the
  terminal);
- a real Finder drag from start to drop;
- the real keychain and `codesign` path, and the Accessibility grant
  surviving a rebuild;
- `herdr plugin install` from GitHub end to end.

## Credits

The cell geometry, pane layout parsing, tty size and Accessibility text-area
lookup in `helper/FocusPaneVendor.swift` come from
[herdr-focus-pane](https://github.com/walid-mos/herdr-focus-pane) by Walid
Mostefaoui, MIT licensed. Its copyright and licence notice are kept in that
file.

## Demo

The GIF is an illustration, not a screen recording. Regenerate it and
`docs/spring-load-demo.png` with `python3 tools/make_demo.py` (requires
Pillow).

## License

MIT, see [LICENSE](LICENSE).
