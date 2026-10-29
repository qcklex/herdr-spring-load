"""Shared by setup.py and uninstall.py (both also kept in the support folder,
since herdr deletes the plugin checkout on uninstall): transactional file
changes and the per-OS login service that runs `python3 -m spring_load run`.

Only macOS has a service backend so far (a LaunchAgent). A Linux backend (a
systemd user unit or an XDG autostart entry) plugs in through SERVICES."""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from spring_load.paths import log_file, support_dir

LABEL = 'dev.qcklex.herdr-spring-load'
VERSION = '0.3.0'
SEEN = 'registered-seen'

class SetupError(Exception):
    pass

class Interrupted(BaseException):
    pass

def interrupt(signum, frame):
    # A killed install (SIGTERM/SIGHUP) still rolls back.
    raise Interrupted(f'interrupted by signal {signum}')

def tool(name):
    path = shutil.which(name)
    if not path:
        raise SetupError(f'{name} not found')
    return path

def run(argv, input=None, **kwargs):
    # No shell: paths stay data.
    if input is None:
        kwargs['stdin'] = subprocess.DEVNULL
    return subprocess.run(argv, input=input, capture_output=True, text=True, **kwargs)

def checked(argv, what, input=None):
    result = run(argv, input=input)
    if result.returncode != 0:
        raise SetupError(f'{what} failed: {(result.stderr or result.stdout).strip()[-500:]}')
    return result.stdout

def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

def real_path(path):
    """The file a config path really names (dotfile managers symlink it)."""
    return path.resolve() if path.is_symlink() else path

def file_mode(path, default=0o600):
    return path.stat().st_mode & 0o777 if path.exists() else default

class Transaction:
    """Every side effect registers its undo; rollback restores the previous
    state, then runs `after` steps. `commit` runs cleanups that must wait until
    nothing can fail any more."""
    def __init__(self):
        self.undo = []
        self.after = []
        self.cleanups = []

    def write(self, path, data, mode=None):
        previous = path.read_bytes() if path.exists() else None
        old_mode = file_mode(path) if previous is not None else None
        self.mkdir(path.parent)
        self.undo.append(lambda: atomic_write(path, previous, old_mode) if previous is not None else path.unlink(missing_ok=True))
        atomic_write(path, data, mode if mode is not None else (old_mode or 0o600))

    def remove(self, path):
        if not path.exists() or path.is_dir():
            return
        previous, mode = path.read_bytes(), file_mode(path)
        self.undo.append(lambda: atomic_write(path, previous, mode))
        path.unlink()

    def mkdir(self, path):
        missing = []
        for parent in [path, *path.parents]:
            if parent.exists():
                break
            missing.append(parent)
        path.mkdir(parents=True, exist_ok=True)
        # Removed on rollback only when empty: another tool may have written there.
        for made in missing:
            self.undo.append(lambda made=made: made.rmdir() if made.exists() and not any(made.iterdir()) else None)

    def replace_dir(self, staged, target):
        """Move a staged directory into place; the old one is kept until commit."""
        self.mkdir(target.parent)
        incoming = target.with_name(target.name + f'.new-{time.time_ns()}')
        shutil.copytree(staged, incoming)
        old = None
        if target.exists():
            old = target.with_name(target.name + f'.old-{time.time_ns()}')
            os.replace(target, old)
        os.replace(incoming, target)
        def undo():
            shutil.rmtree(target, ignore_errors=True)
            if old is not None and old.exists():
                os.replace(old, target)
        self.undo.append(undo)
        if old is not None:
            self.cleanups.append(lambda: shutil.rmtree(old, ignore_errors=True))

    def rollback(self):
        for step in [*reversed(self.undo), *self.after]:
            try:
                step()
            except (OSError, SetupError, subprocess.SubprocessError):
                pass
        self.undo, self.after, self.cleanups = [], [], []

    def commit(self):
        for step in self.cleanups:
            try:
                step()
            except OSError:
                pass
        self.undo, self.after, self.cleanups = [], [], []

class Launchd:
    """A LaunchAgent in the user's Aqua session. It asks for no permission:
    the sensor it starts uses none."""
    name = 'LaunchAgent'

    def __init__(self, home=None):
        home = Path(home or os.environ.get('HOME') or Path.home())
        self.plist = home/'Library/LaunchAgents'/(LABEL + '.plist')
        self.target = f'gui/{os.getuid()}/{LABEL}'

    def definition(self, python, lib, args, env):
        return plistlib.dumps({
            'Label': LABEL, 'ProgramArguments': [python, '-m', 'spring_load', 'run', *args],
            'EnvironmentVariables': {**env, 'PYTHONPATH': str(lib), 'PYTHONDONTWRITEBYTECODE': '1'},
            'WorkingDirectory': str(lib.parent), 'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False},
            'ThrottleInterval': 10, 'ProcessType': 'Interactive', 'LimitLoadToSessionType': 'Aqua',
            'StandardErrorPath': str(log_file().with_suffix('.stderr.log'))})

    def loaded(self):
        return run([tool('launchctl'), 'print', self.target]).returncode == 0

    def stop(self, timeout=10):
        launchctl = tool('launchctl')
        run([launchctl, 'bootout', self.target])
        deadline = time.monotonic() + timeout
        while run([launchctl, 'print', self.target]).returncode == 0:   # bootout returns early
            if time.monotonic() > deadline:
                raise SetupError('the previous helper did not stop')
            time.sleep(0.1)

    def start(self, attempts=3):
        launchctl = tool('launchctl')
        for attempt in range(attempts):
            result = run([launchctl, 'bootstrap', f'gui/{os.getuid()}', str(self.plist)])
            if result.returncode == 0:
                return
            time.sleep(0.5 * (attempt + 1))   # "5: Input/output error" while the old job finishes
        raise SetupError(f'starting the helper LaunchAgent failed: {(result.stderr or result.stdout).strip()[-300:]}')

SERVICES = {'darwin': Launchd}

def service_for(platform=None):
    backend = SERVICES.get(platform or sys.platform)
    return backend() if backend else None
