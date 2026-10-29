"""Build the macOS sensor (docs/SENSOR.md section 5). Needs only swiftc from
the Xcode Command Line Tools. The binary is ad-hoc signed by the linker; it
needs no stable signature because it asks for no macOS permission."""
import os
from pathlib import Path
import shutil
import subprocess

SOURCE = Path(__file__).resolve().with_name('main.swift')

def build(out):
    out = Path(out)
    swiftc = os.environ.get('SPRING_LOAD_SWIFTC') or shutil.which('swiftc')
    if not swiftc:
        raise RuntimeError('swiftc was not found. Install the Xcode Command Line Tools (xcode-select --install), then run setup again.')
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run([swiftc, '-O', '-o', str(out), str(SOURCE)], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f'could not run swiftc: {error}') from None
    if result.returncode != 0 or not out.exists():
        detail = (result.stderr or result.stdout).strip().splitlines()[-5:]
        raise RuntimeError('building the macOS sensor failed:\n' + '\n'.join(detail))
