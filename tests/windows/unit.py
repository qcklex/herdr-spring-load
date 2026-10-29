"""The unit suite on Windows (CI):

    python tests/windows/unit.py          the modules that can run on Windows (gating)
    python tests/windows/unit.py --all    unittest discover over everything (informational)

Some modules are written for macOS and Linux only: test_install,
test_isolated_herdr and test_macos_sensor import fcntl, pty or termios;
test_engine and test_linux_sensor serve fakes over AF_UNIX sockets, and
several tests create their temp folders in /private/tmp. --all shows what
that leaves out on Windows."""
from pathlib import Path
import sys
import unittest

TESTS = Path(__file__).resolve().parents[1]
ROOT = TESTS.parent
PORTABLE = ['test_clients', 'test_contract', 'test_geometry', 'test_herdr_api', 'test_render', 'test_windows_sensor']

def main():
    sys.path[:0] = [str(TESTS), str(ROOT)]
    loader = unittest.TestLoader()
    if '--all' in sys.argv:
        suite = loader.discover(str(TESTS), top_level_dir=str(TESTS))
    else:
        suite = loader.loadTestsFromNames(PORTABLE)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)

if __name__ == '__main__':
    main()
