"""Find image file paths in text pasted into a terminal.

macOS terminals paste a dropped file as a shell-escaped path. Depending on the
terminal and the app this can look like any of:

    /Users/me/My\\ Shot.png
    '/Users/me/My Shot.png'
    "/Users/me/My Shot.png"
    file:///Users/me/My%20Shot.png
    /a.png /b.jpg            (several files, space separated)
    ESC[200~/a.png ESC[201~  (bracketed paste markers)

The helper does not need this (it reads file URLs off the drag pasteboard).
It is used by the preview-last action to recover paths from a pane's screen.
"""

from __future__ import annotations

import os
import re
from typing import Iterable, List, Optional
from urllib.parse import unquote, urlparse

IMAGE_EXTENSIONS = frozenset(
    {"png", "jpg", "jpeg", "gif", "heic", "heif", "webp", "tif", "tiff", "bmp"}
)

_BRACKETED_PASTE = re.compile(r"\x1b\[20[01]~")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def is_image_path(path: str) -> bool:
    _, ext = os.path.splitext(path)
    return ext[1:].lower() in IMAGE_EXTENSIONS


def strip_control(text: str) -> str:
    """Remove bracketed paste markers and other ANSI escape sequences."""
    return _ANSI.sub("", _BRACKETED_PASTE.sub(" ", text))


def tokenize(line: str) -> List[str]:
    """Split a line the way a POSIX shell splits words, but never fail.

    Handles backslash escapes, single quotes and double quotes. An unmatched
    quote is kept as a literal character instead of raising, because screen
    text often contains stray apostrophes.
    """
    tokens: List[str] = []
    buf: List[str] = []
    in_token = False
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c in " \t\r\n":
            if in_token:
                tokens.append("".join(buf))
                buf = []
                in_token = False
            i += 1
            continue
        in_token = True
        if c == "\\":
            if i + 1 < n:
                buf.append(line[i + 1])
                i += 2
            else:
                i += 1
            continue
        if c == "'":
            end = line.find("'", i + 1)
            if end == -1:
                buf.append(c)
                i += 1
            else:
                buf.append(line[i + 1 : end])
                i = end + 1
            continue
        if c == '"':
            j = i + 1
            quoted: List[str] = []
            closed = False
            while j < n:
                d = line[j]
                if d == "\\" and j + 1 < n and line[j + 1] in '"\\$`':
                    quoted.append(line[j + 1])
                    j += 2
                    continue
                if d == '"':
                    closed = True
                    break
                quoted.append(d)
                j += 1
            if closed:
                buf.extend(quoted)
                i = j + 1
            else:
                buf.append(c)
                i += 1
            continue
        buf.append(c)
        i += 1
    if in_token:
        tokens.append("".join(buf))
    return tokens


def token_to_path(token: str) -> Optional[str]:
    """Turn one token into an absolute filesystem path, or None."""
    if token.lower().startswith("file://"):
        parsed = urlparse(token)
        if parsed.netloc not in ("", "localhost"):
            return None
        path = unquote(parsed.path)
    else:
        path = token
    if path.startswith("~"):
        path = os.path.expanduser(path)
    if not path.startswith("/"):
        return None
    return path


def image_paths_in_line(line: str, must_exist: bool = True) -> List[str]:
    """All image paths in one line, in order, without duplicates."""
    found: List[str] = []
    for token in tokenize(strip_control(line)):
        path = token_to_path(token)
        if path is None or not is_image_path(path):
            continue
        if must_exist and not os.path.isfile(path):
            continue
        if path not in found:
            found.append(path)
    return found


def image_paths(text: str, must_exist: bool = True) -> List[str]:
    """All image paths in a block of text, in order, without duplicates."""
    found: List[str] = []
    for line in strip_control(text).splitlines():
        for path in image_paths_in_line(line, must_exist):
            if path not in found:
                found.append(path)
    return found


def last_dropped(text: str, must_exist: bool = True) -> List[str]:
    """Image paths on the lowest line of the screen that has any.

    A drop lands on the prompt line, which is normally the last non-empty
    line, so this picks the most recent drop rather than older output.
    """
    lines: Iterable[str] = reversed(strip_control(text).splitlines())
    for line in lines:
        paths = image_paths_in_line(line, must_exist)
        if paths:
            return paths
    return []
