#!/usr/bin/env python3
"""Action preview-last: reopen the last dropped images.

Uses the paths the viewer saved in HERDR_PLUGIN_STATE_DIR/last.json. If there
are none (or they were deleted), it falls back to reading the focused pane and
taking the image paths on the lowest line that has any, which covers a path
that was dropped or pasted while the helper was not running.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from springload import config, launch, paths  # noqa: E402


def from_state() -> List[str]:
    state_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR", "")
    try:
        with open(os.path.join(state_dir, "last.json"), encoding="utf-8") as f:
            saved = json.load(f).get("paths", [])
    except (OSError, ValueError, AttributeError):
        return []
    return [p for p in saved if isinstance(p, str) and os.path.isfile(p)]


def from_pane() -> List[str]:
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        return []
    result = subprocess.run(
        [launch.herdr_bin(), "pane", "read", pane, "--source", "recent-unwrapped", "--lines", "200"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return paths.last_dropped(result.stdout)


def main() -> int:
    settings = config.load()
    found = from_state() or from_pane()
    found = found[: int(settings["max_files"])]
    if not found:
        sys.stderr.write("herdr-spring-load: no recent image to show\n")
        return 1
    used = launch.open_images(found, settings["viewer"])
    print("opened %d image(s) with %s" % (len(found), used))
    return 0


if __name__ == "__main__":
    sys.exit(main())
