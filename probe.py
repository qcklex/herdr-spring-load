#!/usr/bin/env python3
"""Reply to the native helper from the client receiving the private probe key."""
import json
import os
from pathlib import Path
import socket
import time
import sys

BASE = Path.home() / 'Library/Application Support/herdr-spring-load/probes'

def request(path, method, params, timeout=0.6):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(timeout)
        connection.connect(path)
        connection.sendall((json.dumps({'id': 'spring-load', 'method': method, 'params': params})+'\n').encode())
        data = bytearray()
        while b'\n' not in data:
            chunk = connection.recv(65536)
            if not chunk or len(data) > 4_000_000:
                raise ValueError('invalid response')
            data.extend(chunk)
        result = json.loads(data.split(b'\n', 1)[0])
        if 'error' in result:
            raise ValueError('Herdr request failed: '+json.dumps(result['error'])[:300])
        return result['result']

def main(slot="1"):
    if slot not in {str(i) for i in range(1,8)}:
        return
    try:
        challenge = json.loads((BASE / ('request-'+slot+'.json')).read_text())
        if not 0 <= time.time() - challenge['time'] < 3:
            return
        context = json.loads(os.environ['HERDR_PLUGIN_CONTEXT_JSON'])
        pane = context.get('focused_pane_id') or os.environ.get('HERDR_PANE_ID')
        workspace = context.get('workspace_id') or os.environ.get('HERDR_WORKSPACE_ID')
        path = os.environ['HERDR_SOCKET_PATH']
        if not pane or not workspace:
            return
        layout = request(path, 'pane.layout', {'pane_id': pane})
        tabs = request(path, 'tab.list', {'workspace_id': workspace})
        for tab in tabs.get('tabs',[]):
            tab['focused'] = tab.get('tab_id') == context.get('tab_id')
        layout['layout']['focused_pane_id'] = pane
        spaces = request(path, 'workspace.list', {})
        for space in spaces.get('workspaces',[]):
            space['focused'] = space.get('workspace_id') == workspace
        response = {'token': challenge['token'], 'socket': path, 'pane': pane,
                    'workspace': workspace, 'layout': layout, 'tabs': tabs, 'spaces': spaces}
        # Do not let an old invocation satisfy a newer helper request.
        if json.loads((BASE / ('request-'+slot+'.json')).read_text()).get('token') != challenge['token']:
            return
        temp = BASE / ('reply.' + str(os.getpid()) + '.tmp')
        with temp.open('x') as out:
            json.dump(response, out)
        os.chmod(temp, 0o600)
        temp.replace(BASE / ('reply-'+slot+'.json'))
    except (OSError, ValueError, KeyError, TypeError):
        return

if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv)>1 else "1")
