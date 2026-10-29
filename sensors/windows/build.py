"""Build the Windows sensor (docs/SENSOR.md, "Building and installing a sensor").

The sensor is Python, so the build packs it into a zip application and puts
pip's Windows script launcher in front of it, the same layout pip uses for
console scripts: launcher .exe, then a "#!<python>" line, then the zip. The
launcher runs that Python on the file itself, and Python runs the zip. The GUI
launcher and pythonw.exe are used so that no console window opens.

Standard library only. The launcher comes from an installed pip or from the
pip wheel that ensurepip bundles with Python."""
import glob
import importlib.util
import io
import os
from pathlib import Path
import platform
import sys
import zipfile

HERE = Path(__file__).resolve().parent
MODULES = ('__init__.py', 'logic.py', 'structs.py', 'win32.py', 'sensor.py')
MAIN = 'import sys\nfrom sensors.windows.sensor import main\nsys.exit(main())\n'

def launcher_name(machine=None):
    machine = (machine or platform.machine()).lower()
    return 'w64-arm.exe' if machine in ('arm64', 'aarch64') else 'w64.exe'

def find_launcher(name):
    """Bytes of pip's distlib launcher `name`, or None."""
    spec = importlib.util.find_spec('pip')
    if spec and spec.submodule_search_locations:
        for location in spec.submodule_search_locations:
            path = Path(location)/'_vendor/distlib'/name
            if path.is_file():
                return path.read_bytes()
    spec = importlib.util.find_spec('ensurepip')
    if spec and spec.origin:
        for wheel in sorted(glob.glob(str(Path(spec.origin).parent/'_bundled'/'pip-*.whl'))):
            with zipfile.ZipFile(wheel) as archive:
                try:
                    return archive.read('pip/_vendor/distlib/' + name)
                except KeyError:
                    continue
    return None

def windowless_python(executable=None):
    executable = Path(executable or sys.executable)
    candidate = executable.with_name('pythonw.exe')
    return candidate if candidate.is_file() else executable

def application():
    """The sensor as zip bytes: the sensors.windows package and a __main__."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('__main__.py', MAIN)
        archive.writestr('sensors/__init__.py', '')
        for name in MODULES:
            archive.write(HERE/name, 'sensors/windows/' + name)
    return data.getvalue()

def build(out, python=None, launcher=None):
    """Write the sensor executable to `out`. Raises RuntimeError with a
    user-facing message when it cannot be built."""
    out = Path(out)
    if launcher is None:
        name = launcher_name()
        launcher = find_launcher(name)
        if launcher is None:
            raise RuntimeError(f'cannot build the Windows sensor: pip\'s script launcher ({name}) was not found. '
                               'Install pip for this Python (python -m ensurepip) and run setup again.')
    interpreter = str(python or windowless_python())
    shebang = ('#!"' + interpreter + '"\n').encode('utf-8')
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_name(out.name + '.tmp')
    temp.write_bytes(launcher + shebang + application())
    os.chmod(temp, 0o755)
    os.replace(temp, out)

if __name__ == '__main__':
    build(Path(sys.argv[1]))
