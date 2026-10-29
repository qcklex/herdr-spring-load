r"""herdr's local API: one newline-delimited JSON request per connection, and
an event subscription. The only state-changing calls the plugin makes are
workspace.focus, tab.focus and pane.focus.

Transport, as herdr 0.9.3's ipc.rs (interprocess 2.4.2) sets it up: a Unix
domain socket at the socket path on macOS and Linux; on Windows a byte-mode
named pipe called \\.\pipe\ followed by the socket path string verbatim
(GenericNamespaced), with a marker file left at the path itself."""
import json
import socket
import sys
import threading
import time

class ApiError(RuntimeError):
    pass

FOCUS_METHODS = {'space': ('workspace.focus', 'workspace_id'), 'tab': ('tab.focus', 'tab_id'), 'pane': ('pane.focus', 'pane_id')}

PIPE_PREFIX = '\\\\.\\pipe\\'
ERROR_PIPE_BUSY = 231

def pipe_name(path):
    """herdr's named pipe for a socket path on Windows."""
    return PIPE_PREFIX + str(path)

class PipeConnection:
    """A named-pipe client with the small part of the socket interface used
    here (sendall, recv with a timeout, shutdown, close)."""
    def __init__(self, name, timeout, opener=open):
        deadline = time.monotonic() + (timeout or 0)
        while True:
            try:
                self.file = opener(name, 'r+b', buffering=0)
                break
            except OSError as error:
                # All pipe instances busy: herdr creates the next one right after accepting.
                if getattr(error, 'winerror', None) == ERROR_PIPE_BUSY and time.monotonic() < deadline:
                    time.sleep(0.02)
                    continue
                raise
        self.timeout = timeout

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, data):
        view = memoryview(data)
        while view:
            written = self.file.write(view)
            if not written:
                raise OSError('the herdr pipe accepted no data')
            view = view[written:]

    def recv(self, size):
        if self.timeout is None:
            return self.file.read(size) or b''
        result = {}
        def read():
            try:
                result['data'] = self.file.read(size) or b''
            except OSError as error:
                result['error'] = error
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(self.timeout)
        if reader.is_alive():
            self._cancel()
            raise socket.timeout('herdr did not answer in time')
        if 'error' in result:
            raise result['error']
        return result['data']

    def _cancel(self):
        if sys.platform != 'win32':
            return
        try:
            import ctypes, msvcrt
            ctypes.windll.kernel32.CancelIoEx(msvcrt.get_osfhandle(self.file.fileno()), None)
        except (OSError, AttributeError, ValueError):
            pass

    def shutdown(self, how=None):
        self._cancel()

    def close(self):
        try:
            self.file.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

USE_PIPES = sys.platform == 'win32'
pipe_opener = open   # replaced in tests

def _connect(path, timeout):
    if USE_PIPES:
        return PipeConnection(pipe_name(path), timeout, opener=pipe_opener)
    if not hasattr(socket, 'AF_UNIX'):
        raise ApiError('this Python has no Unix sockets')
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(str(path))
    except OSError:
        connection.close()
        raise
    return connection

def _read_line(connection, limit=8_000_000):
    data = bytearray()
    while b'\n' not in data:
        chunk = connection.recv(65536)
        if not chunk:
            raise ApiError('herdr closed the connection')
        data.extend(chunk)
        if len(data) > limit:
            raise ApiError('response too large')
    line, _, rest = bytes(data).partition(b'\n')
    return line, rest

def request(path, method, params=None, timeout=1.0):
    with _connect(path, timeout) as connection:
        connection.sendall((json.dumps({'id': 'herdr-spring-load', 'method': method, 'params': params or {}}) + '\n').encode())
        line, _ = _read_line(connection)
    reply = json.loads(line)
    if 'error' in reply:
        raise ApiError(f'{method}: {json.dumps(reply["error"])[:300]}')
    return reply['result']

def snapshot(path, timeout=1.0):
    return request(path, 'session.snapshot', {}, timeout)['snapshot']

def focus(path, kind, target_id, timeout=1.0):
    method, key = FOCUS_METHODS[kind]
    return request(path, method, {key: target_id}, timeout)

class Subscription:
    """Focus events of one session on a background thread; `callback(event)`
    gets each decoded event dict. Reconnects are the caller's job."""
    TYPES = ('workspace.focused', 'tab.focused', 'pane.focused')

    def __init__(self, path, callback, types=TYPES):
        self.connection = _connect(path, 2.0)
        self.connection.sendall((json.dumps({'id': 'herdr-spring-load-events', 'method': 'events.subscribe',
                                             'params': {'subscriptions': [{'type': t} for t in types]}}) + '\n').encode())
        line, self.buffer = _read_line(self.connection)
        reply = json.loads(line)
        if 'error' in reply:
            self.connection.close()
            raise ApiError(f'events.subscribe: {reply["error"]}')
        self.callback = callback
        self.alive = True
        self.connection.settimeout(None)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        data = bytearray(self.buffer)
        try:
            while True:
                while b'\n' in data:
                    line, _, rest = bytes(data).partition(b'\n')
                    data = bytearray(rest)
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    self.callback(event)
                chunk = self.connection.recv(65536)
                if not chunk:
                    break
                data.extend(chunk)
        except OSError:
            pass
        self.alive = False

    def close(self):
        self.alive = False
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass
        self.connection.close()
