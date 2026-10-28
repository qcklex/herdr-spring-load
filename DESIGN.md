# herdr-image-drop: design

This is the design note written before the code. One change since: the shipped helper also
confirms the drop in herdr's focused pane before opening the popup, and uses Quick Look otherwise.
The README section "How it works" describes the current behaviour.

Goal: when an image file is dragged onto a herdr pane in Ghostty, a preview opens on its own.

## 1. What herdr 0.9.3 gives us (checked against `herdr api schema --json`)

- There is no paste, drop or input event. The subscription types are workspace.*, worktree.*, tab.*,
  pane.created/closed/updated/focused/moved/exited/agent_detected/agent_status_changed/scroll_changed,
  layout.updated and `pane.output_matched`. Plugin `[[events]]` hooks only fire on lifecycle events.
- `pane.output_matched` is the only content signal: `events.subscribe` with
  `{type:"pane.output_matched", pane_id, source:"recent"|"visible"|..., match:{type:"regex", value}}`.
  It is scoped to one pane (an unknown pane id rejects the whole request), and the event carries
  `matched_line` plus a `read` snapshot. `herdr pane wait-output <pane> --regex ...` is the one-shot CLI form.
  `pane.read --source recent-unwrapped` reads the screen. All of these only see what the pane app echoes;
  `pane.updated` fires on title changes, not output.
- Startup hooks are one-shot. The docs say they are "not supervised daemons", and plugins must stop
  detached background processes. A long-running watcher therefore cannot live cleanly inside the plugin.
- Popups: a manifest pane with `placement = "popup"` is session-modal and closes when its command exits.
  The 0.9.3 CLI `--placement` lacks `popup`, so set it in the manifest and open with
  `herdr plugin pane open --plugin <id> --entrypoint viewer --env IMAGE=<path>` (`ui_busy` if a modal is open).
- Pane graphics: apps write Kitty graphics to their own pane; herdr forwards them (`pane.graphics.*` is gone).

## 2. Prior art (read before writing this)

None of them auto-opens an image when its path is dropped.
- ecylmz/herdr-drop: closest. Drop, then press a key; it reads the path off the prompt (`pick_path`:
  last quoted/unquoted token, `file://` decoded, `isfile`) and uploads it over the pty. Manual.
- pglira/herdr-image-hints: keypress puts hint labels on image paths on the screen; picking one opens a
  Kitty-graphics popup. Manual. It does expose `herdr-image-hints open <image>`, which we could reuse as a viewer.
- hx-w/herdr-visuals: keybinding-opened split browsing images/Mermaid/LaTeX from agent sessions.
- haretoke/herdr-image-viewer: history of images an agent `Read`s, fed by a Claude Code hook. Not drop.
- walid-mos/herdr-focus-pane: a native macOS helper (Swift LaunchAgent) that watches Ghostty drags so the
  drop lands in the pane under the cursor. It shows that detecting the drag natively works, but it opens nothing.

## 3. Target terminal

Ghostty. It supports Kitty graphics and herdr has
`kitty_graphics = true`, so an in-herdr popup can draw the image. Ghostty pastes a drop as a
shell-escaped path (backslash before spaces); multiple files arrive space-separated.

## 4. Why not watch pane output

Checked and rejected as the primary mechanism:
- Inconsistent in the main use case (both observed on 2026-10-28). In Claude Code a
  dropped PNG/JPG becomes `[Image #N]`, so the path never shows. A dropped HEIC (the iPhone default) stays
  as the plain path. Scraping would work for HEIC only; apps that don't echo input (vim, less) hide both.
- Cost. One `pane.output_matched` subscription per pane (59+ now), re-subscribed on pane
  created/closed/moved, and no place to host the loop because startup hooks are one-shot.
- False positives. `ls`, build logs and agents quoting `foo.png` all match an image-path regex, and
  separating "just pasted" from "printed" needs cursor and prompt heuristics.

## 5. Recommended design: detect the drag gesture natively, show the preview through herdr

Two parts in one repo.
A. `image-drop-watch`: a small native macOS helper (Swift, about 150 lines, run as a LaunchAgent the way
   herdr-focus-pane does it; the user installs it). It needs no Accessibility or Input Monitoring permission:
   1. Poll `NSPasteboard(name: .drag).changeCount` every 100 ms. On a change, read
      `readObjects(forClasses: [NSURL], options: [.urlReadingFileURLsOnly: true])` and keep files whose UTType
      conforms to `public.image` (png, jpg, gif, heic, webp, tiff, bmp). Ignore drags with no images.
   2. While an image drag is live, poll `NSEvent.pressedMouseButtons`. When it drops to 0, take
      `NSEvent.mouseLocation` and walk `CGWindowListCopyWindowInfo(.optionOnScreenOnly, kCGNullWindowID)`
      front to back. The drop counts only if the first window containing the point belongs to Ghostty
      (bundle `com.mitchellh.ghostty`), layer 0.
   3. Debounce: one open per drag (remember the changeCount), at most N files (default 4), and the file
      must exist (screenshot-thumbnail promises may land a moment later, so retry `isfile` for up to 1 s).
      Screenshot drags come from `$TMPDIR/TemporaryItems/NSIRD_screencaptureui_*/`, which is deleted soon
      after and blocked by TCC (a shell here gets "Operation not permitted"). The viewer copies the file to
      `HERDR_PLUGIN_STATE_DIR` at once. Test first: the helper only passes the path and the popup (under
      Ghostty's TCC identity) reads it; if that fails too, the helper needs Full Disk Access.
   4. Open the preview (below); log to `~/Library/Logs/herdr-image-drop.log`.

B. The herdr plugin `herdr-image-drop` (the display side; no startup hook, nothing long-lived):
   ```toml
   [[panes]]
   id = "viewer"
   title = "Image"
   placement = "popup"
   width = "80%"
   height = "80%"
   command = ["python3", "viewer.py"]   # reads IMAGE / IMAGES env, draws Kitty graphics, any key closes
   [[actions]]
   id = "preview-last"                  # re-open the last dropped image (path read from HERDR_PLUGIN_STATE_DIR)
   ```
   The helper runs `"$HERDR" plugin pane open --plugin herdr-image-drop --entrypoint viewer --env IMAGE=<abs path>`,
   herdr from `/opt/homebrew/bin/herdr` or config (a LaunchAgent has no `HERDR_BIN_PATH`; default socket).
   Kitty `f=100` takes PNG only (no HEIC, no JPEG), so viewer.py runs every image (HEIC, JPG, WebP, TIFF,
   large PNG) through `sips -s format png -Z 1600 <in> --out <state>/<hash>.png` before sending Kitty
   chunks. Quick Look and Preview open HEIC natively.
   With several files, j/k steps through them.

Fallback chain if the popup open fails (`ui_busy`, no attached client, server down, or the drop is in a
non-herdr Ghostty window): `qlmanage -p <file>` (Quick Look, transient). A config key
`viewer = "popup" | "quicklook" | "preview"` (`open -a Preview`) lives in
`$(herdr plugin config-dir herdr-image-drop)/config.toml`, and the helper reads it too.

## 6. Edge cases

- Escaped spaces, quotes, `file://`: not an issue, because paths come from the drag pasteboard as NSURLs,
  not from parsing echoed text. `preview-last` falls back to herdr-drop's `pick_path` approach for manual use.
- Multiple files: one popup with the images in order; non-image files are ignored.
- HEIC (iPhone default): matched by `public.image` (`public.heic`), converted with sips as above.
- SSH or `herdr --remote` panes: the file is on the Mac, so a local preview is correct (the pasted path is
  meaningless remotely; out of scope). The popup appears on the local client.
- Agent TUIs work, because detection does not depend on what the app echoes.
- A browser drag (image data, no file URL) or a file promise that never lands: skip and log.
- Ghostty window not in herdr: still previews (gate on `herdr pane current` for a stricter mode).
- Popup is modal: the key that closes it is consumed and does not reach the agent prompt.

## 7. False positives

- A drag released over Ghostty but cancelled (Esc during the drag, or a drop Ghostty refuses): it still
  previews, because pasteboard plus mouse-up cannot tell a cancel from a drop. This is rare and harmless.
- A drop on Ghostty's title bar or tab bar: previews, and Ghostty pastes the path anyway. Fine.
- Not affected: image paths printed by `ls` or agents, typed by hand, or pasted with Cmd-V (by design).

## 8. Testing without touching the live herdr

`plugin link` is global to the user across all sessions, so do not link it during development. Test
viewer.py by running it directly in a scratch Ghostty tab outside herdr (Ghostty draws Kitty graphics itself),
and test the helper with `viewer = "quicklook"`. Link it yourself when you are ready.
