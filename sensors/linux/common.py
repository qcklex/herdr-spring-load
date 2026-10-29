"""Shared parts of the Linux sensors: output, lifetime, and herdr clients from /proc.

Everything here only reads: /proc, and the tty size through TIOCGWINSZ.
"""
import fcntl
import json
import os
import struct
import sys
import termios
import threading
import time

VERSION = '0.3.0'
CONTRACT = 1
MOVE_MIN_INTERVAL = 0.030
MOVE_MAX_INTERVAL = 0.250
CLIENT_REFRESH = 1.0
HERDR_NAMES = {'herdr', 'herdr.exe'}
# Only these variables may leave a client's environment (docs/SENSOR.md, section 2.1).
CLIENT_ENV = ('HERDR_SESSION', 'HERDR_SOCKET_PATH', 'HERDR_CLIENT_SOCKET_PATH', 'HERDR_CONFIG_PATH',
              'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'HOME')

_start = time.monotonic()
_verbose = False
_watch_stdin = False


def now():
    return round(time.monotonic() - _start, 4)


def set_verbose(value):
    global _verbose
    _verbose = value


def is_verbose():
    return _verbose


def log(message):
    if _verbose:
        sys.stderr.write('sensor: ' + message + '\n')
        sys.stderr.flush()


def emit(event):
    """Write one event as a single line. Exit quietly once the core has gone."""
    event = dict(event)
    event.setdefault('t', now())
    try:
        sys.stdout.write(json.dumps(event, separators=(',', ':')) + '\n')
        sys.stdout.flush()
    except (BrokenPipeError, ValueError):
        os._exit(0)


def watch_stdin_after_hello():
    global _watch_stdin
    _watch_stdin = True


def hello(sensor, coords, capabilities):
    emit({'type': 'hello', 'contract': CONTRACT, 'sensor': sensor, 'version': VERSION,
          'coords': coords, 'capabilities': capabilities})
    # Only now: an early EOF must not stop the sensor before the hello is out.
    # An inert sensor (no capabilities) reports why first and then waits in main.
    if _watch_stdin and capabilities:
        exit_on_stdin_eof()


def status(level, message):
    emit({'type': 'status', 'level': level, 'message': message})


def wait_for_stdin_eof():
    try:
        while sys.stdin.buffer.read(4096):
            pass
    except (OSError, ValueError):
        pass


def exit_on_stdin_eof():
    """The core closes stdin to stop the sensor (docs/SENSOR.md, section 1)."""
    def wait():
        wait_for_stdin_eof()
        os._exit(0)
    threading.Thread(target=wait, daemon=True).start()


class MoveThrottle:
    """drag_move at most every 30 ms, on any change, and at least every 250 ms."""

    def __init__(self):
        self.last_time = None
        self.last_key = None

    def due(self, key, at):
        if self.last_time is not None and at - self.last_time < MOVE_MIN_INTERVAL:
            return False
        if key != self.last_key or self.last_time is None or at - self.last_time >= MOVE_MAX_INTERVAL:
            self.last_time, self.last_key = at, key
            return True
        return False


# Process tree and tty, from /proc.

def read_stat(pid, proc='/proc'):
    """(comm, ppid, pgrp, tty_nr, tpgid, starttime in clock ticks after boot) from /proc/<pid>/stat, or None."""
    try:
        with open('%s/%d/stat' % (proc, pid), 'rb') as handle:
            stat = handle.read().decode('utf-8', 'replace')
    except OSError:
        return None
    # comm can contain spaces and parentheses, so split after the last ')'.
    comm = stat[stat.index('(') + 1:stat.rindex(')')]
    fields = stat[stat.rindex(')') + 2:].split()
    return comm, int(fields[1]), int(fields[2]), int(fields[4]), int(fields[5]), int(fields[19])


def boot_time(proc='/proc'):
    """Seconds since the epoch at boot (btime in /proc/stat), or None."""
    try:
        with open('%s/stat' % proc) as handle:
            for line in handle:
                if line.startswith('btime '):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


def started_at(ticks, proc='/proc'):
    """Process start in seconds since the epoch (docs/SENSOR.md, client.started), or None."""
    boot = boot_time(proc)
    try:
        hz = os.sysconf('SC_CLK_TCK')
    except (ValueError, OSError):
        return None
    return round(boot + ticks / hz, 2) if boot is not None and hz > 0 else None


def read_argv(pid, proc='/proc'):
    try:
        with open('%s/%d/cmdline' % (proc, pid), 'rb') as handle:
            raw = handle.read()
    except OSError:
        return []
    return [arg.decode('utf-8', 'replace') for arg in raw.split(b'\0')[:-1]] if raw else []


def exe_name(pid, comm, argv, proc='/proc'):
    """Executable base name. comm is truncated to 15 characters, so prefer exe and argv[0]."""
    try:
        return os.path.basename(os.readlink('%s/%d/exe' % (proc, pid)))
    except OSError:
        pass
    return os.path.basename(argv[0]) if argv else comm


def children_map(proc='/proc'):
    children = {}
    try:
        entries = os.listdir(proc)
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        stat = read_stat(int(entry), proc)
        if stat:
            children.setdefault(stat[1], []).append((int(entry), stat))
    return children


def tty_path(tty_nr, proc='/proc', pid=None):
    """The controlling tty's path. Unix98 ptys decode directly; others go through fd 0."""
    if not tty_nr:
        return None
    major = (tty_nr >> 8) & 0xfff
    minor = (tty_nr & 0xff) | ((tty_nr >> 12) & 0xfff00)
    if 136 <= major <= 143:
        return '/dev/pts/%d' % ((major - 136) * 256 + minor)
    if pid is not None:
        for fd in (0, 1, 2):
            try:
                path = os.readlink('%s/%d/fd/%d' % (proc, pid, fd))
            except OSError:
                continue
            if path.startswith('/dev/'):
                try:
                    if os.stat(path).st_rdev == os.makedev(major, minor):
                        return path
                except OSError:
                    pass
    return None


def winsize(path):
    """(rows, cols, xpixel, ypixel) or None. Opening read-only never writes to the tty."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        return struct.unpack('HHHH', fcntl.ioctl(fd, termios.TIOCGWINSZ, b'\0' * 8))
    except OSError:
        return None
    finally:
        os.close(fd)


def read_env(pid, proc='/proc'):
    """The allowed variables from /proc/<pid>/environ, or None when it cannot be read."""
    try:
        with open('%s/%d/environ' % (proc, pid), 'rb') as handle:
            raw = handle.read()
    except OSError:
        return None
    env = {}
    for entry in raw.split(b'\0'):
        key, sep, value = entry.partition(b'=')
        name = key.decode('utf-8', 'replace')
        if sep and name in CLIENT_ENV:
            env[name] = value.decode('utf-8', 'replace')
    return env


def client_record(pid, stat, proc='/proc', size=winsize):
    """The contract client object for a herdr process with a controlling tty, else None."""
    comm, _, pgrp, tty_nr, tpgid, ticks = stat
    if not tty_nr:
        return None
    argv = read_argv(pid, proc)
    if exe_name(pid, comm, argv, proc) not in HERDR_NAMES:
        return None
    path = tty_path(tty_nr, proc, pid)
    rows = cols = None
    xpixel = ypixel = 0
    dims = size(path) if path else None
    if dims:
        rows, cols, xpixel, ypixel = dims
    return {'pid': pid, 'argv': argv, 'tty': path, 'rows': rows, 'cols': cols, 'xpixel': xpixel, 'ypixel': ypixel,
            'foreground': (pgrp == tpgid) if tpgid > 0 else None, 'started': started_at(ticks, proc),
            'env': read_env(pid, proc)}


def herdr_clients(owner_pid, proc='/proc', size=winsize):
    """Every herdr process with a controlling tty below owner_pid (docs/SENSOR.md, section 2.1).

    Returns None when the process tree cannot be read at all."""
    tree = children_map(proc)
    if tree is None:
        return None
    found, stack, seen = [], [owner_pid], set()
    while stack:
        parent = stack.pop()
        for pid, stat in tree.get(parent, []):
            if pid in seen:
                continue
            seen.add(pid)
            stack.append(pid)
            record = client_record(pid, stat, proc, size)
            if record:
                found.append(record)
    found.sort(key=lambda client: client['pid'])
    return found


def all_herdr_clients(proc='/proc', size=winsize, uid=None):
    """Every herdr client of this user on the machine, wherever its window is (drag_start.all_clients)."""
    uid = os.getuid() if uid is None else uid
    tree = children_map(proc)
    if tree is None:
        return None
    found = []
    for pid, stat in (entry for entries in tree.values() for entry in entries):
        try:
            if os.stat('%s/%d' % (proc, pid)).st_uid != uid:
                continue
        except OSError:
            continue
        record = client_record(pid, stat, proc, size)
        if record:
            found.append(record)
    found.sort(key=lambda client: client['pid'])
    return found


class ClientCache:
    """Client lookup on drag start, on a window change, and at most once a second otherwise."""

    def __init__(self, lookup=herdr_clients, clock=time.monotonic):
        self.lookup, self.clock = lookup, clock
        self.key = None
        self.value = None
        self.when = 0.0

    def reset(self):
        self.key = None

    def get(self, window_id, pid):
        at = self.clock()
        key = (window_id, pid)
        if key != self.key or at - self.when >= CLIENT_REFRESH:
            self.key, self.when = key, at
            self.value = self.lookup(pid)
        return self.value
