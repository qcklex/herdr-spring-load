"""herdr's local API: one newline-delimited JSON request per connection, and
an event subscription. The only state-changing calls the plugin makes are
workspace.focus, tab.focus and pane.focus."""
import json
import socket
import threading

class ApiError(RuntimeError):
    pass

FOCUS_METHODS = {'space': ('workspace.focus', 'workspace_id'), 'tab': ('tab.focus', 'tab_id'), 'pane': ('pane.focus', 'pane_id')}

def _connect(path, timeout):
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
        except OSError:
            pass
        self.connection.close()
