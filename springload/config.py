"""Plugin config: $(herdr plugin config-dir herdr-spring-load)/config.toml.

Only flat `key = value` lines are used, so this reads them directly instead of
requiring tomllib (missing from the Python 3.9 that ships with macOS).
"""

from __future__ import annotations

import os
from typing import Dict

VIEWERS = ("popup", "quicklook", "preview")

DEFAULTS: Dict[str, str] = {
    "viewer": "popup",
    "max_files": "4",
}


def parse(text: str) -> Dict[str, str]:
    values = dict(DEFAULTS)
    for raw in text.splitlines():
        line = _strip_comment(raw).strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    if values.get("viewer") not in VIEWERS:
        values["viewer"] = DEFAULTS["viewer"]
    try:
        if int(values.get("max_files", "")) < 1:
            raise ValueError
    except ValueError:
        values["max_files"] = DEFAULTS["max_files"]
    return values


def _strip_comment(line: str) -> str:
    """Drop a trailing '# comment' that is not inside quotes."""
    quote = ""
    for i, c in enumerate(line):
        if quote:
            if c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c == "#":
            return line[:i]
    return line


def load(config_dir: str = "") -> Dict[str, str]:
    config_dir = config_dir or os.environ.get("HERDR_PLUGIN_CONFIG_DIR", "")
    if not config_dir:
        return dict(DEFAULTS)
    try:
        with open(os.path.join(config_dir, "config.toml"), encoding="utf-8") as f:
            return parse(f.read())
    except OSError:
        return dict(DEFAULTS)
