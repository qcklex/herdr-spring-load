#!/usr/bin/env python3
"""Popup pane: show the images named in IMAGES (JSON list) or IMAGE.

j / l / space / right arrow: next image. k / h / left arrow: previous.
Any other key closes the popup.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import tty
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from springload import kitty  # noqa: E402

MAX_PIXELS = 2048
NEXT_KEYS = (b"j", b"l", b" ", b"\x1b[C", b"\x1bOC")
PREV_KEYS = (b"k", b"h", b"\x1b[D", b"\x1bOD")


def requested_paths(environ: Dict[str, str]) -> List[str]:
    raw = environ.get("IMAGES", "")
    if raw:
        try:
            value = json.loads(raw)
            if isinstance(value, list):
                return [str(p) for p in value if p]
        except ValueError:
            pass
    single = environ.get("IMAGE", "")
    return [single] if single else []


def display_safe(text: str) -> str:
    """Make untrusted text (file names, error messages) safe to print.

    macOS file names may contain ESC, BEL and other control characters; a
    name with an OSC sequence would otherwise reach the terminal. C0, DEL and
    C1 controls are shown as visible \\xNN escapes instead.
    """
    return "".join(
        "\\x%02x" % ord(c) if ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F else c for c in text
    )


def record_last(paths: List[str]) -> None:
    state_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if not state_dir or not paths:
        return
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "last.json"), "w", encoding="utf-8") as f:
            json.dump({"paths": paths}, f)
    except OSError:
        pass


def parse_sips_size(output: str) -> Optional[Tuple[int, int]]:
    width = re.search(r"pixelWidth:\s*(\d+)", output)
    height = re.search(r"pixelHeight:\s*(\d+)", output)
    if not width or not height:
        return None
    return int(width.group(1)), int(height.group(1))


def heic_to_jpeg(path: str, tmpdir: str) -> str:
    """Kitty graphics cannot draw HEIC, so convert it to a temp JPEG first."""
    out = os.path.join(tmpdir, "heic.jpg")
    result = subprocess.run(
        ["sips", "-s", "format", "jpeg", path, "--out", out], capture_output=True
    )
    if result.returncode != 0 or not os.path.isfile(out):
        raise ValueError("sips could not convert this HEIC file")
    return out


def to_png(path: str, tmpdir: str) -> bytes:
    """PNG bytes for any image macOS can read, capped at MAX_PIXELS a side.

    Kitty graphics (f=100) takes PNG only, so everything else, including the
    JPEG made from a HEIC file, goes through sips.
    """
    if os.path.splitext(path)[1].lower() in (".heic", ".heif"):
        path = heic_to_jpeg(path, tmpdir)
    probe = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", path],
        capture_output=True,
        text=True,
    )
    size = parse_sips_size(probe.stdout)
    with open(path, "rb") as f:
        head = f.read(8)
    if head == kitty.PNG_SIGNATURE and size and max(size) <= MAX_PIXELS:
        with open(path, "rb") as f:
            return f.read()
    out = os.path.join(tmpdir, "image.png")
    cmd = ["sips", "-s", "format", "png"]
    if size is None or max(size) > MAX_PIXELS:
        cmd += ["-Z", str(MAX_PIXELS)]
    result = subprocess.run(cmd + [path, "--out", out], capture_output=True)
    if result.returncode != 0 or not os.path.isfile(out):
        raise ValueError("sips could not convert this file")
    with open(out, "rb") as f:
        return f.read()


def window(fd: int) -> Tuple[int, int, float, float]:
    """cols, rows, cell width and cell height in pixels."""
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
        rows, cols, xpix, ypix = struct.unpack("HHHH", packed)
    except OSError:
        size = shutil.get_terminal_size()
        rows, cols, xpix, ypix = size.lines, size.columns, 0, 0
    cell_w = xpix / cols if xpix and cols else 8.0
    cell_h = ypix / rows if ypix and rows else 16.0
    return cols, rows, cell_w, cell_h


class Viewer:
    def __init__(self, paths: List[str], fd: int) -> None:
        self.paths = paths
        self.fd = fd
        self.index = 0
        self.cache: Dict[int, object] = {}
        self.tmpdir = tempfile.mkdtemp(prefix="herdr-spring-load.")
        self.resized = False

    def load(self, i: int) -> object:
        if i not in self.cache:
            try:
                png = to_png(self.paths[i], self.tmpdir)
                size = kitty.png_size(png)
                if size is None:
                    raise ValueError("not a readable image")
                self.cache[i] = (png, size)
            except (OSError, ValueError) as err:
                self.cache[i] = str(err)
        return self.cache[i]

    def draw(self) -> None:
        cols, rows, cell_w, cell_h = window(self.fd)
        out = [kitty.delete_all(), "\x1b[2J\x1b[H"]
        area_rows = max(1, rows - 1)
        item = self.load(self.index)
        if isinstance(item, tuple):
            png, (w, h) = item
            c, r = kitty.fit(w, h, cols, area_rows, cell_w, cell_h)
            top = (area_rows - r) // 2
            left = (cols - c) // 2
            out.append("\x1b[%d;%dH" % (top + 1, left + 1))
            out.extend(kitty.place(png, c, r))
            detail = "%dx%d" % (w, h)
        else:
            out.append("\x1b[%d;1H" % max(1, area_rows // 2))
            out.append(("cannot show image: %s" % display_safe(str(item)))[:cols])
            detail = "error"
        out.append("\x1b[%d;1H\x1b[2K" % rows)
        out.append(self.footer(detail, cols))
        os.write(sys.stdout.fileno(), "".join(out).encode("utf-8", "replace"))

    def footer(self, detail: str, cols: int) -> str:
        name = display_safe(os.path.basename(self.paths[self.index]))
        parts = [name, detail]
        if len(self.paths) > 1:
            parts.append("%d/%d" % (self.index + 1, len(self.paths)))
            parts.append("j/k next/prev")
        parts.append("any key closes")
        return "  ".join(parts)[:cols]

    def run(self) -> None:
        self.draw()
        while True:
            if self.resized:
                self.resized = False
                self.draw()
            try:
                ready, _, _ = select.select([self.fd], [], [], 0.2)
            except InterruptedError:
                continue
            if not ready:
                continue
            key = os.read(self.fd, 32)
            if not key:
                return
            if len(self.paths) > 1 and key in NEXT_KEYS:
                self.index = (self.index + 1) % len(self.paths)
            elif len(self.paths) > 1 and key in PREV_KEYS:
                self.index = (self.index - 1) % len(self.paths)
            else:
                return
            self.draw()

    def close(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)


def main() -> int:
    paths = [p for p in requested_paths(dict(os.environ)) if os.path.isfile(p)]
    if not paths:
        sys.stderr.write("herdr-spring-load: no image files given\n")
        return 1
    record_last(paths)
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    viewer = Viewer(paths, fd)

    def on_resize(_signum: int, _frame: object) -> None:
        viewer.resized = True

    signal.signal(signal.SIGWINCH, on_resize)
    try:
        tty.setraw(fd)
        os.write(sys.stdout.fileno(), b"\x1b[?25l")
        viewer.run()
    finally:
        os.write(sys.stdout.fileno(), (kitty.delete_all() + "\x1b[2J\x1b[H\x1b[?25h").encode())
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        viewer.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
