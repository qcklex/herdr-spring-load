"""Build the Linux sensor (docs/SENSOR.md, section 5): one executable zipapp, stdlib only."""
from pathlib import Path
import shutil
import tempfile
import zipapp

HERE = Path(__file__).resolve().parent
MODULES = ('main.py', 'common.py', 'tracker.py', 'x11.py', 'hyprland.py')


def build(out):
    """Write the sensor to `out` as an executable Python zipapp with a /usr/bin/env python3 shebang."""
    out = Path(out)
    with tempfile.TemporaryDirectory() as staging:
        for name in MODULES:
            source = HERE / name
            if not source.is_file():
                raise RuntimeError('Linux sensor source missing: %s' % source)
            shutil.copy2(source, Path(staging) / name)
        try:
            zipapp.create_archive(staging, out, interpreter='/usr/bin/env python3', main='main:main')
        except OSError as error:
            raise RuntimeError('could not write the Linux sensor to %s: %s' % (out, error)) from None
    out.chmod(0o755)


if __name__ == '__main__':
    import sys
    build(sys.argv[1] if len(sys.argv) > 1 else 'herdr-spring-load-sensor-linux')
