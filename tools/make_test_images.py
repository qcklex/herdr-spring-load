#!/usr/bin/env python3
"""Write neutral test images (gradients and shapes) for screenshots and manual tests.

    python3 tools/make_test_images.py OUT_DIR

Creates test-1.png, test-2.png, test-3.png and, with sips (macOS), test-2.jpg and
test-photo.heic. Standard library only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from annotate import write_png  # noqa: E402

W, H = 1200, 800

# Background corner colours and one shape per image.
PALETTES = [
    ((52, 72, 140), (60, 150, 130), "circle", (240, 200, 90)),
    ((150, 70, 90), (230, 160, 80), "square", (245, 245, 240)),
    ((40, 110, 120), (120, 90, 170), "stripes", (230, 230, 230)),
]


def make(index: int):
    top, bottom, shape, fg = PALETTES[index % len(PALETTES)]
    rows = []
    for y in range(H):
        t = y / (H - 1)
        bg = bytes(int(a + (b - a) * t) for a, b in zip(top, bottom))
        row = bytearray(bg * W)
        for x in range(W):
            if shape == "circle":
                inside = (x - 600) ** 2 + (y - 400) ** 2 < 230 ** 2
            elif shape == "square":
                inside = 380 < x < 820 and 180 < y < 620
            else:
                inside = 200 < y < 600 and (x // 80) % 2 == 0
            if inside:
                row[x * 3:x * 3 + 3] = bytes(fg)
        rows.append(row)
    return rows


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__.strip().split("\n\n")[1].strip(), file=sys.stderr)
        return 2
    out = argv[1]
    os.makedirs(out, exist_ok=True)
    for i in range(3):
        path = os.path.join(out, "test-%d.png" % (i + 1))
        write_png(path, W, make(i))
        print(path)
    if shutil.which("sips"):
        for src, fmt, name in (("test-2.png", "jpeg", "test-2.jpg"), ("test-1.png", "heic", "test-photo.heic")):
            dst = os.path.join(out, name)
            subprocess.run(["sips", "-s", "format", fmt, os.path.join(out, src), "--out", dst],
                           check=True, stdout=subprocess.DEVNULL)
            print(dst)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
