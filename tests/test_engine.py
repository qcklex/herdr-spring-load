"""The core loop with a scripted sensor and a fake herdr API socket serving a
golden snapshot (tests/golden/spaces_tabs.json: 120x40, sidebar 26, tab bar on
top, two panes in the focused tab)."""
import copy
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from spring_load import contract, engine as core, geometry, render

GOLDEN_TEMP = tempfile.mkdtemp(prefix='sl-eg.')
GOLDEN = json.loads((ROOT/'tests/golden/spaces_tabs.json').read_text().replace('$TEMP', GOLDEN_TEMP))
CELL_W, CELL_H, TITLEBAR = 8, 20, 28

class FakeHerdr:
    """session.snapshot, the three focus calls and events.subscribe on a Unix socket."""
    def __init__(self, path, snapshot):
        self.path = str(path)
        self.snapshot = copy.deepcopy(snapshot)
        self.calls = []
        self.subscribers = []
        self.lock = threading.Lock()
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(self.path)
        self.sock.listen(8)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        data = b''
        while b'\n' not in data:
            chunk = conn.recv(65536)
            if not chunk:
                conn.close()
                return
            data += chunk
        request = json.loads(data.split(b'\n')[0])
        method, params = request['method'], request.get('params', {})
        with self.lock:
            self.calls.append((method, params))
        if method == 'events.subscribe':
            conn.sendall(b'{"id":"e","result":{"type":"subscription_started"}}\n')
            self.subscribers.append(conn)
            return
        if method == 'session.snapshot':
            result = {'type': 'session_snapshot', 'snapshot': self.snapshot}
        elif method in ('workspace.focus', 'tab.focus', 'pane.focus'):
            self.apply(method, params)
            result = {'type': 'ok'}
        else:
            conn.sendall(json.dumps({'id': request['id'], 'error': {'code': 'unknown'}}).encode() + b'\n')
            conn.close()
            return
        conn.sendall(json.dumps({'id': request['id'], 'result': result}).encode() + b'\n')
        conn.close()

    def apply(self, method, params):
        s = self.snapshot
        if method == 'pane.focus':
            pane = next(p for p in s['panes'] if p['pane_id'] == params['pane_id'])
            s['focused_pane_id'], s['focused_tab_id'], s['focused_workspace_id'] = pane['pane_id'], pane['tab_id'], pane['workspace_id']
        elif method == 'tab.focus':
            tab = next(t for t in s['tabs'] if t['tab_id'] == params['tab_id'])
            s['focused_tab_id'], s['focused_workspace_id'] = tab['tab_id'], tab['workspace_id']
            s['focused_pane_id'] = next(p['pane_id'] for p in s['panes'] if p['tab_id'] == tab['tab_id'])
        else:
            workspace = next(w for w in s['workspaces'] if w['workspace_id'] == params['workspace_id'])
            s['focused_workspace_id'], s['focused_tab_id'] = workspace['workspace_id'], workspace['active_tab_id']
            s['focused_pane_id'] = next(p['pane_id'] for p in s['panes'] if p['tab_id'] == workspace['active_tab_id'])

    def publish(self, event, **data):
        line = json.dumps({'event': event, 'data': {'type': event, **data}}).encode() + b'\n'
        for conn in self.subscribers:
            conn.sendall(line)

    def focus_calls(self):
        with self.lock:
            return [(m, p) for m, p in self.calls if m.endswith('.focus')]

    def close(self):
        self.sock.close()
        for conn in self.subscribers:
            conn.close()

class ScriptedSensor:
    """Feeds validated events at scripted times on a fake clock."""
    def __init__(self, steps):
        self.now = 0.0
        self.steps = sorted(steps, key=lambda s: s[0])
        self.hello = contract.validate_hello({'type': 'hello', 't': 0, 'contract': 1, 'sensor': 'scripted', 'version': 't',
                                              'coords': 'macos-points', 'capabilities': ['drag', 'click', 'window', 'clients']})
        self.sent_hello = False

    def clock(self):
        return self.now

    def next(self, timeout=None):
        if not self.sent_hello:
            self.sent_hello = True
            return self.hello
        time.sleep(0.002)   # lets the fake server's event threads run
        if self.steps and self.steps[0][0] <= self.now + (timeout or 0):
            at, item = self.steps.pop(0)
            self.now = max(self.now, at)
            if callable(item):
                item()
                return None
            item.setdefault('t', at)
            return contract.validate_event(item)
        self.now += timeout or 0
        return None

    def done(self):
        return not self.steps and self.now > 3.0

class EngineTest(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='sl-e.', dir='/private/tmp'))
        self.addCleanup(lambda: __import__('shutil').rmtree(self.temp, True))
        self.config = self.temp/'c/herdr'
        self.config.mkdir(parents=True)
        (self.config/'config.toml').write_text(GOLDEN['config'])
        (self.config/'plugins.json').write_text(json.dumps([{'plugin_id': 'herdr-spring-load', 'enabled': True,
                                                             'manifest_path': str(ROOT/'herdr-plugin.toml')}]))
        self.env = {'HOME': str(self.temp), 'XDG_CONFIG_HOME': str(self.temp/'c'), 'XDG_STATE_HOME': str(self.temp/'s')}
        self.server = FakeHerdr(self.config/'herdr.sock', GOLDEN['snapshot'])
        self.addCleanup(self.server.close)
        self.window_xy = (100.0, 50.0)

    def client(self, pid=4242, argv=('herdr',), env=None):
        return {'pid': pid, 'argv': list(argv), 'tty': f'/dev/ttys{pid % 1000:03d}', 'rows': 40, 'cols': 120,
                'xpixel': 120 * CELL_W, 'ypixel': 40 * CELL_H, 'foreground': True, 'env': env if env is not None else self.env}

    def window(self, clients=None, peers=()):
        x, y = self.window_xy
        return {'id': '77', 'pid': 1, 'app': 'test.terminal',
                'bounds': {'x': x, 'y': y, 'width': 120 * CELL_W + 2, 'height': 40 * CELL_H + TITLEBAR},
                'scale': 2.0, 'peers': list(peers), 'clients': clients if clients is not None else [self.client()]}

    def point(self, column, row, offset_y=TITLEBAR):
        """Screen point of a cell centre, assuming the grid sits under a titlebar."""
        x, y = self.window_xy
        return x + (column + 0.5) * CELL_W, y + offset_y + (row + 0.5) * CELL_H

    def drag(self, start, points, everyone=None, window=None):
        window = window or self.window()
        steps = [(start, {'type': 'drag_start', 'x': 0, 'y': 0, 'kind': 'files', 'window': None,
                          'all_clients': everyone if everyone is not None else window['clients']})]
        t = start
        for (x, y), hold in points:
            steps.append((t, {'type': 'drag_move', 'x': x, 'y': y, 'window': window}))
            for i in range(1, int(hold / 0.1) + 1):
                steps.append((t + i * 0.1, {'type': 'drag_move', 'x': x, 'y': y, 'window': window}))
            t += hold
        steps.append((t + 0.05, {'type': 'drag_end', 'x': 0, 'y': 0, 'reason': 'released'}))
        return steps

    def run_engine(self, steps, uninstall=None):
        sensor = ScriptedSensor(steps)
        engine = core.Engine(sensor, self.temp/'support', env=self.env, clock=sensor.clock,
                             list_processes=lambda: None, uninstall=uninstall)
        engine.run(until=sensor.done)
        return engine

    def other_pane(self):
        s = self.server.snapshot
        return next(p['pane_id'] for p in s['panes'] if p['tab_id'] == s['focused_tab_id'] and p['pane_id'] != s['focused_pane_id'])

    def pane_point(self, pane_id):
        layout = next(l for l in self.server.snapshot['layouts'] if l['tab_id'] == self.server.snapshot['focused_tab_id'])
        rect = next(p['rect'] for p in layout['panes'] if p['pane_id'] == pane_id)
        return self.point(26 + rect['x'] + rect['width'] // 2, 1 + rect['y'] + rect['height'] // 2)

    def test_hovering_a_pane_focuses_it_through_the_api(self):
        target = self.other_pane()
        engine = self.run_engine(self.drag(0.5, [(self.pane_point(target), 0.5)]))
        self.assertEqual(self.server.focus_calls(), [('pane.focus', {'pane_id': target})], engine.status)

    def test_short_hover_does_nothing(self):
        self.run_engine(self.drag(0.5, [(self.pane_point(self.other_pane()), 0.15)]))
        self.assertEqual(self.server.focus_calls(), [])

    def test_two_attached_clients_do_nothing(self):
        window = self.window()
        everyone = [self.client(), self.client(pid=5151)]
        engine = self.run_engine(self.drag(0.5, [(self.pane_point(self.other_pane()), 0.8)], everyone=everyone, window=window))
        self.assertEqual(self.server.focus_calls(), [])
        self.assertIn('2 herdr clients are attached', engine.status['reason'])

    def test_client_of_another_session_does_not_count(self):
        everyone = [self.client(), self.client(pid=5151, argv=('herdr', '--session', 'work')), self.client(pid=6161, argv=('herdr', '--remote', 'box'))]
        target = self.other_pane()
        self.run_engine(self.drag(0.5, [(self.pane_point(target), 0.5)], everyone=everyone))
        self.assertEqual(self.server.focus_calls(), [('pane.focus', {'pane_id': target})])

    def test_same_size_window_of_the_same_app_is_ambiguous(self):
        window = self.window(peers=[{'x': 0, 'y': 0, 'width': 120 * CELL_W + 2, 'height': 40 * CELL_H + TITLEBAR}])
        engine = self.run_engine(self.drag(0.5, [(self.pane_point(self.other_pane()), 0.8)], window=window))
        self.assertEqual(self.server.focus_calls(), [])
        self.assertIn('same size', engine.status['reason'])

    def test_window_of_another_size_is_not_bound(self):
        window = self.window()
        window['bounds']['width'] = 400
        self.run_engine(self.drag(0.5, [(self.pane_point(self.other_pane()), 0.8)], window=window))
        self.assertEqual(self.server.focus_calls(), [])

    def test_tabs_need_calibration_then_spring_load(self):
        files = next(t for t in self.server.snapshot['tabs'] if t['label'] == 'a much longer tab name')
        model = render.build_model(120, 40, self.server.snapshot, render.ui_from_config({'ui': {}}), render.Preferences())
        rect = model.find('tab', files['tab_id']).rect
        # Upper part of the tab row: one calibrating click leaves under half a row of doubt.
        tab_point = (self.point(rect.x + rect.width // 2, 0)[0], self.window_xy[1] + TITLEBAR + 3)
        # Uncalibrated: the titlebar makes row 0 uncertain, so the tab never fires.
        self.run_engine(self.drag(0.5, [(tab_point, 1.0)]))
        self.assertEqual(self.server.focus_calls(), [])
        # The user clicks the first tab normally; herdr reports the focus; the core learns.
        first = model.find('tab', next(t['tab_id'] for t in self.server.snapshot['tabs'] if t['label'] == '1'))
        cx, cy = self.point(first.rect.x + 2, 0)
        steps = [(0.2, {'type': 'click', 'x': cx, 'y': cy, 'window': self.window()}),
                 (0.25, lambda: self.server.publish('tab_focused', tab_id=first.id, workspace_id='w1')),
                 (0.26, lambda: self.server.publish('pane_focused', pane_id='w1:p1', workspace_id='w1'))]
        engine = self.run_engine(steps + self.drag(1.5, [(tab_point, 1.0)]))
        self.assertEqual(engine.status['calibrated'], 1)
        self.assertEqual(self.server.focus_calls(), [('tab.focus', {'tab_id': files['tab_id']})])
        stored = json.loads((self.temp/'support/calibration.json').read_text())
        low, high = list(stored.values())[0][2:]
        self.assertLessEqual(low, TITLEBAR)
        self.assertGreaterEqual(high, TITLEBAR)
        self.assertLessEqual(high - low, CELL_H / 2)

    def test_space_then_tab_then_pane_in_one_drag(self):
        """The README flow: one held drag opens a Space, then a tab, then a pane."""
        s = self.server.snapshot
        ws = {w['label']: w for w in s['workspaces']}
        tabs = {t['label']: t for t in s['tabs']}
        project, assets = ws['project'], ws['assets']
        project['active_tab_id'] = tabs['1']['tab_id']
        # herdr is showing "assets" when the drag starts.
        s['focused_workspace_id'], s['focused_tab_id'] = assets['workspace_id'], assets['active_tab_id']
        s['focused_pane_id'] = next(p['pane_id'] for p in s['panes'] if p['tab_id'] == assets['active_tab_id'])
        model = render.build_model(120, 40, s, render.ui_from_config({'ui': {}}), render.Preferences())
        row_of = lambda w: model.find('space', w['workspace_id']).rect.y
        # Two ordinary clicks on the "assets" row (top and bottom of the row) calibrate the titlebar.
        x0 = self.window_xy[0] + 5 * CELL_W
        top = self.window_xy[1] + TITLEBAR + row_of(assets) * CELL_H + 2
        steps = [(0.2, {'type': 'click', 'x': x0, 'y': top, 'window': self.window()}),
                 (0.25, lambda: self.server.publish('workspace_focused', workspace_id=assets['workspace_id'])),
                 (1.0, {'type': 'click', 'x': x0, 'y': top + CELL_H - 4, 'window': self.window()}),
                 (1.05, lambda: self.server.publish('workspace_focused', workspace_id=assets['workspace_id']))]
        engine = self.run_engine(steps)
        low, high = list(json.loads((self.temp/'support/calibration.json').read_text()).values())[0][2:]
        self.assertLessEqual(high - low, 3)
        # Pointer path: the "project" row, then the "files" tab, then the pane that is not focused there.
        def centre(rect):
            return (self.window_xy[0] + (rect.x + rect.width / 2) * CELL_W,
                    self.window_xy[1] + TITLEBAR + (rect.y + 0.5) * CELL_H)
        space_point = centre(model.find('space', project['workspace_id']).rect)
        s_after = copy.deepcopy(s)
        s_after['focused_workspace_id'] = project['workspace_id']
        tab_model = render.build_model(120, 40, s_after, render.ui_from_config({'ui': {}}), render.Preferences())
        tab_point = centre(tab_model.find('tab', tabs['files']['tab_id']).rect)
        files_panes = [p['pane_id'] for p in s['panes'] if p['tab_id'] == tabs['files']['tab_id']]
        layout = next(l for l in s['layouts'] if l['tab_id'] == tabs['files']['tab_id'])
        rect = next(p['rect'] for p in layout['panes'] if p['pane_id'] == files_panes[1])
        pane_point = self.point(26 + rect['x'] + rect['width'] // 2, 1 + rect['y'] + rect['height'] // 2)
        self.run_engine(self.drag(0.5, [(space_point, 0.9), (tab_point, 0.9), (pane_point, 0.6)]))
        self.assertEqual(self.server.focus_calls(), [('workspace.focus', {'workspace_id': project['workspace_id']}),
                                                     ('tab.focus', {'tab_id': tabs['files']['tab_id']}),
                                                     ('pane.focus', {'pane_id': files_panes[1]})])

    def test_unknown_herdr_version_is_inert(self):
        self.server.snapshot['version'] = '0.10.0'
        engine = self.run_engine(self.drag(0.5, [(self.pane_point(self.other_pane()), 0.8)]))
        self.assertEqual(self.server.focus_calls(), [])
        self.assertIn('0.10.0', json.dumps(engine.status['refused']))

    def test_disabled_plugin_pauses(self):
        (self.config/'plugins.json').write_text(json.dumps([{'plugin_id': 'herdr-spring-load', 'enabled': False,
                                                             'manifest_path': str(ROOT/'herdr-plugin.toml')}]))
        engine = self.run_engine(self.drag(0.5, [(self.pane_point(self.other_pane()), 0.8)]))
        self.assertEqual(self.server.focus_calls(), [])
        self.assertEqual(engine.status['state'], 'paused')

    def test_uninstalled_plugin_removes_the_helper(self):
        (self.temp/'support').mkdir()
        (self.temp/'support/registered-seen').touch()
        (self.config/'plugins.json').write_text('[]')
        calls = []
        engine = self.run_engine(self.drag(0.5, [(self.pane_point(self.other_pane()), 0.8)]), uninstall=lambda: calls.append(1) or 0)
        self.assertEqual(calls, [1])
        self.assertTrue(engine.removed)
        self.assertEqual(self.server.focus_calls(), [])

    def test_not_yet_registered_only_pauses(self):
        (self.config/'plugins.json').write_text('[]')
        calls = []
        engine = self.run_engine(self.drag(0.5, [(self.pane_point(self.other_pane()), 0.8)]), uninstall=lambda: calls.append(1) or 0)
        self.assertEqual(calls, [])
        self.assertEqual(engine.status['state'], 'paused')

class GeometryTest(unittest.TestCase):
    def test_fit_picks_the_unit_and_refuses_ambiguity(self):
        client = {'cols': 239, 'rows': 68, 'xpixel': 1916, 'ypixel': 1160}
        grid = geometry.fit((1920, 1164), client, 2.0)
        self.assertAlmostEqual(grid.cell_width, 1916 / 239)
        self.assertEqual((grid.origin_x.high, grid.origin_y.high), (4, 4))
        self.assertIsNone(geometry.fit((1000, 1164), client, 2.0), 'grid wider than the window')
        self.assertIsNone(geometry.fit((1920, 1164), {**client, 'xpixel': 0}, 2.0), 'no pixel size')
        retina = geometry.fit((960, 600), {'cols': 100, 'rows': 30, 'xpixel': 1904, 'ypixel': 1080}, 2.0)
        self.assertAlmostEqual(retina.cell_width, 9.52)

    def test_resolve_needs_agreement(self):
        model = render.Model(10, 10, [render.Target('pane', 'a', render.Rect(0, 1, 5, 9)), render.Target('pane', 'b', render.Rect(5, 1, 5, 9)),
                                      render.Target('tab', 't', render.Rect(0, 0, 10, 1))])
        grid = geometry.Grid(10, 10, 10, 10, geometry.Interval(0, 5), geometry.Interval(0, 15))
        self.assertEqual(geometry.resolve(model, grid, 25, 60).id, 'a')
        self.assertIsNone(geometry.resolve(model, grid, 52, 60), 'straddles two panes')
        self.assertIsNone(geometry.resolve(model, grid, 25, 12), 'row 0 or row 1')
        calibrated = grid.narrowed(geometry.Interval(0, 0), geometry.Interval(2, 3))
        self.assertEqual(geometry.resolve(model, calibrated, 25, 9).id, 't')
