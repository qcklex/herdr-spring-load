"""Open images in the herdr popup, with Quick Look and Preview as fallbacks."""

from __future__ import annotations

import json
import os
import subprocess
from typing import List, Sequence

PLUGIN_ID = "herdr-image-drop"


def herdr_bin() -> str:
    env = os.environ.get("HERDR_BIN_PATH")
    if env:
        return env
    for candidate in ("/opt/homebrew/bin/herdr", "/usr/local/bin/herdr"):
        if os.access(candidate, os.X_OK):
            return candidate
    return "herdr"


def popup_command(paths: Sequence[str]) -> List[str]:
    return [
        herdr_bin(),
        "plugin",
        "pane",
        "open",
        "--plugin",
        os.environ.get("HERDR_PLUGIN_ID", PLUGIN_ID),
        "--entrypoint",
        "viewer",
        "--env",
        "IMAGE=%s" % paths[0],
        "--env",
        "IMAGES=%s" % json.dumps(list(paths)),
    ]


def open_popup(paths: Sequence[str]) -> bool:
    try:
        result = subprocess.run(
            popup_command(paths),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def open_quicklook(paths: Sequence[str]) -> None:
    # qlmanage blocks until the panel closes, so do not wait for it.
    subprocess.Popen(
        ["qlmanage", "-p", *paths],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def open_preview(paths: Sequence[str]) -> None:
    subprocess.run(["open", "-a", "Preview", *paths], check=False)


def open_images(paths: Sequence[str], viewer: str = "popup") -> str:
    """Open paths with the configured viewer; return the viewer actually used."""
    if not paths:
        return "none"
    if viewer == "preview":
        open_preview(paths)
        return "preview"
    if viewer == "popup" and open_popup(paths):
        return "popup"
    open_quicklook(paths)
    return "quicklook"
