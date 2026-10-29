"""Hyprland backend, best effort, over Hyprland's IPC socket. Needs no permission.

Hyprland's IPC reports the cursor position (j/cursorpos) and every client's
position, size, pid and workspace (j/clients, j/monitors), which is all that
hit-testing and client binding need. It reports nothing about drag-and-drop or
mouse buttons, and no Wayland protocol gives a third-party client either.
Button state from evdev would need the 'input' group and still could not tell
a file drag from a text selection, so it is not used.

So this sensor declares the 'window' and 'clients' capabilities but not
'drag', and the core stays inert under Hyprland (docs/SENSOR.md, section 3).
describe() is kept so the core can use it once Hyprland exposes a drag
signal, or for the terminal-native path.

Coordinates are Hyprland's global layout space: origin at the top-left of the
layout, y down, logical pixels. The window's 'scale' is its monitor's scale.
"""
import json
import os
import socket

import common

CAPABILITIES = ['window', 'clients']
COORDS = 'hyprland-layout'


def socket_path(env=os.environ):
    signature = env.get('HYPRLAND_INSTANCE_SIGNATURE')
    if not signature:
        return None
    runtime = env.get('XDG_RUNTIME_DIR')
    for base in ([os.path.join(runtime, 'hypr')] if runtime else []) + ['/tmp/hypr']:
        path = os.path.join(base, signature, '.socket.sock')
        if os.path.exists(path):
            return path
    return None


class Hyprland:
    def __init__(self, path):
        self.path = path
        self.cache = common.ClientCache()

    def request(self, command):
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(0.5)
            connection.connect(self.path)
            connection.sendall(('j/' + command).encode())
            data = bytearray()
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                data.extend(chunk)
        return json.loads(bytes(data))

    def cursor(self):
        position = self.request('cursorpos')
        return position['x'], position['y']

    def describe(self, x, y):
        """The window object for the topmost visible client at (x, y), or None."""
        monitors = self.request('monitors')
        visible, scale_of = set(), {}
        for monitor in monitors:
            for key in ('activeWorkspace', 'specialWorkspace'):
                workspace = monitor.get(key) or {}
                if workspace.get('id'):
                    visible.add(workspace['id'])
                    scale_of[workspace['id']] = monitor.get('scale')
        candidates = [c for c in self.request('clients')
                      if c.get('mapped') and not c.get('hidden')
                      and (c.get('workspace') or {}).get('id') in visible]
        hits = [c for c in candidates if contains(c, x, y)]
        if not hits:
            return None
        # Fullscreen over floating over tiled; within a layer, the most recently focused.
        hits.sort(key=lambda c: (not c.get('fullscreen'), not c.get('floating'), c.get('focusHistoryID', 1 << 30)))
        top = hits[0]
        pid = top.get('pid')
        if not isinstance(pid, int) or pid <= 0:
            return None
        return {'id': top.get('address', ''), 'pid': pid, 'app': top.get('class') or None,
                'bounds': rect(top), 'scale': scale_of.get(top['workspace']['id']),
                'peers': [rect(c) for c in candidates if c is not top and c.get('pid') == pid],
                'clients': self.cache.get(top.get('address'), pid)}


def contains(client, x, y):
    (cx, cy), (w, h) = client['at'], client['size']
    return cx <= x < cx + w and cy <= y < cy + h


def rect(client):
    return {'x': client['at'][0], 'y': client['at'][1], 'width': client['size'][0], 'height': client['size'][1]}


def run():
    path = socket_path()
    if not path:
        common.hello('linux-hyprland', COORDS, [])
        common.status('error', 'Hyprland IPC socket not found; sensor inert')
        return
    common.hello('linux-hyprland', COORDS, CAPABILITIES)
    common.status('warn', 'Hyprland reports no drag-and-drop or button state to other programs, '
                          'so spring-loading is off under Hyprland')
    if common.is_verbose():
        try:
            x, y = Hyprland(path).cursor()
            common.log('cursor %s,%s window %s' % (x, y, json.dumps(Hyprland(path).describe(x, y))))
        except (OSError, ValueError, KeyError) as error:
            common.log('Hyprland IPC failed: %s' % error)
