# Screenshots

The README screenshots are taken from a real run of the plugin. They have not been taken yet,
because the plugin has to be linked into herdr and the helper installed first. This file lists
the shots and how to make them.

## Rules

- Use a clean herdr session: only the `herdr-spring-load` workspace, or one throwaway pane with a
  plain shell. No agent conversations, notifications or other workspaces in view.
- Drag only the generated test images, never personal photos or documents:

  ```sh
  python3 tools/make_test_images.py ~/Desktop/hid-test
  ```

  This writes `test-1.png`, `test-2.png`, `test-3.png`, `test-2.jpg` and `test-photo.heic`
  (gradients and simple shapes).
- Keep raw captures outside the repo, and delete them after annotating. Only the annotated PNGs
  in this folder are committed.
- Before committing, open each annotated PNG and check that nothing else shows in the crop
  (Dock, menu bar, notifications, other windows).

## Shots

| File | What it shows | Callouts | Caption |
|------|---------------|----------|---------|
| `drag-in.png` | Mid-drag: `test-1.png` held over a herdr pane, Finder window beside it | 1 the file in Finder, 2 the pointer over the pane | 1 Drag an image from Finder  2 Release it over any herdr pane |
| `popup.png` | The popup viewer showing `test-1.png` right after the drop | 1 the image, 2 the footer line (`name  size  any key closes`) | 1 The preview opens on its own  2 Any key closes it |
| `heic.png` | The popup showing `test-photo.heic` | 1 the image, 2 the `.heic` file name in the footer | 1 HEIC files are converted with sips  2 and shown the same way |
| `multi-file.png` | Three files dropped at once, popup on the second | 1 the image, 2 `2/3`, 3 `j/k next/prev` | 1 Several files open together  2 position  3 step with j and k |

## Capture

`screencapture` works from a shell. For the popups, the popup stays open until a key is pressed,
so a window capture is easiest (click the terminal window when the camera pointer appears; the
click does not reach herdr):

```sh
screencapture -x -o -w ~/Desktop/hid-raw/popup.png
```

For the mid-drag shot, use a timed full-screen capture, start the drag, and hold it until the
shutter sound:

```sh
screencapture -x -T 6 ~/Desktop/hid-raw/drag-in.png
```

## Annotate

`tools/annotate.py` crops, adds numbered callouts and a caption bar, and writes
`docs/screenshots/NAME.png`. Coordinates are pixels in the raw capture (open it in Preview and
use Tools > Show Inspector, or the selection size readout, to read them). On a Retina display
the raw capture is at 2x, so the coordinates are 2x the points you see.

```sh
python3 tools/annotate.py ~/Desktop/hid-raw/popup.png --name popup \
    --crop 0,0,2400,1500 --mark 1200,700 --mark 300,1440 \
    --caption "1 The preview opens on its own  2 Any key closes it"
```

It uses Pillow if installed, otherwise ImageMagick (with sips for the crop), otherwise plain
python3 (8-bit PNG only, upper-case built-in font). Pass `--backend` to force one.

Then reference the files from the README Screenshots section and delete `~/Desktop/hid-raw`.
