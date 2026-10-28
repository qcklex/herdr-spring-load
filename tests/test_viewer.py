import base64
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import viewer  # noqa: E402
from imagedrop import config, kitty  # noqa: E402


def tiny_png(width=3, height=2):
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        kitty.PNG_SIGNATURE
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class KittyTest(unittest.TestCase):
    def test_png_size(self):
        self.assertEqual(kitty.png_size(tiny_png(7, 5)), (7, 5))
        self.assertIsNone(kitty.png_size(b"GIF89a" + b"\0" * 30))

    def test_fit_shrinks_wide_image(self):
        # 1000x500 into 80x24 cells of 10x20 px: width limits, 800 px wide.
        self.assertEqual(kitty.fit(1000, 500, 80, 24, 10, 20), (80, 20))

    def test_fit_shrinks_tall_image(self):
        self.assertEqual(kitty.fit(500, 2000, 80, 24, 10, 20), (12, 24))

    def test_fit_does_not_enlarge(self):
        self.assertEqual(kitty.fit(40, 40, 80, 24, 10, 20), (4, 2))

    def test_fit_degenerate(self):
        self.assertEqual(kitty.fit(0, 10, 80, 24, 10, 20), (0, 0))

    def test_place_chunks(self):
        png = os.urandom(10000)
        seqs = kitty.place(png, 10, 5)
        self.assertGreater(len(seqs), 1)
        self.assertIn("a=T,f=100,q=2,C=1,i=1,c=10,r=5,m=1;", seqs[0])
        self.assertTrue(seqs[-1].startswith("\x1b_Gm=0;"))
        payload = "".join(s.split(";", 1)[1][:-2] for s in seqs)
        self.assertEqual(base64.b64decode(payload), png)


class ViewerInputTest(unittest.TestCase):
    def test_images_json(self):
        env = {"IMAGES": json.dumps(["/a b.png", "/c.heic"]), "IMAGE": "/a b.png"}
        self.assertEqual(viewer.requested_paths(env), ["/a b.png", "/c.heic"])

    def test_image_fallback(self):
        self.assertEqual(viewer.requested_paths({"IMAGE": "/x.png"}), ["/x.png"])
        self.assertEqual(viewer.requested_paths({"IMAGES": "nope", "IMAGE": "/x.png"}), ["/x.png"])

    def test_parse_sips_size(self):
        out = "/x.png\n  pixelWidth: 1200\n  pixelHeight: 800\n"
        self.assertEqual(viewer.parse_sips_size(out), (1200, 800))
        self.assertIsNone(viewer.parse_sips_size("Error"))


@unittest.skipUnless(shutil.which("sips"), "needs macOS sips")
class ConversionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.png = os.path.join(self.dir, "src.png")
        with open(self.png, "wb") as f:
            f.write(tiny_png(30, 20))

    def tearDown(self):
        shutil.rmtree(self.dir)

    def convert(self, fmt, ext):
        out = os.path.join(self.dir, "src." + ext)
        subprocess.run(["sips", "-s", "format", fmt, self.png, "--out", out], capture_output=True)
        if not os.path.isfile(out):
            self.skipTest("sips cannot write %s here" % fmt)
        return out

    def test_png_passes_through(self):
        self.assertEqual(kitty.png_size(viewer.to_png(self.png, self.dir)), (30, 20))

    def test_jpeg_converted(self):
        jpg = self.convert("jpeg", "jpg")
        self.assertEqual(kitty.png_size(viewer.to_png(jpg, self.dir)), (30, 20))

    def test_heic_goes_through_jpeg(self):
        heic = self.convert("heic", "heic")
        png = viewer.to_png(heic, self.dir)
        self.assertEqual(kitty.png_size(png), (30, 20))
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "heic.jpg")))

    def test_not_an_image(self):
        bogus = os.path.join(self.dir, "bogus.png")
        with open(bogus, "w") as f:
            f.write("not an image")
        with self.assertRaises(ValueError):
            viewer.to_png(bogus, self.dir)


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(config.parse(""), {"viewer": "popup", "max_files": "4"})

    def test_values_and_comments(self):
        text = '# settings\nviewer = "quicklook"  # fallback\nmax_files = 2\n'
        self.assertEqual(config.parse(text), {"viewer": "quicklook", "max_files": "2"})

    def test_invalid_values_reset(self):
        self.assertEqual(
            config.parse('viewer = "kitty"\nmax_files = 0'),
            {"viewer": "popup", "max_files": "4"},
        )


if __name__ == "__main__":
    unittest.main()
