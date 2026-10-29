# Changelog

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
