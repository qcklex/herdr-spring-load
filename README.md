# herdr-spring-load

Automatically preview image files dropped into herdr panes on macOS.



It also works in agent TUIs such as Claude Code, which swap a dropped image
for an `[Image #1]` placeholder: the dropped file is taken from the macOS drag
itself, not parsed out of the pane. The preview is a herdr popup that draws
the image with Kitty graphics, and `j`/`k` step through several files.

macOS only. Supported files: png, jpg, jpeg, gif, heic, heif, webp, tif, tiff,
bmp. HEIC/HEIF is converted with `sips` before it is drawn.

## How it works

There are two parts, and they are installed separately.

1. **The herdr plugin** (this repo, installed with `herdr plugin install`)
   declares a popup pane, `viewer`, and one action, `preview-last`. Nothing
   in it runs in the background.
2. **The drag helper**, `herdr-spring-load`, is a small native macOS program
   (Swift). herdr has no drop or paste event, so the drag has to be seen
   outside herdr. You build it with `swiftc` and run it as a per-user
   LaunchAgent, using the script in `helper/`.

What happens on a drop:

1. The helper polls the system drag pasteboard (10 times a second). When a
   drag that carries image files starts, it asks herdr for the focused pane
   (`herdr pane current`) and reads that pane's recent text
   (`herdr pane read`).
2. When the mouse button is released over the terminal app (Ghostty by
   default), the helper waits up to 1.5 seconds for that same pane to show
   the drop: the file name, or a new `[Image #N]` placeholder.
3. If it does, the helper runs
   `herdr plugin pane open --plugin herdr-spring-load --entrypoint viewer`
   with the image paths, and the popup opens over the herdr session.
4. If it does not (a plain terminal tab, a named or remote herdr session, a
   cancelled drag), or herdr cannot open the popup, the helper shows the
   files in Quick Look instead.
5. In the popup, `j`/`k` or the arrow keys step through the files, and any
   other key closes it. That key does not reach the pane.

## Requirements

- macOS
- herdr 0.9.0 or newer, with `kitty_graphics = true` in the herdr config
- A terminal with Kitty graphics support (Ghostty by default)
- `python3` (the one that ships with macOS is enough)
- Xcode command line tools, for `swiftc` to build the helper
  (`xcode-select --install`)

## Install

1. Install the herdr plugin:

   ```sh
   herdr plugin install qcklex/herdr-spring-load
   ```

   This installs only the herdr side: the popup viewer and the
   `preview-last` action. On its own it does not react to drops.

2. Build and start the drag helper from a checkout of this repo:

   ```sh
   git clone https://github.com/qcklex/herdr-spring-load
   sh herdr-spring-load/helper/install.sh
   ```

   `install.sh` compiles the helper with `swiftc` into
   `~/Library/Application Support/herdr-spring-load/herdr-spring-load`, writes
   `~/Library/LaunchAgents/dev.qcklex.herdr-spring-load.plist` and loads it
   with `launchctl`, so it also starts at login.

   For a terminal other than Ghostty, pass its bundle id:
   `sh herdr-spring-load/helper/install.sh --terminal com.googlecode.iterm2`.

Remove the helper with `sh herdr-spring-load/helper/uninstall.sh` and the
plugin with `herdr plugin uninstall herdr-spring-load`.

### Permissions

The helper needs no Accessibility, Input Monitoring or Screen Recording
permission, and macOS does not prompt for any. It only reads the drag
pasteboard, the mouse button state and pointer position, and the list of
on-screen windows (to see which app is under the pointer). It talks to herdr
through the `herdr` CLI on its default socket. The pane text it reads to
confirm a drop is kept in memory and not written anywhere.

## Use

- Drop one or more image files onto a herdr pane. A popup shows the first
  image; `j`/`k` (or the arrow keys) step through several. Any other key
  closes it.
- Action `herdr-spring-load.preview-last` reopens the last images shown. If
  there are none, it takes the image paths from the lowest line of the
  focused pane that has any. Bind it like any plugin action:

  ```toml
  [[keys.command]]
  key = "prefix+i"
  type = "plugin_action"
  command = "herdr-spring-load.preview-last"
  description = "preview last image"
  ```

## Configure

Optional `config.toml` in `$(herdr plugin config-dir herdr-spring-load)`:

```toml
viewer = "popup"   # popup | quicklook | preview
max_files = 4      # images shown per drop
```

The helper reads the same file. If the popup cannot open (another herdr modal
is up, no client is attached, or the server is down), it falls back to Quick
Look.

## Known limits

- This repository provides image previews. Hovering to switch tabs, panes or
  workspaces is not implemented here.

- The popup opens only when the drop shows up in the focused pane of the
  default herdr session (the file name, or a new `[Image #N]` placeholder
  from an agent). A drop anywhere else opens Quick Look: a plain shell, a
  named session (`herdr --session`), a remote client (`herdr --remote`), or a
  drop that does not reach the pane. A very quick drop, released before the
  helper has read the pane, can also fall back to Quick Look.
- A drag released over the terminal but cancelled (Esc) opens Quick Look;
  mouse-up alone cannot tell a cancel from a drop.
- The helper only checks which app owns the window under the pointer. A
  release over another app's window that sits in front of the terminal (the
  Dock, a floating panel) can open Quick Look although nothing was dropped.
- Images dragged from a browser (image data, no file) are skipped.
- Dragging the floating screenshot thumbnail straight into a pane has not been
  tested; that file lives in a protected temporary folder.
- The preview opens in addition to whatever the pane does with the drop; an
  agent that attaches the image still attaches it.
- The popup opens over the whole herdr session, not inside the pane the file
  was dropped on.
- Drops into `ssh` sessions inside a local pane are previewed like local
  drops. The preview is always of the file on your Mac.

Helper log: `~/Library/Logs/herdr-spring-load.log` (capped at about 1 MB; it
contains the paths of previewed images). Add `--verbose` to the helper to also
log ignored drags.

## Development

```sh
python3 -m unittest discover -s tests
swiftc -O helper/DropLogic.swift helper/main.swift -o herdr-spring-load
./herdr-spring-load --dry-run   # logs drops, opens nothing
```

`viewer.py` can be tried outside herdr in a Ghostty tab:
`IMAGE=/path/to/shot.png python3 viewer.py`.

`python3 tools/make_test_images.py DIR` writes neutral test images (PNG, JPEG,
HEIC). `tools/annotate.py` crops a screenshot and adds numbered callouts and a
caption; see `docs/screenshots/SCREENSHOTS.md`.

## License

MIT, see [LICENSE](LICENSE).
