"""Start the core from the support folder's lib copy without PYTHONPATH.

The Windows Run key (experimental) holds a plain command line, so it cannot
set environment variables the way a LaunchAgent or a systemd unit does. This
puts lib on sys.path, applies the variables setup recorded in service.json,
and runs `python -m spring_load` with the given arguments."""
import json
import os
from pathlib import Path
import sys

LIB = Path(__file__).resolve().parent

def main():
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(LIB))
    try:
        recorded = json.loads((LIB.parent/'service.json').read_text()).get('env') or {}
    except (OSError, ValueError):
        recorded = {}
    os.environ.update({k: v for k, v in recorded.items() if isinstance(k, str) and isinstance(v, str)})
    from spring_load.__main__ import main as core
    core(sys.argv[1:])

if __name__ == '__main__':
    main()
