"""Draw a PNG with the Kitty graphics protocol (herdr forwards it to Ghostty)."""

from __future__ import annotations

import base64
import math
import struct
from typing import List, Optional, Tuple

CHUNK = 4096
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_size(data: bytes) -> Optional[Tuple[int, int]]:
    """Width and height from a PNG IHDR chunk, or None if not a PNG."""
    if len(data) < 24 or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def fit(
    img_w: int,
    img_h: int,
    cols: int,
    rows: int,
    cell_w: float,
    cell_h: float,
) -> Tuple[int, int]:
    """Cell box (cols, rows) that shows the image at its aspect ratio.

    The image is shrunk to fit the area but never enlarged past its own size.
    """
    if img_w <= 0 or img_h <= 0 or cols <= 0 or rows <= 0:
        return 0, 0
    scale = min(cols * cell_w / img_w, rows * cell_h / img_h, 1.0)
    c = max(1, min(cols, math.ceil(img_w * scale / cell_w)))
    r = max(1, min(rows, math.ceil(img_h * scale / cell_h)))
    return c, r


def place(png: bytes, cols: int, rows: int, image_id: int = 1) -> List[str]:
    """Escape sequences that transmit and display a PNG in a cols x rows box.

    The cursor is left where it was (C=1), and replies are suppressed (q=2)
    so nothing leaks into the terminal input.
    """
    payload = base64.standard_b64encode(png).decode("ascii")
    chunks = [payload[i : i + CHUNK] for i in range(0, len(payload), CHUNK)] or [""]
    out = []
    for n, chunk in enumerate(chunks):
        more = 1 if n < len(chunks) - 1 else 0
        if n == 0:
            head = "a=T,f=100,q=2,C=1,i=%d,c=%d,r=%d,m=%d" % (image_id, cols, rows, more)
        else:
            head = "m=%d" % more
        out.append("\x1b_G%s;%s\x1b\\" % (head, chunk))
    return out


def delete_all() -> str:
    return "\x1b_Ga=d,d=A,q=2\x1b\\"
