"""Build the macOS sensor (docs/SENSOR.md section 5). Needs only swiftc from
the Xcode Command Line Tools. The binary is ad-hoc signed by the linker; it
needs no stable signature because it asks for no macOS permission."""
import os
from pathlib import Path
import shutil
import subprocess

SOURCE = Path(__file__).resolve().with_name('main.swift')

MISSING = 'Xcode Command Line Tools are missing: run xcode-select --install, then run setup again.'

def find_swiftc():
    # /usr/bin/swiftc is a shim that opens an install dialog when the tools are
    # missing, so ask xcode-select first and never run the shim blind.
    select, xcrun = shutil.which('xcode-select'), shutil.which('xcrun')
    if not select or not xcrun:
        raise RuntimeError(MISSING)
    if subprocess.run([select, '-p'], stdin=subprocess.DEVNULL, capture_output=True).returncode != 0:
        raise RuntimeError(MISSING)
    found = subprocess.run([xcrun, '--find', 'swiftc'], stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if found.returncode != 0 or not found.stdout.strip():
        raise RuntimeError(MISSING)
    return [xcrun, 'swiftc']   # xcrun also selects the SDK

def build(out):
    out = Path(out)
    swiftc = [os.environ['SPRING_LOAD_SWIFTC']] if os.environ.get('SPRING_LOAD_SWIFTC') else find_swiftc()
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        cache = out.parent/'.swift-module-cache'
        result = subprocess.run([*swiftc, '-O', '-module-cache-path', str(cache), '-o', str(out), str(SOURCE)],
                                stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f'could not run swiftc: {error}') from None
    shutil.rmtree(out.parent/'.swift-module-cache', ignore_errors=True)
    if result.returncode != 0 or not out.exists():
        detail = (result.stderr or result.stdout).strip().splitlines()[-5:]
        raise RuntimeError('building the macOS sensor failed:\n' + '\n'.join(detail))
