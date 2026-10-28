# herdr-image-drop

Drag an image file onto a herdr pane and a preview opens on its own.

It works in every pane, including agent TUIs such as Claude Code that swap a
dropped image for an `[Image #1]` placeholder, because it never reads the path
off the screen. A small macOS helper watches the system drag pasteboard. When
a drag that carries image files is released over your terminal window, it asks
herdr to open the plugin's popup viewer, which draws the image with Kitty
graphics.

macOS only. Supported files: png, jpg, jpeg, gif, heic, heif, webp, tif, tiff,
bmp. HEIC/HEIF is converted with `sips` before it is drawn.

## Requirements

- herdr 0.9.0 or newer, with `kitty_graphics = true` in the herdr config
- A terminal with Kitty graphics support (Ghostty by default)
- `python3` (the one that ships with macOS is enough)
- Xcode command line tools, for `swiftc` to build the helper

## Install

1. Install the plugin (the popup viewer):

   ```sh
   herdr plugin install qcklex/herdr-image-drop
   ```

2. Install the drag helper as a LaunchAgent, from a checkout of this repo:

   ```sh
   git clone https://github.com/qcklex/herdr-image-drop
   sh herdr-image-drop/helper/install.sh
   ```

   It builds `image-drop-watch` into
   `~/Library/Application Support/herdr-image-drop/` and starts it at login.
   It needs no Accessibility or Input Monitoring permission.

   For a terminal other than Ghostty, pass its bundle id:
   `sh helper/install.sh --terminal com.googlecode.iterm2`.

Remove the helper with `sh helper/uninstall.sh` and the plugin with
`herdr plugin uninstall herdr-image-drop`.

## Use

- Drop one or more image files onto a herdr pane. A popup shows the first
  image; `j`/`k` (or the arrow keys) step through several. Any other key
  closes it, and that key does not reach the pane.
- Action `herdr-image-drop.preview-last` reopens the last images shown. If
  there are none, it takes the image paths from the lowest line of the
  focused pane that has any. Bind it like any plugin action:

  ```toml
  [[keys.command]]
  key = "prefix+i"
  type = "plugin_action"
  command = "herdr-image-drop.preview-last"
  description = "preview last image"
  ```

## Configure

Optional `config.toml` in `$(herdr plugin config-dir herdr-image-drop)`:

```toml
viewer = "popup"   # popup | quicklook | preview
max_files = 4      # images shown per drop
```

If the popup cannot open (another herdr modal is up, no client is attached, or
the server is down), the helper falls back to Quick Look.

## Known limits

- A drag released over the terminal but cancelled still opens a preview;
  the pasteboard cannot tell a cancel from a drop.
- A drop on a terminal window that is not running herdr opens Quick Look.
- Images dragged from a browser (image data, no file) are skipped.
- The preview opens in addition to whatever the pane does with the drop; an
  agent that attaches the image still attaches it.
- The popup opens over the whole herdr session, not inside the pane the file
  was dropped on.
- The helper only checks which app owns the window under the pointer. A drop
  on a window of another app that sits in front of the terminal (the Dock, a
  floating panel) can be mistaken for a drop on the terminal.
- Remote panes (`ssh`, `herdr --remote`) are not handled specially. The
  preview is of the file on your Mac.
- The helper is a separate install step that needs `swiftc`.
  `herdr plugin install` alone gives you only the viewer and the
  `preview-last` action.

Helper log: `~/Library/Logs/herdr-image-drop.log`.

## Development

```sh
python3 -m unittest discover -s tests
swiftc -O helper/ImageDropWatch.swift -o image-drop-watch
./image-drop-watch --dry-run   # logs drops, opens nothing
```

`viewer.py` can be tried outside herdr in a Ghostty tab:
`IMAGE=/path/to/shot.png python3 viewer.py`.

## License

MIT
