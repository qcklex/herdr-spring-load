"""Opt-in integration against a real herdr 0.9.3, entirely in a scratch HOME/XDG
with its own PTYs and server:

    HERDR_TEST_ISOLATED=1 python3 -m unittest tests.test_isolated_herdr -v

The real macOS sensor binary describes the herdr client processes (argv, tty,
TIOCGWINSZ size, environment, start time) and the core runs in-process. Only
pointer and drag events are scripted, since a test cannot hold a real drag
without synthesising input (which the plugin never does). It checks:

1. one ordinary click on a different tab calibrates (herdr's own focus events);
2. one held drag focuses a Space, then a tab, then a pane, through the API only;
3. the native drop still lands in that pane: the test writes the bracketed
   paste a terminal sends for a dropped file. No plugin code delivers it;
4. with a second client attached to the session, nothing happens.

Never reads or connects to any other herdr session."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import pty
import queue
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from spring_load import contract, engine as core, herdr_api, render

COLS, ROWS, CELL_W, CELL_H = 120, 40, 8, 20
TITLEBAR, LEFT = 28, 2
WINDOW = {'x': 100.0, 'y': 50.0, 'width': COLS * CELL_W + 4, 'height': ROWS * CELL_H + TITLEBAR}
CONFIG = ('onboarding = false\n[update]\nversion_check = false\nmanifest_check = false\n'
          '[ui]\nsidebar_width = 26\nprompt_new_tab_name = false\n')

class FeedSensor:
    """The sensor side of the contract, fed by the test in real time."""
    def __init__(self):
        self.queue = queue.Queue()

    def put(self, event):
        self.queue.put(contract.validate_event(dict(event, t=time.monotonic())))

    def next(self, timeout=None):
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

class Client:
    """A herdr client on its own PTY."""
    def __init__(self, binary, env, cwd):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack('HHHH', ROWS, COLS, COLS * CELL_W, ROWS * CELL_H))
            os.chdir(cwd)
            os.execve(binary, [binary], env)
        self.stop = threading.Event()
        self.reader = threading.Thread(target=self.drain, daemon=True)
        self.reader.start()

    def drain(self):
        while not self.stop.is_set():
            try:
                if select.select([self.fd], [], [], 0.1)[0] and not os.read(self.fd, 65536):
                    break
            except OSError:
                break

    def write(self, data):
        os.write(self.fd, data)

    def close(self):
        self.stop.set()
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        self.reader.join(1)
        os.close(self.fd)
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            pass

def wait(check, timeout=8):
    until = time.time() + timeout
    while time.time() < until:
        try:
            value = check()
            if value:
                return value
        except (OSError, ValueError, KeyError, StopIteration, AttributeError, herdr_api.ApiError):
            pass
        time.sleep(0.05)
    return None

@unittest.skipUnless(os.environ.get('HERDR_TEST_ISOLATED') == '1' and shutil.which('herdr') and sys.platform == 'darwin',
                     'opt-in isolated herdr integration (macOS sensor)')
class IsolatedHerdrTest(unittest.TestCase):
    def test_space_tab_pane_by_api_native_drop_and_two_clients(self):
        binary = shutil.which('herdr')
        with tempfile.TemporaryDirectory(prefix='sl-int.', dir='/private/tmp') as temp:
            base = Path(temp)
            sensor_binary = base/'herdr-spring-load-sensor-macos'
            spec = importlib.util.spec_from_file_location('macos_build', ROOT/'sensors/macos/build.py')
            build = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(build)
            build.build(sensor_binary)
            config = base/'c/herdr/config.toml'
            config.parent.mkdir(parents=True)
            config.write_text(CONFIG)
            (config.parent/'plugins.json').write_text(json.dumps([{'plugin_id': 'herdr-spring-load', 'enabled': True,
                                                                  'manifest_path': str(ROOT/'herdr-plugin.toml')}]))
            os.utime(config, (time.time() - 5, time.time() - 5))   # the client starts after its config
            env = {'HOME': temp, 'XDG_CONFIG_HOME': str(base/'c'), 'XDG_STATE_HOME': str(base/'s'),
                   'PATH': '/usr/bin:/bin', 'TERM': 'xterm-256color', 'SHELL': '/bin/sh', 'LANG': 'en_US.UTF-8',
                   'TMPDIR': temp}
            sock = str(config.parent/'herdr.sock')
            api = lambda method, **params: herdr_api.request(sock, method, params, timeout=3)
            snap = lambda: herdr_api.snapshot(sock, timeout=3)
            ui = render.ui_from_config(tomllib.loads(CONFIG))
            model = lambda: render.build_model(COLS, ROWS, snap(), ui, render.Preferences())
            first, second, runner = Client(binary, env, temp), None, None
            sensor = FeedSensor()
            engine = core.Engine(sensor, base/'support', env=env, list_processes=lambda: None)
            sensor.put({'type': 'hello', 'contract': 1, 'sensor': 'macos', 'version': '0.3.0', 'coords': 'macos-points',
                        'capabilities': ['drag', 'click', 'window', 'clients']})
            running = threading.Event()
            try:
                self.assertTrue(wait(lambda: api('workspace.list').get('workspaces')), 'isolated server did not start')

                def clients():
                    """Local herdr clients below this test, as the real sensor reports them."""
                    out = subprocess.run([str(sensor_binary), '--clients-below', str(os.getpid())],
                                         capture_output=True, text=True, timeout=10).stdout
                    return [c for c in json.loads(out)['clients'] if len(c['argv']) == 1]

                listed = wait(lambda: [c for c in clients() if c['pid'] == first.pid])
                self.assertTrue(listed, 'the sensor did not find the herdr client')
                client = listed[0]
                self.assertEqual((client['rows'], client['cols']), (ROWS, COLS))
                self.assertEqual(client['env']['HOME'], temp)

                def window(described):
                    return {'id': '1', 'pid': 1, 'app': 'isolated.terminal', 'bounds': dict(WINDOW), 'scale': 1.0,
                            'peers': [], 'clients': described}

                def point(rect, fy=0.5):
                    return (WINDOW['x'] + LEFT + (rect.x + rect.width / 2) * CELL_W,
                            WINDOW['y'] + TITLEBAR + (rect.y + rect.height * fy) * CELL_H)

                # Session: "project" (tabs 1 and 2, 1 focused) and "assets" (tab 1, and "files" with two panes).
                project = snap()['focused_workspace_id']
                api('workspace.rename', workspace_id=project, label='project')
                project_2 = api('tab.create', workspace_id=project, label='2', focus=False)['tab']['tab_id']
                assets = api('workspace.create', cwd=temp, label='assets', focus=False)['workspace']['workspace_id']
                files = api('tab.create', workspace_id=assets, label='files', cwd=temp, focus=False)
                files_tab, files_root = files['tab']['tab_id'], files['root_pane']['pane_id']
                destination = api('pane.split', target_pane_id=files_root, direction='right', focus=False)['pane']['pane_id']
                api('pane.focus', pane_id=files_root)
                api('workspace.focus', workspace_id=project)
                self.assertTrue(wait(lambda: snap()['focused_workspace_id'] == project and model().find('tab', project_2)),
                                'replica does not place tab 2')

                runner = threading.Thread(target=lambda: engine.run(until=running.is_set), daemon=True)
                runner.start()
                self.assertTrue(wait(lambda: sock in map(str, engine.subscriptions)), 'core did not subscribe early')
                time.sleep(0.3)

                # 1. An ordinary click on tab "2": herdr gets the mouse, the sensor reports the click after it.
                tab = model().find('tab', project_2)
                column = tab.rect.x + 2
                first.write(f'\x1b[<0;{column + 1};1M\x1b[<0;{column + 1};1m'.encode())
                self.assertTrue(wait(lambda: snap()['focused_tab_id'] == project_2, 3), 'herdr did not take the click')
                x = WINDOW['x'] + LEFT + (column + 0.5) * CELL_W
                y = WINDOW['y'] + TITLEBAR + CELL_H - 1        # low in row 0: one click pins the row
                sensor.put({'type': 'click', 'x': x, 'y': y, 'window': window([client])})
                self.assertTrue(wait(lambda: engine.status.get('calibrated'), 3), f'no calibration: {engine.status}')

                # 2. One held drag: Space "assets", then tab "files", then the pane that is not focused.
                def hold(target, check, seconds=3.0):
                    until = time.time() + seconds
                    while time.time() < until:
                        sensor.put({'type': 'drag_move', 'x': target[0], 'y': target[1], 'window': window([client])})
                        if check():
                            return True
                        time.sleep(0.05)
                    return False

                sensor.put({'type': 'drag_start', 'x': 0, 'y': 0, 'kind': 'files', 'window': None, 'all_clients': clients()})
                space = model().find('space', assets)
                self.assertTrue(hold(point(space.rect), lambda: snap()['focused_workspace_id'] == assets),
                                f'space: {engine.status}')
                files_rect = wait(lambda: model().find('tab', files_tab))
                self.assertTrue(hold(point(files_rect.rect), lambda: snap()['focused_tab_id'] == files_tab),
                                f'tab: {engine.status}')
                pane = wait(lambda: model().find('pane', destination))
                if pane is None:
                    current = snap()
                    self.fail(f'replica has no pane {destination}: refused={model().refused} '
                              f'panes={[(p["pane_id"], p.get("tab_id")) for p in current["panes"]]} '
                              f'layouts={[l for l in current["layouts"] if l["tab_id"] == files_tab]}')
                self.assertTrue(hold(point(pane.rect), lambda: snap()['focused_pane_id'] == destination),
                                f'pane: {engine.status}')
                sensor.put({'type': 'drag_end', 'x': 0, 'y': 0, 'reason': 'released'})

                # 3. Passive: the terminal's own drop (a bracketed paste of the path) reaches that pane.
                image = base/'photo.png'
                image.write_bytes(b'fake image')
                first.write(b'\x1b[200~' + str(image).encode() + b'\x1b[201~')
                self.assertTrue(wait(lambda: str(image) in api('pane.read', pane_id=destination, source='recent_unwrapped',
                                                                lines=10)['read']['text']),
                                'the native drop did not reach the focused pane')
                print('\nisolated herdr 0.9.3: click calibration -> space -> tab -> pane (API) -> native drop PASS')

                # 4. A second client attaches: herdr's focus calls would move both, so nothing happens.
                second = Client(binary, env, temp)
                both = wait(lambda: (lambda found: found if len(found) == 2 else None)(clients()))
                self.assertTrue(both, 'second client not found')
                time.sleep(1.2)   # past the core's after-drag window
                before = snap()['focused_workspace_id']
                sensor.put({'type': 'drag_start', 'x': 0, 'y': 0, 'kind': 'files', 'window': None, 'all_clients': both})
                back = model().find('space', project)
                changed = hold(point(back.rect), lambda: snap()['focused_workspace_id'] != before, 2.0)
                sensor.put({'type': 'drag_end', 'x': 0, 'y': 0, 'reason': 'released'})
                self.assertFalse(changed, 'two clients: the core must not act')
                self.assertIn('2 herdr clients are attached', engine.status['reason'] or '')
                print('isolated herdr 0.9.3: two clients -> nothing happens PASS')
            finally:
                running.set()
                if runner:
                    runner.join(3)
                try:
                    api('server.stop')
                except (OSError, ValueError, herdr_api.ApiError):
                    pass
                for c in (first, second):
                    if c:
                        c.close()

if __name__ == '__main__':
    unittest.main()
