"""Validation of the sensor contract (docs/SENSOR.md, version 1).

python3 -m spring_load.contract < output.ndjson   checks recorded sensor output:
exit 1 on the first invalid line, or when the first line is not a valid hello."""
import json
import math
import sys

CONTRACT = 1
MAX_LINE = 64 * 1024
COORDS = {'macos-points', 'x11-root', 'windows-virtual'}
CAPABILITIES = {'drag', 'click', 'window', 'clients'}
KINDS = {'files', 'other', 'unknown'}
REASONS = {'released', 'cancelled'}
LEVELS = {'info', 'warn', 'error'}
CLIENT_ENV = {'HERDR_SESSION', 'HERDR_SOCKET_PATH', 'HERDR_CLIENT_SOCKET_PATH', 'HERDR_CONFIG_PATH', 'XDG_CONFIG_HOME',
              'XDG_STATE_HOME', 'HOME', 'APPDATA', 'LOCALAPPDATA', 'USERPROFILE'}

class ContractError(ValueError):
    pass

def _number(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContractError(f'{what} must be a finite number')
    return float(value)

def _int(value, what, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f'{what} must be an integer')
    return value

def _string(value, what, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ContractError(f'{what} must be a string')
    return value

def validate_rect(value, what='bounds'):
    if not isinstance(value, dict):
        raise ContractError(f'{what} must be an object')
    rect = {k: _number(value.get(k), f'{what}.{k}') for k in ('x', 'y', 'width', 'height')}
    if rect['width'] < 0 or rect['height'] < 0:
        raise ContractError(f'{what} has a negative size')
    return rect

def validate_client(value):
    if not isinstance(value, dict):
        raise ContractError('client must be an object')
    argv = value.get('argv')
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise ContractError('client.argv must be a string array')
    foreground = value.get('foreground')
    if foreground is not None and not isinstance(foreground, bool):
        raise ContractError('client.foreground must be a boolean or null')
    client = {'pid': _int(value.get('pid'), 'client.pid'), 'argv': list(argv),
              'tty': _string(value.get('tty'), 'client.tty', nullable=True),
              'rows': _int(value.get('rows'), 'client.rows', nullable=True),
              'cols': _int(value.get('cols'), 'client.cols', nullable=True),
              'xpixel': _int(value.get('xpixel', 0), 'client.xpixel'),
              'ypixel': _int(value.get('ypixel', 0), 'client.ypixel'),
              'foreground': foreground}
    started = value.get('started')
    client['started'] = None if started is None else _number(started, 'client.started')
    env = value.get('env')
    if env is not None:
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ContractError('client.env must be an object of strings or null')
        env = {k: v for k, v in env.items() if k in CLIENT_ENV}
    client['env'] = env
    for key in ('rows', 'cols', 'xpixel', 'ypixel'):
        if client[key] is not None and client[key] < 0:
            raise ContractError(f'client.{key} must not be negative')
    return client

def validate_window(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ContractError('window must be an object or null')
    scale = value.get('scale')
    if scale is not None and _number(scale, 'window.scale') <= 0:
        raise ContractError('window.scale must be positive')
    peers = value.get('peers', [])
    if not isinstance(peers, list):
        raise ContractError('window.peers must be an array')
    clients = value.get('clients')
    if clients is not None and not isinstance(clients, list):
        raise ContractError('window.clients must be an array or null')
    return {'id': _string(value.get('id'), 'window.id'), 'pid': _int(value.get('pid'), 'window.pid'),
            'app': _string(value.get('app'), 'window.app', nullable=True),
            'bounds': validate_rect(value.get('bounds')), 'scale': None if scale is None else float(scale),
            'peers': [validate_rect(p, 'window.peers[]') for p in peers],
            'clients': None if clients is None else [validate_client(c) for c in clients]}

def validate_hello(event):
    if event.get('type') != 'hello':
        raise ContractError('the first line must be a hello event')
    if event.get('contract') != CONTRACT:
        raise ContractError(f'unsupported sensor contract {event.get("contract")!r}; this core speaks {CONTRACT}')
    if event.get('coords') not in COORDS:
        raise ContractError(f'unknown coords {event.get("coords")!r}')
    capabilities = event.get('capabilities')
    if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
        raise ContractError('capabilities must be a string array')
    return {'type': 'hello', 't': _number(event.get('t'), 't'), 'contract': CONTRACT,
            'sensor': _string(event.get('sensor'), 'sensor'), 'version': _string(event.get('version'), 'version'),
            'coords': event['coords'], 'capabilities': [c for c in capabilities if c in CAPABILITIES]}

def _point(event, parsed):
    parsed['x'] = _number(event.get('x'), 'x')
    parsed['y'] = _number(event.get('y'), 'y')

def validate_event(event):
    """A validated copy of one decoded event, None for an unknown type."""
    if not isinstance(event, dict):
        raise ContractError('event must be a JSON object')
    kind = event.get('type')
    if kind == 'hello':
        return validate_hello(event)
    parsed = {'type': kind, 't': _number(event.get('t'), 't')}
    if kind == 'drag_start':
        _point(event, parsed)
        parsed['kind'] = event.get('kind', 'unknown')
        if parsed['kind'] not in KINDS:
            raise ContractError(f'unknown drag kind {parsed["kind"]!r}')
        parsed['window'] = validate_window(event.get('window'))
        everyone = event.get('all_clients')
        if everyone is not None and not isinstance(everyone, list):
            raise ContractError('all_clients must be an array or null')
        parsed['all_clients'] = None if everyone is None else [validate_client(c) for c in everyone]
    elif kind in ('drag_move', 'click'):
        _point(event, parsed)
        parsed['window'] = validate_window(event.get('window'))
    elif kind == 'drag_end':
        _point(event, parsed)
        parsed['reason'] = event.get('reason')
        if parsed['reason'] not in REASONS:
            raise ContractError(f'unknown drag_end reason {parsed["reason"]!r}')
    elif kind == 'status':
        parsed['level'] = event.get('level')
        if parsed['level'] not in LEVELS:
            raise ContractError(f'unknown status level {parsed["level"]!r}')
        parsed['message'] = _string(event.get('message'), 'message')
    else:
        return None
    return parsed

def decode(line):
    """Decode and validate one line; raises ContractError."""
    if isinstance(line, bytes):
        if len(line) > MAX_LINE:
            raise ContractError('line longer than 64 KiB')
        line = line.decode('utf-8')
    elif len(line.encode('utf-8')) > MAX_LINE:
        raise ContractError('line longer than 64 KiB')
    try:
        event = json.loads(line)
    except json.JSONDecodeError as error:
        raise ContractError(f'invalid JSON: {error}') from None
    return validate_event(event)

def parse_event(line):
    """One validated event, or None for an unknown type or an invalid line."""
    try:
        return decode(line)
    except (ContractError, UnicodeDecodeError):
        return None

def check_stream(lines):
    """Validate recorded sensor output. Returns (ok, message)."""
    seen_hello = False
    last_t = None
    dragging = False
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = decode(line.rstrip('\n') if isinstance(line, str) else line.rstrip(b'\n'))
            if not seen_hello:
                if event is None:
                    raise ContractError('the first line must be a hello event')
                validate_hello(event)
                seen_hello = True
            elif event is not None and event['type'] == 'hello':
                raise ContractError('hello sent twice')
            if event is None:
                continue
            if last_t is not None and event['t'] < last_t:
                raise ContractError('t went backwards')
            last_t = event['t']
            if event['type'] == 'drag_start':
                if dragging:
                    raise ContractError('drag_start before drag_end')
                dragging = True
            elif event['type'] == 'drag_end':
                dragging = False
            elif event['type'] == 'drag_move' and not dragging:
                raise ContractError('drag_move outside a drag')
        except (ContractError, UnicodeDecodeError) as error:
            return False, f'line {number}: {error}'
    if not seen_hello:
        return False, 'no hello'
    return True, 'ok'

if __name__ == '__main__':
    ok, message = check_stream(sys.stdin)
    print(message)
    sys.exit(0 if ok else 1)
