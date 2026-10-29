"""Shared by setup.py and uninstall.py (both also kept in the support folder,
since herdr deletes the plugin checkout on uninstall): transactional file
changes and the per-OS login service that runs `python3 -m spring_load run`:
a LaunchAgent on macOS, a systemd user unit on Linux, and (experimental) a
per-user Run key entry on Windows. None needs administrator rights or any
permission."""
import json
import os
from pathlib import Path
import plistlib
import signal
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from spring_load.lock import held, try_lock
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

def catch_signals():
    for name in ('SIGTERM', 'SIGHUP'):   # Windows has no SIGHUP
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), interrupt)

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

    def reconcile(self):
        """Nothing: launchd reads the plist itself."""

def systemd_quote(arg):
    """One ExecStart= word, quoted for systemd (and % escaped from specifiers)."""
    return '"' + arg.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'

class Systemd:
    """A systemd user unit. It is wanted by default.target and by
    graphical-session.target: started at login, and started again once the
    desktop has published DISPLAY or WAYLAND_DISPLAY to the user manager, so
    a start before that (the sensor goes inert and exits) does not stick."""
    name = 'systemd user service'
    unit = LABEL + '.service'
    SESSION_KEYS = ('DISPLAY', 'XAUTHORITY', 'WAYLAND_DISPLAY', 'XDG_SESSION_TYPE', 'HYPRLAND_INSTANCE_SIGNATURE')

    def __init__(self, home=None, env=None):
        env = os.environ if env is None else env
        home = Path(home or env.get('HOME') or Path.home())
        self.units = Path(env.get('XDG_CONFIG_HOME') or home/'.config')/'systemd/user'
        self.plist = self.units/self.unit     # the definition file, named as on macOS

    def systemctl(self, *args):
        return run([tool('systemctl'), '--user', *args])

    def definition(self, python, lib, args, env):
        environment = {**env, 'PYTHONPATH': str(lib), 'PYTHONDONTWRITEBYTECODE': '1'}
        lines = ['[Unit]', 'Description=herdr-spring-load: spring-loaded drag navigation for herdr',
                 'After=graphical-session.target', '',
                 '[Service]', 'ExecStart=' + ' '.join(systemd_quote(a) for a in [python, '-m', 'spring_load', 'run', *args]),
                 'WorkingDirectory=' + str(lib.parent),
                 *('Environment=' + systemd_quote(f'{k}={v}') for k, v in sorted(environment.items())),
                 'Restart=on-failure', 'RestartSec=10', '',
                 '[Install]', 'WantedBy=default.target graphical-session.target', '']
        return '\n'.join(lines).encode()

    def loaded(self):
        result = self.systemctl('is-active', self.unit)
        return result.stdout.strip() in ('active', 'activating', 'reloading', 'deactivating')

    def stop(self, timeout=10):
        self.systemctl('stop', self.unit)
        deadline = time.monotonic() + timeout
        while self.loaded():
            if time.monotonic() > deadline:
                raise SetupError('the previous helper did not stop')
            time.sleep(0.1)

    def start(self, attempts=3):
        if self.systemctl('show-environment').returncode != 0:
            raise SetupError('no systemd user session answers (systemctl --user); herdr-spring-load needs one '
                             'to start at login')
        # The user manager may not have this login's display yet (the desktop
        # publishes it at login; a bare startx does not). Pass on what setup sees.
        manager = dict(line.split('=', 1) for line in self.systemctl('show-environment').stdout.splitlines() if '=' in line)
        if not (manager.get('DISPLAY') or manager.get('WAYLAND_DISPLAY')):
            keys = [k for k in self.SESSION_KEYS if os.environ.get(k)]
            if keys:
                self.systemctl('import-environment', *keys)
        for step in (('daemon-reload',), ('enable', self.unit)):
            result = self.systemctl(*step)
            if result.returncode != 0:
                raise SetupError(f'systemctl --user {step[0]} failed: {(result.stderr or result.stdout).strip()[-300:]}')
        for attempt in range(attempts):
            result = self.systemctl('restart', self.unit)
            if result.returncode == 0:
                return
            time.sleep(0.5 * (attempt + 1))
        raise SetupError(f'starting the helper service failed: {(result.stderr or result.stdout).strip()[-300:]}')

    def reconcile(self):
        """After the unit file was removed or restored: drop `enable`'s links
        to a removed unit (systemctl disable needs the file), then reload."""
        if not self.plist.exists():
            for link in self.units.glob(f'*.wants/{self.unit}'):
                if link.is_symlink():
                    link.unlink()
        self.systemctl('daemon-reload')

class RunKey:
    """Windows (experimental): a value under HKCU\\...\\CurrentVersion\\Run
    starts the core at logon with pythonw (no console window); setup also
    starts it right away. No service manager tracks it, so the core's
    helper.lock tells whether it runs and helper.pid which process to stop."""
    name = 'Run key entry'
    KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
    VALUE = 'herdr-spring-load'

    def __init__(self, support=None):
        self.support = Path(support or support_dir())
        self.plist = self.support/'service.json'   # the definition file, named as on macOS

    def definition(self, python, lib, args, env):
        pythonw = Path(python).with_name('pythonw.exe')
        executable = str(pythonw if pythonw.is_file() else python)
        return json.dumps({'argv': [executable, str(lib/'run_helper.py'), 'run', *args], 'env': env}, indent=1).encode()

    def argv(self):
        return json.loads(self.plist.read_text())['argv']

    def registry(self, value=None, delete=False):
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.KEY) as key:
            if delete:
                try:
                    winreg.DeleteValue(key, self.VALUE)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(key, self.VALUE, 0, winreg.REG_SZ, value)

    def loaded(self):
        return held(self.support/'helper.lock')

    def stop(self, timeout=10):
        if not self.loaded():
            return
        # While the lock is held, helper.pid names the process that holds it.
        pid = int((self.support/'helper.pid').read_text().strip())
        run([tool('taskkill'), '/PID', str(pid), '/T', '/F'])
        deadline = time.monotonic() + timeout
        while self.loaded():
            if time.monotonic() > deadline:
                raise SetupError('the previous helper did not stop')
            time.sleep(0.1)

    def start(self, attempts=3):
        argv = self.argv()
        self.registry(subprocess.list2cmdline(argv))
        flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) \
            | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        subprocess.Popen(argv, cwd=str(self.support), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        deadline = time.monotonic() + 5 * attempts
        while not self.loaded():
            if time.monotonic() > deadline:
                raise SetupError(f'the helper did not start; see {log_file()}')
            time.sleep(0.2)

    def reconcile(self):
        """The Run key follows the definition file: removed with it, and
        pointed at it again when a rollback restored it."""
        if self.plist.exists():
            self.registry(subprocess.list2cmdline(self.argv()))
        else:
            self.registry(delete=True)

SERVICES = {'darwin': Launchd, 'linux': Systemd, 'win32': RunKey}

def service_for(platform=None):
    platform = platform or sys.platform
    backend = SERVICES.get('linux' if platform.startswith('linux') else platform)
    return backend() if backend else None
