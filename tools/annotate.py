#!/usr/bin/env python3
"""Crop a raw screenshot, add numbered callouts and a caption bar, save it for the README.

    python3 tools/annotate.py RAW.png --name drag-in \
        --crop 120,80,1400,900 \
        --mark 300,210 --mark 820,640 \
        --caption "1 Drag the image onto a pane  2 The preview opens"

Coordinates are pixels in RAW (before cropping); marks are numbered 1, 2, ... in order.
The result goes to docs/screenshots/NAME.png.

Backends, picked in this order unless --backend is given:
  pillow  Pillow, if importable
  magick  sips to crop, ImageMagick (`magick`) to draw
  pure    python3 standard library only (8-bit PNG in, RGB PNG out, built-in 5x7 font)
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from typing import List, Optional, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO, "docs", "screenshots")

ACCENT = (247, 107, 21)
WHITE = (255, 255, 255)
BAR = (31, 35, 40)

Box = Tuple[int, int, int, int]
Point = Tuple[int, int]


def parse_ints(text: str, count: int) -> Tuple[int, ...]:
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != count:
        raise argparse.ArgumentTypeError(f"expected {count} comma-separated integers, got {text!r}")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer list: {text!r}")


def sizes(width: int) -> Tuple[int, int, int]:
    """Callout radius, caption bar height and padding for an output this wide."""
    radius = max(14, width // 55)
    bar = max(36, width // 22)
    return radius, bar, bar // 4


def relative_marks(marks: List[Point], crop: Box) -> List[Point]:
    x0, y0, _, _ = crop
    return [(x - x0, y - y0) for x, y in marks]


def clamp_crop(crop: Optional[Box], width: int, height: int) -> Box:
    if crop is None:
        return 0, 0, width, height
    x, y, w, h = crop
    x, y = max(0, x), max(0, y)
    w, h = min(w, width - x), min(h, height - y)
    if w <= 0 or h <= 0:
        raise SystemExit(f"crop box {crop} lies outside the {width}x{height} image")
    return x, y, w, h


# ---------------------------------------------------------------- pillow


def render_pillow(raw: str, out: str, crop: Optional[Box], marks: List[Point], caption: str) -> None:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.open(raw).convert("RGB")
    box = clamp_crop(crop, *img.size)
    x, y, w, h = box
    img = img.crop((x, y, x + w, y + h))
    radius, bar, pad = sizes(w)

    canvas = Image.new("RGB", (w, h + (bar if caption else 0)), BAR)
    canvas.paste(img, (0, 0))
    draw = ImageDraw.Draw(canvas)

    def font(size: int):
        for path in ("/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc",
                     "/Library/Fonts/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size)
                except OSError:
                    continue
        return ImageFont.load_default(size=size)

    num_font = font(int(radius * 1.2))
    for i, (cx, cy) in enumerate(relative_marks(marks, box), 1):
        outline = max(2, radius // 7)
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                     fill=ACCENT, outline=WHITE, width=outline)
        draw.text((cx, cy), str(i), fill=WHITE, font=num_font, anchor="mm")

    if caption:
        draw.text((pad, h + bar // 2), caption, fill=WHITE, font=font(int(bar * 0.45)), anchor="lm")
    canvas.save(out, "PNG", optimize=True)


# ---------------------------------------------------------------- sips + ImageMagick


def png_size(path: str) -> Tuple[int, int]:
    with open(path, "rb") as fh:
        head = fh.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"{path} is not a PNG")
    return struct.unpack(">II", head[16:24])


def render_magick(raw: str, out: str, crop: Optional[Box], marks: List[Point], caption: str) -> None:
    magick = shutil.which("magick") or shutil.which("convert")
    if not magick:
        raise SystemExit("ImageMagick not found")
    box = clamp_crop(crop, *png_size(raw))
    x, y, w, h = box
    with tempfile.TemporaryDirectory() as tmp:
        cropped = os.path.join(tmp, "crop.png")
        if shutil.which("sips"):
            # sips crops around the centre unless given an offset (y then x).
            subprocess.run(["sips", "-c", str(h), str(w), "--cropOffset", str(y), str(x),
                            raw, "--out", cropped], check=True, stdout=subprocess.DEVNULL)
        else:
            subprocess.run([magick, raw, "-crop", f"{w}x{h}+{x}+{y}", "+repage", cropped], check=True)

        radius, bar, pad = sizes(w)
        hexc = "#%02x%02x%02x"
        cmd = [magick, cropped, "-stroke", hexc % WHITE, "-strokewidth", str(max(2, radius // 7))]
        for i, (cx, cy) in enumerate(relative_marks(marks, box), 1):
            cmd += ["-fill", hexc % ACCENT, "-draw", f"circle {cx},{cy} {cx + radius},{cy}"]
        cmd += ["-stroke", "none", "-fill", hexc % WHITE, "-pointsize", str(int(radius * 1.2))]
        for i, (cx, cy) in enumerate(relative_marks(marks, box), 1):
            cmd += ["-gravity", "NorthWest", "-annotate",
                    f"+{cx - int(radius * 0.35 * len(str(i)))}+{cy - int(radius * 0.6)}", str(i)]
        if caption:
            cmd += ["-background", hexc % BAR, "-gravity", "South", "-splice", f"0x{bar}",
                    "-gravity", "SouthWest", "-pointsize", str(int(bar * 0.45)),
                    "-annotate", f"+{pad}+{bar // 4}", caption]
        cmd.append(out)
        subprocess.run(cmd, check=True)


# ---------------------------------------------------------------- pure python

FONT = {
    "0": "01110 10001 10011 10101 11001 10001 01110", "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00010 00100 01000 11111", "3": "11111 00010 00100 00010 00001 10001 01110",
    "4": "00010 00110 01010 10010 11111 00010 00010", "5": "11111 10000 11110 00001 00001 10001 01110",
    "6": "00110 01000 10000 11110 10001 10001 01110", "7": "11111 00001 00010 00100 01000 01000 01000",
    "8": "01110 10001 10001 01110 10001 10001 01110", "9": "01110 10001 10001 01111 00001 00010 01100",
    "A": "01110 10001 10001 11111 10001 10001 10001", "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01110 10001 10000 10000 10000 10001 01110", "D": "11100 10010 10001 10001 10001 10010 11100",
    "E": "11111 10000 10000 11110 10000 10000 11111", "F": "11111 10000 10000 11110 10000 10000 10000",
    "G": "01110 10001 10000 10111 10001 10001 01111", "H": "10001 10001 10001 11111 10001 10001 10001",
    "I": "01110 00100 00100 00100 00100 00100 01110", "J": "00111 00010 00010 00010 00010 10010 01100",
    "K": "10001 10010 10100 11000 10100 10010 10001", "L": "10000 10000 10000 10000 10000 10000 11111",
    "M": "10001 11011 10101 10101 10001 10001 10001", "N": "10001 10001 11001 10101 10011 10001 10001",
    "O": "01110 10001 10001 10001 10001 10001 01110", "P": "11110 10001 10001 11110 10000 10000 10000",
    "Q": "01110 10001 10001 10001 10101 10010 01101", "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110", "T": "11111 00100 00100 00100 00100 00100 00100",
    "U": "10001 10001 10001 10001 10001 10001 01110", "V": "10001 10001 10001 10001 10001 01010 00100",
    "W": "10001 10001 10001 10101 10101 10101 01010", "X": "10001 10001 01010 00100 01010 10001 10001",
    "Y": "10001 10001 10001 01010 00100 00100 00100", "Z": "11111 00001 00010 00100 01000 10000 11111",
    " ": "00000 00000 00000 00000 00000 00000 00000", ".": "00000 00000 00000 00000 00000 01100 01100",
    ",": "00000 00000 00000 00000 01100 00100 01000", ":": "00000 01100 01100 00000 01100 01100 00000",
    "-": "00000 00000 00000 11111 00000 00000 00000", "/": "00001 00010 00010 00100 01000 01000 10000",
    "(": "00010 00100 01000 01000 01000 00100 00010", ")": "01000 00100 00010 00010 00010 00100 01000",
    "'": "00100 00100 01000 00000 00000 00000 00000", "!": "00100 00100 00100 00100 00100 00000 00100",
    "?": "01110 10001 00001 00010 00100 00000 00100", "_": "00000 00000 00000 00000 00000 00000 11111",
    "+": "00000 00100 00100 11111 00100 00100 00000", "=": "00000 00000 11111 00000 11111 00000 00000",
    ">": "01000 00100 00010 00001 00010 00100 01000", "<": "00010 00100 01000 10000 01000 00100 00010",
}


def read_png(path: str) -> Tuple[int, int, List[bytearray]]:
    """Decode a non-interlaced 8-bit PNG into RGB rows."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"{path} is not a PNG")
    pos, idat = 8, []
    width = height = depth = ctype = interlace = 0
    palette = b""
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if depth != 8 or interlace or channels is None:
        raise SystemExit("pure backend reads 8-bit non-interlaced PNG only; install Pillow for others")
    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    prev = bytearray(stride)
    rows = []
    for r in range(height):
        start = r * (stride + 1)
        ftype, line = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            if ftype == 1:
                line[i] = (line[i] + a) & 255
            elif ftype == 2:
                line[i] = (line[i] + b) & 255
            elif ftype == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 255
            elif ftype == 4:
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        rgb = bytearray(width * 3)
        for x in range(width):
            px = line[x * channels:(x + 1) * channels]
            if ctype == 3:
                px = palette[px[0] * 3:px[0] * 3 + 3]
            elif channels <= 2:
                px = bytes((px[0],)) * 3
            rgb[x * 3:x * 3 + 3] = px[:3]
        rows.append(rgb)
    return width, height, rows


def write_png(path: str, width: int, rows: List[bytearray]) -> None:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, len(rows), 8, 2, 0, 0, 0)))
        fh.write(chunk(b"IDAT", zlib.compress(raw, 9)))
        fh.write(chunk(b"IEND", b""))


def put(rows: List[bytearray], x: int, y: int, color: Tuple[int, int, int]) -> None:
    if 0 <= y < len(rows) and 0 <= x < len(rows[0]) // 3:
        rows[y][x * 3:x * 3 + 3] = bytes(color)


def text_width(text: str, scale: int) -> int:
    return len(text) * 6 * scale - scale


def draw_text(rows: List[bytearray], x: int, y: int, text: str, scale: int, color) -> None:
    for ch in text.upper():
        glyph = FONT.get(ch, FONT["?"]).split()
        for gy, line in enumerate(glyph):
            for gx, bit in enumerate(line):
                if bit == "1":
                    for dy in range(scale):
                        for dx in range(scale):
                            put(rows, x + gx * scale + dx, y + gy * scale + dy, color)
        x += 6 * scale


def render_pure(raw: str, out: str, crop: Optional[Box], marks: List[Point], caption: str) -> None:
    width, height, rows = read_png(raw)
    box = clamp_crop(crop, width, height)
    x0, y0, w, h = box
    rows = [bytearray(r[x0 * 3:(x0 + w) * 3]) for r in rows[y0:y0 + h]]
    radius, bar, pad = sizes(w)

    outline = max(2, radius // 7)
    for i, (cx, cy) in enumerate(relative_marks(marks, box), 1):
        for yy in range(cy - radius, cy + radius + 1):
            for xx in range(cx - radius, cx + radius + 1):
                d2 = (xx - cx) ** 2 + (yy - cy) ** 2
                if d2 <= radius * radius:
                    inner = (radius - outline) ** 2
                    put(rows, xx, yy, ACCENT if d2 < inner else WHITE)
        label = str(i)
        scale = max(1, radius // 6)
        draw_text(rows, cx - text_width(label, scale) // 2, cy - 7 * scale // 2, label, scale, WHITE)

    if caption:
        rows += [bytearray(bytes(BAR) * w) for _ in range(bar)]
        scale = max(1, (bar // 2) // 7)
        draw_text(rows, pad, h + (bar - 7 * scale) // 2, caption, scale, WHITE)
    write_png(out, w, rows)


# ---------------------------------------------------------------- main

BACKENDS = {"pillow": render_pillow, "magick": render_magick, "pure": render_pure}


def pick_backend(name: str) -> str:
    if name != "auto":
        return name
    try:
        import PIL  # noqa: F401
        return "pillow"
    except ImportError:
        pass
    if shutil.which("magick") or shutil.which("convert"):
        return "magick"
    return "pure"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("raw", help="raw PNG screenshot")
    ap.add_argument("--name", required=True, help="output name; saved as docs/screenshots/NAME.png")
    ap.add_argument("--crop", type=lambda s: parse_ints(s, 4), help="x,y,width,height in raw pixels")
    ap.add_argument("--mark", type=lambda s: parse_ints(s, 2), action="append", default=[],
                    help="x,y of a numbered callout in raw pixels; repeat, numbered in order")
    ap.add_argument("--caption", default="", help="text for the caption bar under the image")
    ap.add_argument("--backend", choices=["auto", *BACKENDS], default="auto")
    ap.add_argument("--out-dir", default=OUT_DIR, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    name = os.path.basename(args.name)
    if name.endswith(".png"):
        name = name[:-4]
    os.makedirs(args.out_dir, exist_ok=True)
    out = os.path.join(args.out_dir, name + ".png")
    backend = pick_backend(args.backend)
    BACKENDS[backend](args.raw, out, args.crop, args.mark, args.caption)
    print(f"{out} ({backend})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
