"""Finding, starting and reading the per-OS sensor (docs/SENSOR.md, section 5)."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

from .contract import ContractError, MAX_LINE, decode, validate_hello

SENSOR_ENV = 'SPRING_LOAD_SENSOR'

class SensorError(RuntimeError):
    pass

def platform_key(platform=None):
    platform = platform or sys.platform
    if platform == 'darwin':
        return 'macos'
    if platform.startswith('linux'):
        return 'linux'
    if platform in ('win32', 'cygwin'):
        return 'windows'
    raise SensorError(f'unsupported platform {platform}')

def sensor_command(support_dir, environ=None, platform=None):
    """argv of the sensor to start, or None when there is none."""
    environ = os.environ if environ is None else environ
    override = environ.get(SENSOR_ENV)
    if override:
        argv = json.loads(override)
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise SensorError(f'{SENSOR_ENV} must be a JSON array of strings')
        return argv
    key = platform_key(platform)
    binary = Path(support_dir)/'sensors'/(f'herdr-spring-load-sensor-{key}' + ('.exe' if key == 'windows' else ''))
    return [str(binary)] if runnable(binary, key) else None

def runnable(path, key):
    """Whether `path` can be started as the sensor. Windows has no executable
    bit (os.access(X_OK) is true for any file there): an .exe file is
    runnable. Elsewhere the file needs its executable bit."""
    if not path.is_file():
        return False
    if key == 'windows':
        return path.suffix.lower() == '.exe'
    return os.access(path, os.X_OK)

class SensorProcess:
    """Starts the sensor and yields its validated events in order; the first is the hello."""
    def __init__(self, argv, env=None, log=None):
        self.log = log or (lambda message: None)
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=env)
        self.events = queue.Queue()
        self.hello = None
        self.dropped = set()
        self.closed = False
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()

    def _read(self):
        stream = self.process.stdout
        while True:
            line = stream.readline(MAX_LINE + 1)
            if not line:
                break
            if len(line) > MAX_LINE and not line.endswith(b'\n'):
                while line and not line.endswith(b'\n'):
                    line = stream.readline(MAX_LINE + 1)
                self._drop('line longer than 64 KiB')
                continue
            self.events.put(line.rstrip(b'\n'))
        self.events.put(None)

    def _stderr(self):
        for line in self.process.stderr:
            self.log('sensor: ' + line.decode('utf-8', 'replace').rstrip())

    def _drop(self, why):
        if why not in self.dropped:
            self.dropped.add(why)
            self.log(f'dropped sensor event: {why}')

    def next(self, timeout=None):
        """The next valid event, None on timeout. Raises SensorError when the sensor is gone."""
        while True:
            try:
                line = self.events.get(timeout=timeout)
            except queue.Empty:
                return None
            if line is None:
                self.events.put(None)
                code = self.process.poll()
                raise SensorError(f'sensor exited ({code})' if self.hello else 'sensor exited before its hello')
            try:
                event = decode(line)
            except (ContractError, UnicodeDecodeError) as error:
                if self.hello is None:
                    self.close()
                    raise SensorError(f'invalid sensor hello: {error}')
                self._drop(str(error))
                continue
            if self.hello is None:
                try:
                    self.hello = validate_hello(event or {})
                except ContractError as error:
                    self.close()
                    raise SensorError(str(error))
                return self.hello
            if event is not None and event['type'] != 'hello':
                return event

    def __iter__(self):
        while True:
            yield self.next()

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        for stream in (self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except OSError:
                pass
