#!/usr/bin/env python3
"""Record golden screens from a real herdr for tests/test_render.py.

    python3 tools/record_golden.py [scenario ...]

Each scenario runs its own throwaway herdr server and client in a scratch
HOME/XDG and a private PTY, sets up workspaces and tabs through the API, and
stores the client's screen (rebuilt by tests/vt.py) plus everything the
renderer replica reads: the config, the client preferences file and
session.snapshot. Never connects to any other herdr session."""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tests'))
from vt import Screen

OUT = ROOT/'tests/golden'
BASE_CONFIG = 'onboarding = false\n[update]\nversion_check = false\nmanifest_check = false\n'

def request(path, method, params, timeout=3):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall((json.dumps({'id': 'golden', 'method': method, 'params': params}) + '\n').encode())
        data = b''
        while b'\n' not in data:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    reply = json.loads(data.split(b'\n')[0])
    if 'error' in reply:
        raise RuntimeError(f'{method}: {reply["error"]}')
    return reply['result']

def preferences_path(socket_path, state_dir):
    value = 0xcbf29ce484222325
    for byte in str(socket_path).encode():
        value = ((value ^ byte) * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    return Path(state_dir)/'client-shell'/f'local-{value:016x}.json'

GIT_REPOS = []

def git_repo(path, branch='main'):
    GIT_REPOS.append(str(path))
    path.mkdir(parents=True)
    (path/'.git/refs/heads').mkdir(parents=True)
    (path/'.git/objects').mkdir()
    (path/'.git/HEAD').write_text(f'ref: refs/heads/{branch}\n')
    (path/'.git/config').write_text('[core]\n\trepositoryformatversion = 0\n')
    return path

def run(name, cols, rows, config='', prefs=None, setup=None, xpixel=0, ypixel=0):
    herdr = shutil.which('herdr')
    GIT_REPOS.clear()
    with tempfile.TemporaryDirectory(prefix='sl-golden.', dir='/private/tmp') as temp:
        base = Path(temp)
        config_file = base/'c/herdr/config.toml'
        config_file.parent.mkdir(parents=True)
        config_file.write_text(BASE_CONFIG + config)
        sock = str(base/'c/herdr/herdr.sock')
        if prefs is not None:
            path = preferences_path(base/'c/herdr/herdr-client.sock', base/'s/herdr')
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(prefs))
        home = base/'home'
        home.mkdir()
        # A fixed prompt: a login /bin/sh would print the host and user names.
        shell = base/'shell'
        shell.write_text("#!/bin/sh\nPS1='$ ' exec /bin/sh --norc --noprofile -i\n")
        shell.chmod(0o755)
        env = {'HOME': str(home), 'XDG_CONFIG_HOME': str(base/'c'), 'XDG_STATE_HOME': str(base/'s'),
               'HERDR_CONFIG_PATH': str(config_file), 'PATH': os.environ['PATH'], 'TERM': 'xterm-256color',
               'SHELL': str(shell), 'LANG': 'en_US.UTF-8', 'TMPDIR': temp}
        pid, fd = pty.fork()
        if pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, xpixel, ypixel))
            os.chdir(home)
            os.execve(herdr, [herdr], env)
        screen = Screen(cols, rows)
        stamp = [time.monotonic()]
        stop = threading.Event()
        def drain():
            while not stop.is_set():
                try:
                    if select.select([fd], [], [], .05)[0]:
                        chunk = os.read(fd, 65536)
                        if not chunk:
                            return
                        screen.feed(chunk)
                        stamp[0] = time.monotonic()
                except OSError:
                    return
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        def settle(quiet=0.8, limit=8):
            until = time.monotonic() + limit
            while time.monotonic() < until and time.monotonic() - stamp[0] < quiet:
                time.sleep(0.05)
        try:
            until = time.time() + 10
            while True:
                try:
                    request(sock, 'workspace.list', {})
                    break
                except (OSError, RuntimeError):
                    if time.time() > until:
                        raise
                    time.sleep(0.1)
            until = time.time() + 10
            while 'spaces' not in '\n'.join(screen.lines()) and time.time() < until and cols > 64:
                time.sleep(0.05)
            if setup:
                setup(sock, base)
            settle()
            snapshot = request(sock, 'session.snapshot', {})['snapshot']
            settle(0.5)
            prefs_file = preferences_path(base/'c/herdr/herdr-client.sock', base/'s/herdr')
            record = {'name': name, 'herdr': snapshot['version'], 'cols': cols, 'rows': rows,
                      'config': BASE_CONFIG + config, 'socket': sock, 'temp': temp,
                      'preferences': json.loads(prefs_file.read_text()) if prefs_file.exists() else None,
                      'git_repos': list(GIT_REPOS), 'snapshot': snapshot, 'screen': screen.lines()}
        finally:
            try:
                request(sock, 'server.stop', {})
            except (OSError, RuntimeError):
                pass
            stop.set()
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    break
                time.sleep(0.3)
            reader.join(1)
            os.close(fd)
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
    # Paths under the scratch directory are rewritten so fixtures are stable.
    text = json.dumps(record, indent=1, ensure_ascii=False).replace(temp, '$TEMP')
    OUT.mkdir(exist_ok=True)
    (OUT/f'{name}.json').write_text(text + '\n')
    print(f'recorded {name}')

def spaces_and_tabs(sock, base):
    first = request(sock, 'workspace.list', {})['workspaces'][0]['workspace_id']
    request(sock, 'workspace.rename', {'workspace_id': first, 'label': 'project'})
    plain = base/'plain'
    plain.mkdir()
    request(sock, 'workspace.create', {'cwd': str(plain), 'label': 'assets'})
    tab = request(sock, 'tab.create', {'workspace_id': first, 'label': 'files', 'cwd': str(plain)})['tab']['tab_id']
    request(sock, 'tab.create', {'workspace_id': first, 'label': 'a much longer tab name', 'cwd': str(plain)})
    request(sock, 'tab.focus', {'tab_id': tab})
    pane = request(sock, 'pane.list', {'workspace_id': first})['panes']
    focused = next(p['pane_id'] for p in pane if p['focused'])
    request(sock, 'pane.split', {'pane_id': focused, 'direction': 'right'})

def zoomed_tab(sock, base):
    spaces_and_tabs(sock, base)
    focused = request(sock, 'session.snapshot', {})['snapshot']['focused_pane_id']
    request(sock, 'pane.zoom', {'pane_id': focused})

def git_spaces(sock, base):
    repo = git_repo(base/'repo')
    first = request(sock, 'workspace.list', {})['workspaces'][0]['workspace_id']
    request(sock, 'workspace.create', {'cwd': str(repo), 'label': 'repo'})
    plain = base/'plain'
    plain.mkdir()
    request(sock, 'workspace.create', {'cwd': str(plain), 'label': 'plain'})
    request(sock, 'workspace.create', {'cwd': str(repo), 'label': 'again'})
    request(sock, 'workspace.focus', {'workspace_id': first})

def wide_labels(sock, base):
    first = request(sock, 'workspace.list', {})['workspaces'][0]['workspace_id']
    request(sock, 'workspace.rename', {'workspace_id': first, 'label': 'données'})
    request(sock, 'workspace.create', {'cwd': str(base), 'label': '日本語'})
    request(sock, 'tab.create', {'workspace_id': first, 'label': '表示 tab', 'cwd': str(base)})
    request(sock, 'tab.create', {'workspace_id': first, 'label': 'café', 'cwd': str(base)})

def many_tabs(sock, base):
    first = request(sock, 'workspace.list', {})['workspaces'][0]['workspace_id']
    for i in range(14):
        request(sock, 'tab.create', {'workspace_id': first, 'label': f'tab number {i}', 'cwd': str(base)})

def many_spaces(sock, base):
    for i in range(30):
        request(sock, 'workspace.create', {'cwd': str(base), 'label': f'space {i}'})

SCENARIOS = {
    'default': dict(cols=120, rows=40),
    'spaces_tabs': dict(cols=120, rows=40, config='[ui]\nprompt_new_tab_name = false\n', setup=spaces_and_tabs),
    'zoomed': dict(cols=140, rows=30, config='[ui]\nprompt_new_tab_name = false\n', setup=zoomed_tab),
    'git_rows': dict(cols=100, rows=36, setup=git_spaces),
    'bottom_wide_gap': dict(cols=130, rows=40, setup=git_spaces,
                            config='[ui]\ntab_bar_position = "bottom"\nsidebar_width = 31\n[ui.sidebar.spaces]\nrow_gap = 1\n'),
    'one_row_spaces': dict(cols=110, rows=32, setup=spaces_and_tabs,
                           config='[ui]\nprompt_new_tab_name = false\n[ui.sidebar.spaces]\nrows = [["state_icon", "workspace"]]\n'),
    'collapsed_prefs': dict(cols=100, rows=30, setup=spaces_and_tabs, prefs={'sidebar_collapsed': True},
                            config='[ui]\nprompt_new_tab_name = false\n'),
    'narrow_prefs_split': dict(cols=100, rows=30, setup=git_spaces, prefs={'sidebar_width': 20, 'sidebar_section_split': 0.7}),
    'hidden_single_tab': dict(cols=100, rows=30, config='[ui]\nhide_tab_bar_when_single_tab = true\n'),
    'status_text': dict(cols=120, rows=30, setup=spaces_and_tabs,
                        config='[ui]\nprompt_new_tab_name = false\ntab_bar_right = [{ type = "text", text = "build ok" }, { type = "zoom" }]\n'),
    'wide_labels': dict(cols=110, rows=30, config='[ui]\nprompt_new_tab_name = false\n', setup=wide_labels),
    'tab_overflow': dict(cols=100, rows=30, setup=many_tabs),
    'space_scroll': dict(cols=100, rows=24, setup=many_spaces),
    'mobile': dict(cols=60, rows=24),
}

if __name__ == '__main__':
    names = sys.argv[1:] or list(SCENARIOS)
    for name in names:
        run(name, **SCENARIOS[name])
