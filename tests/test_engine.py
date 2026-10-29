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
        for conn in list(self.subscribers):
            try:
                conn.sendall(line)
            except OSError:
                self.subscribers.remove(conn)
                conn.close()

    def user_focus(self, method, **params):
        """What a user's click does in herdr: change focus, then emit all three
        focus events (herdr's emit_focus_api_events), whatever level changed."""
        with self.lock:
            self.apply(method, params)
            s = self.snapshot
        self.publish('workspace_focused', workspace_id=s['focused_workspace_id'])
        self.publish('tab_focused', tab_id=s['focused_tab_id'], workspace_id=s['focused_workspace_id'])
        self.publish('pane_focused', pane_id=s['focused_pane_id'], workspace_id=s['focused_workspace_id'])

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
        self.client_started = time.time() + 60   # the client started after config.toml was written

    def client(self, pid=4242, argv=('herdr',), env=None):
        return {'pid': pid, 'argv': list(argv), 'tty': f'/dev/ttys{pid % 1000:03d}', 'rows': 40, 'cols': 120,
                'xpixel': 120 * CELL_W, 'ypixel': 40 * CELL_H, 'foreground': True, 'env': env if env is not None else self.env,
                'started': self.client_started}

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
        # herdr focuses on mouse up, before the sensor reports the click (review H2),
        # and sends workspace, tab and pane events though only the tab changed (H1).
        steps = [(0.15, lambda: self.server.user_focus('tab.focus', tab_id=first.id)),
                 (0.2, {'type': 'click', 'x': cx, 'y': cy, 'window': self.window()})]
        self.server.calls.clear()
        engine = self.run_engine(steps + self.drag(1.5, [(tab_point, 1.0)]))
        self.assertEqual(engine.status['calibrated'], 1)
        self.assertEqual(self.server.focus_calls(), [('tab.focus', {'tab_id': files['tab_id']})])
        stored = json.loads((self.temp/'support/calibration.json').read_text())
        low, high = list(stored.values())[0]['origin'][2:]
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
        # Each click moves away from "assets" first (by keyboard, say), then clicks it again.
        project_ws = project['workspace_id']
        steps = [(0.1, lambda: self.server.user_focus('workspace.focus', workspace_id=project_ws)),
                 (0.25, lambda: self.server.user_focus('workspace.focus', workspace_id=assets['workspace_id'])),
                 (0.3, {'type': 'click', 'x': x0, 'y': top, 'window': self.window()}),
                 (1.5, lambda: self.server.user_focus('workspace.focus', workspace_id=project_ws)),
                 (2.2, {'type': 'click', 'x': x0, 'y': top + CELL_H - 4, 'window': self.window()}),
                 (2.25, lambda: self.server.user_focus('workspace.focus', workspace_id=assets['workspace_id']))]
        self.run_engine(steps)
        self.server.calls.clear()
        low, high = list(json.loads((self.temp/'support/calibration.json').read_text()).values())[0]['origin'][2:]
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

    def tab_setup(self):
        model = render.build_model(120, 40, self.server.snapshot, render.ui_from_config({'ui': {}}), render.Preferences())
        tab = lambda label: next(t['tab_id'] for t in self.server.snapshot['tabs'] if t['label'] == label)
        files = model.find('tab', tab('a much longer tab name'))
        first = model.find('tab', tab('1'))
        tab_point = (self.point(files.rect.x + files.rect.width // 2, 0)[0], self.window_xy[1] + TITLEBAR + 3)
        click = self.point(first.rect.x + 2, 0)
        return files, first, tab_point, click

    def test_click_attribution_uses_the_level_that_changed(self):
        # Review H1: herdr emits all three events; the shallowest *changed* level counts.
        engine = core.Engine(ScriptedSensor([]), self.temp/'support', env=self.env, clock=lambda: 10.0)
        engine.focus['s'] = {'space': 'w1', 'tab': 't1', 'pane': 'p1'}
        def burst(at, ws, tab, pane):
            for kind, value in (('workspace_focused', {'workspace_id': ws}), ('tab_focused', {'tab_id': tab}),
                                ('pane_focused', {'pane_id': pane})):
                engine.events.put((at, 's', {'event': kind, 'data': value}))
            engine.drain_herdr_events()
        burst(9.0, 'w1', 't2', 'p2')
        self.assertEqual(engine.attribute({'at': 9.1, 'socket': 's'}), ('tab', 't2'))
        burst(9.0, 'w1', 't2', 'p3')
        self.assertEqual(engine.attribute({'at': 9.1, 'socket': 's'}), ('pane', 'p3'))
        burst(9.0, 'w2', 't9', 'p9')
        self.assertEqual(engine.attribute({'at': 9.1, 'socket': 's'}), ('space', 'w2'))
        burst(9.0, 'w2', 't9', 'p8')
        burst(9.5, 'w2', 't9', 'p7')
        self.assertIsNone(engine.attribute({'at': 9.3, 'socket': 's'}), 'two focus changes near one click')
        self.assertIsNone(engine.attribute({'at': 5.0, 'socket': 's'}), 'nothing near')

    def test_focus_event_after_the_click_also_calibrates(self):
        # Review H2: both orders.
        files, first, tab_point, (cx, cy) = self.tab_setup()
        steps = [(0.2, {'type': 'click', 'x': cx, 'y': cy, 'window': self.window()}),
                 (0.3, lambda: self.server.user_focus('tab.focus', tab_id=first.id))]
        engine = self.run_engine(steps + self.drag(1.5, [(tab_point, 1.0)]))
        self.assertEqual(engine.status['calibrated'], 1)
        self.assertIn(('tab.focus', {'tab_id': files.id}), self.server.focus_calls())

    def test_config_newer_than_the_client_is_refused_until_a_click_confirms(self):
        # Review H3: the client may still show the config it started with.
        self.client_started = time.time() - 3600
        target = self.other_pane()
        engine = self.run_engine(self.drag(0.5, [(self.pane_point(target), 0.8)]))
        self.assertEqual(self.server.focus_calls(), [])
        self.assertIn("config changed after this herdr client started", engine.status['reason'])
        files, first, tab_point, (cx, cy) = self.tab_setup()
        steps = [(0.15, lambda: self.server.user_focus('tab.focus', tab_id=first.id)),
                 (0.2, {'type': 'click', 'x': cx, 'y': cy, 'window': self.window()})]
        self.server.calls.clear()
        self.run_engine(steps + self.drag(1.5, [(tab_point, 1.0)]))
        self.assertEqual(self.server.focus_calls(), [('tab.focus', {'tab_id': files.id})])

    def test_click_where_the_replica_draws_nothing_turns_the_window_off(self):
        # Review H3: herdr focused a tab, but the click was far below the tab bar.
        files, first, tab_point, _ = self.tab_setup()
        cx, cy = self.point(first.rect.x + 2, 30)
        steps = [(0.15, lambda: self.server.user_focus('tab.focus', tab_id=first.id)),
                 (0.2, {'type': 'click', 'x': cx, 'y': cy, 'window': self.window()})]
        self.server.calls.clear()
        engine = self.run_engine(steps + self.drag(1.5, [(self.pane_point(self.other_pane()), 0.8)]))
        self.assertEqual(self.server.focus_calls(), [])
        self.assertIn('does not draw it', engine.status['reason'])

    def test_changed_identity_cwd_unsettles_the_branch_row(self):
        # Review M2
        engine = core.Engine(ScriptedSensor([]), self.temp/'support', env=self.env)
        self.assertTrue(engine.branch_settled('s', 'w1', str(self.temp)))
        self.assertTrue(engine.branch_settled('s', 'w1', str(self.temp)))
        self.assertFalse(engine.branch_settled('s', 'w1', str(self.temp/'c')), 'the cwd just changed')
        repo = self.temp/'repo'
        (repo/'.git').mkdir(parents=True)
        (repo/'.git/HEAD').write_text('ref: refs/heads/main\n')
        self.assertFalse(engine.branch_settled('s', 'w2', str(repo)), 'HEAD just moved')
        os.utime(repo/'.git/HEAD', (time.time() - 60, time.time() - 60))
        self.assertTrue(engine.branch_settled('s', 'w2', str(repo)))

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

    def seen_registered(self, manifest):
        (self.temp/'support').mkdir(exist_ok=True)
        (self.temp/'support/registered-seen').write_text(json.dumps({'manifest': str(manifest)}))
        (self.config/'plugins.json').write_text('[]')

    def two_drags(self):
        point = self.pane_point(self.other_pane())
        return self.drag(0.5, [(point, 0.3)]) + self.drag(4.5, [(point, 0.3)])

    def test_uninstalled_plugin_removes_the_helper(self):
        # Review M4: missing twice, 3 s apart, and the plugin folder is gone.
        self.seen_registered(self.temp/'gone/herdr-plugin.toml')
        calls = []
        engine = self.run_engine(self.two_drags(), uninstall=lambda: calls.append(1) or 0)
        self.assertEqual(calls, [1])
        self.assertTrue(engine.removed)
        self.assertEqual(self.server.focus_calls(), [])

    def test_transient_missing_registration_keeps_the_helper(self):
        self.seen_registered(self.temp/'gone/herdr-plugin.toml')
        calls = []
        engine = self.run_engine(self.drag(0.5, [(self.pane_point(self.other_pane()), 0.8)]),
                                 uninstall=lambda: calls.append(1) or 0)
        self.assertEqual(calls, [], 'one missing reading is not enough')
        self.assertEqual(engine.status['state'], 'paused')

    def test_missing_registration_with_plugin_folder_present_keeps_the_helper(self):
        self.seen_registered(ROOT/'herdr-plugin.toml')   # an update rewriting plugins.json
        calls = []
        self.run_engine(self.two_drags(), uninstall=lambda: calls.append(1) or 0)
        self.assertEqual(calls, [])

    def test_registration_records_the_manifest(self):
        self.run_engine(self.drag(0.5, [(self.pane_point(self.other_pane()), 0.5)]))
        seen = json.loads((self.temp/'support/registered-seen').read_text())
        self.assertEqual(seen['manifest'], str(ROOT/'herdr-plugin.toml'))

    def test_not_yet_registered_only_pauses(self):
        (self.config/'plugins.json').write_text('[]')
        calls = []
        engine = self.run_engine(self.drag(0.5, [(self.pane_point(self.other_pane()), 0.8)]), uninstall=lambda: calls.append(1) or 0)
        self.assertEqual(calls, [])
        self.assertEqual(engine.status['state'], 'paused')

