import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imagedrop import paths  # noqa: E402


class TokenizeTest(unittest.TestCase):
    def test_escaped_spaces(self):
        self.assertEqual(paths.tokenize(r"/a/My\ Shot\ 1.png"), ["/a/My Shot 1.png"])

    def test_single_and_double_quotes(self):
        self.assertEqual(
            paths.tokenize("'/a/b c.png' \"/d/e f.jpg\""),
            ["/a/b c.png", "/d/e f.jpg"],
        )

    def test_escaped_quote_inside_double_quotes(self):
        self.assertEqual(paths.tokenize(r'"/a/say \"hi\".png"'), ['/a/say "hi".png'])

    def test_unmatched_quote_is_literal(self):
        self.assertEqual(paths.tokenize("don't /a/b.png"), ["don't", "/a/b.png"])

    def test_adjacent_quoted_parts_join(self):
        self.assertEqual(paths.tokenize("/a/'b c'.png"), ["/a/b c.png"])


class ParseTest(unittest.TestCase):
    def find(self, text):
        return paths.image_paths(text, must_exist=False)

    def test_plain_path(self):
        self.assertEqual(self.find("/Users/me/shot.png"), ["/Users/me/shot.png"])

    def test_escaped_spaces(self):
        self.assertEqual(
            self.find(r"/Users/me/Screenshot\ 2026-10-28\ at\ 13.45.28.png"),
            ["/Users/me/Screenshot 2026-10-28 at 13.45.28.png"],
        )

    def test_quoted_paths(self):
        self.assertEqual(
            self.find("'/Users/me/a b.jpg' \"/Users/me/c d.gif\""),
            ["/Users/me/a b.jpg", "/Users/me/c d.gif"],
        )

    def test_file_url(self):
        self.assertEqual(
            self.find("file:///Users/me/My%20Shot.png"),
            ["/Users/me/My Shot.png"],
        )

    def test_file_url_localhost(self):
        self.assertEqual(self.find("file://localhost/tmp/x.webp"), ["/tmp/x.webp"])

    def test_file_url_remote_host_ignored(self):
        self.assertEqual(self.find("file://otherhost/tmp/x.png"), [])

    def test_multiple_files(self):
        self.assertEqual(
            self.find(r"/a/one.png /b/two\ 2.jpeg 'c/rel.png' /d/three.tiff"),
            ["/a/one.png", "/b/two 2.jpeg", "/d/three.tiff"],
        )

    def test_non_image_extensions_ignored(self):
        self.assertEqual(
            self.find("/a/notes.txt /a/doc.pdf /a/movie.mov /a/archive.png.zip /a/pic.PNG"),
            ["/a/pic.PNG"],
        )

    def test_relative_and_url_ignored(self):
        self.assertEqual(self.find("shot.png https://x.test/a.png"), [])

    def test_bracketed_paste_and_ansi(self):
        text = "\x1b[200~/a/b\\ c.png\x1b[201~ \x1b[32m/a/d.jpg\x1b[0m"
        self.assertEqual(self.find(text), ["/a/b c.png", "/a/d.jpg"])

    def test_duplicates_removed(self):
        self.assertEqual(self.find("/a/x.png /a/x.png"), ["/a/x.png"])

    def test_tilde(self):
        self.assertEqual(
            self.find("~/Desktop/x.png"),
            [os.path.expanduser("~/Desktop/x.png")],
        )

    def test_prompt_noise(self):
        self.assertEqual(self.find("> /Users/me/x.png"), ["/Users/me/x.png"])


class HeicTest(unittest.TestCase):
    def find(self, text):
        return paths.image_paths(text, must_exist=False)

    def test_heic_plain(self):
        self.assertEqual(self.find("/Users/me/IMG_0001.HEIC"), ["/Users/me/IMG_0001.HEIC"])

    def test_heic_escaped_spaces(self):
        self.assertEqual(
            self.find(r"/Users/me/Photo\ from\ phone.heic"),
            ["/Users/me/Photo from phone.heic"],
        )

    def test_heif_quoted(self):
        self.assertEqual(self.find("'/Users/me/a b.heif'"), ["/Users/me/a b.heif"])

    def test_heic_file_url(self):
        self.assertEqual(
            self.find("file:///Users/me/IMG%200002.heic"),
            ["/Users/me/IMG 0002.heic"],
        )

    def test_heic_mixed_with_png_and_text(self):
        self.assertEqual(
            self.find("/a/x.heic /a/readme.md /a/y.png"),
            ["/a/x.heic", "/a/y.png"],
        )

    def test_heic_left_on_prompt_is_last_drop(self):
        screen = "earlier output /old/one.png\n> /Users/me/IMG_0003.heic\n"
        self.assertEqual(
            paths.last_dropped(screen, must_exist=False),
            ["/Users/me/IMG_0003.heic"],
        )


class ExistenceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.real = os.path.join(self.dir, "real shot.png")
        open(self.real, "wb").close()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_missing_files_dropped(self):
        text = "%s /nope/missing.png" % self.real.replace(" ", "\\ ")
        self.assertEqual(paths.image_paths(text), [self.real])

    def test_directory_named_like_image_ignored(self):
        folder = os.path.join(self.dir, "folder.png")
        os.mkdir(folder)
        self.assertEqual(paths.image_paths(folder), [])


class LastDroppedTest(unittest.TestCase):
    def test_lowest_line_wins(self):
        screen = "$ ls\n/a/old.png\n$ /b/new\\ 1.jpg /b/new2.gif\n\n"
        self.assertEqual(
            paths.last_dropped(screen, must_exist=False),
            ["/b/new 1.jpg", "/b/new2.gif"],
        )

    def test_nothing_found(self):
        self.assertEqual(paths.last_dropped("$ echo hi\nhi\n", must_exist=False), [])


if __name__ == "__main__":
    unittest.main()
