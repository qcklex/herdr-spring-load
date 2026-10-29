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
sys.path.insert(0, str(ROOT/'tests'))
from golden_fixture import load_golden

GOLDEN_TEMP = tempfile.mkdtemp(prefix='sl-eg.')
GOLDEN = load_golden(ROOT/'tests/golden/spaces_tabs.json', GOLDEN_TEMP)
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

class TextEncodingTest(unittest.TestCase):
    def test_core_reads_text_files_as_utf8(self):
        # Windows CI: read_text() without an encoding uses the ANSI code page (cp1252)
        # there, which garbles a non-ASCII herdr config (separators, labels) and so
        # the replica's widths. Every text read in the core names UTF-8.
        import re
        for path in sorted((ROOT/'spring_load').glob('*.py')):
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                for call in re.findall(r'read_text\(([^)]*)\)', line):
                    self.assertIn("encoding='utf-8'", call, f'{path.name}:{number}: {line.strip()}')

    def test_non_ascii_config_is_read_as_utf8(self):
        temp = Path(tempfile.mkdtemp(prefix='sl-u.'))
        self.addCleanup(lambda: __import__('shutil').rmtree(temp, True))
        config = temp/'config.toml'
        config.write_bytes('[ui]\ntab_bar_right_separator = " \u2502 "\n'.encode('utf-8'))
        engine = core.Engine(ScriptedSensor([]), temp/'support', env={'HOME': str(temp)})
        self.assertEqual(engine.config_ui(config).tab_bar_right_separator, ' \u2502 ')

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

    def run_engine(self, steps, uninstall=None, log=None):
        sensor = ScriptedSensor(steps)
        engine = core.Engine(sensor, self.temp/'support', env=self.env, clock=sensor.clock,
                             list_processes=lambda: None, uninstall=uninstall, log=log)
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

    def test_more_sessions_than_windows_does_nothing(self):
        # Review N10: herdr in a hidden tab of the hovered terminal fits the window as
        # well as a visible one. An app that runs more sessions than it shows windows
        # (tabs, splits) is refused, as the Windows sensor does.
        target = self.other_pane()
        window = {**self.window(), 'sessions': 2}
        engine = self.run_engine(self.drag(0.5, [(self.pane_point(target), 0.8)], window=window))
        self.assertEqual(self.server.focus_calls(), [])
        self.assertIn('run herdr alone in its window', engine.status['reason'])
        # Two sessions in two visible windows (one per window) are fine.
        other = {'x': 2000, 'y': 0, 'width': 500, 'height': 400}
        window = {**self.window(peers=[other]), 'sessions': 2}
        self.run_engine(self.drag(0.5, [(self.pane_point(target), 0.8)], window=window))
        self.assertEqual(self.server.focus_calls(), [('pane.focus', {'pane_id': target})])

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
        # Each click on "assets" follows a move away from it (by keyboard, say) well before.
        project_ws = project['workspace_id']
        steps = [(0.1, lambda: self.server.user_focus('workspace.focus', workspace_id=project_ws)),
                 (0.75, lambda: self.server.user_focus('workspace.focus', workspace_id=assets['workspace_id'])),
                 (0.8, {'type': 'click', 'x': x0, 'y': top, 'window': self.window()}),
                 (1.6, lambda: self.server.user_focus('workspace.focus', workspace_id=project_ws)),
                 (2.2, {'type': 'click', 'x': x0, 'y': top + CELL_H - 4, 'window': self.window()}),
                 (2.25, lambda: self.server.user_focus('workspace.focus', workspace_id=assets['workspace_id']))]
        lines = []
        self.run_engine(steps, log=lines.append)
        self.server.calls.clear()
        outcomes = [l.split('calibration ')[1] for l in lines if 'calibration ' in l]
        # Review N1: each click is attributed to the Space actually clicked. "assets" is the
        # last row, so no other Space lies in the first click's uncertainty: it counts at once.
        self.assertEqual(outcomes, ['learned', 'agrees'])
        self.assertTrue(all(f"space {assets['workspace_id']}" in l for l in lines if 'calibration ' in l))
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
        burst(9.0, 'w3', 't3', 'p30')
        burst(9.1, 'w4', 't4', 'p40')
        self.assertIsNone(engine.attribute({'at': 9.2, 'socket': 's'}), 'two Spaces in one burst (review N1)')
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

    def test_config_newer_than_the_client_is_refused_until_clicks_confirm_tabs(self):
        # Review H3: the client may still show the config it started with. A click
        # proves only its own kind, and only once an origin was learned before it.
        self.client_started = time.time() - 3600
        target = self.other_pane()
        engine = self.run_engine(self.drag(0.5, [(self.pane_point(target), 0.8)]))
        self.assertEqual(self.server.focus_calls(), [])
        self.assertIn("config changed after this herdr client started", engine.status['reason'])
        files, first, tab_point, (cx, cy) = self.tab_setup()
        s = self.server.snapshot
        ws = {w['label']: w for w in s['workspaces']}
        model = render.build_model(120, 40, s, render.ui_from_config({'ui': {}}), render.Preferences())
        space_rect = model.find('space', ws['assets']['workspace_id']).rect
        space_point = (self.window_xy[0] + 5 * CELL_W, self.window_xy[1] + TITLEBAR + (space_rect.y + 0.5) * CELL_H)
        def tab_clicks(at):
            return [(at, lambda: self.server.user_focus('tab.focus', tab_id=first.id)),
                    (at + 0.05, {'type': 'click', 'x': cx, 'y': cy, 'window': self.window()}),
                    (at + 0.8, lambda: self.server.user_focus('tab.focus', tab_id=files.id)),
                    (at + 1.5, lambda: self.server.user_focus('tab.focus', tab_id=first.id)),
                    (at + 1.55, {'type': 'click', 'x': cx, 'y': cy - 4, 'window': self.window()})]
        self.server.calls.clear()
        engine = self.run_engine(tab_clicks(0.1) + self.drag(3.0, [(space_point, 1.0), (tab_point, 1.0)]))
        self.assertEqual(self.server.focus_calls(), [('tab.focus', {'tab_id': files.id})],
                         'two tab clicks confirm tabs (and panes), not the Space rows')
        self.assertIn('Spaces stay off until the herdr client restarts', json.dumps(engine.status.get('refused')))
        # Review N9c: a click on a Space below the first, even one that contains the
        # stored origin, never confirms the Space rows.
        here, away = ws['assets']['workspace_id'], ws['project']['workspace_id']
        # Upper part of the "project" row: the calibrated origin leaves under half a row of doubt.
        project_point = (space_point[0], self.window_xy[1] + TITLEBAR + (space_rect.y - 1) * CELL_H + 4)
        steps = [(0.1, lambda: self.server.user_focus('workspace.focus', workspace_id=here)),
                 (0.15, {'type': 'click', 'x': space_point[0], 'y': space_point[1], 'window': self.window()})]
        self.server.calls.clear()
        lines = []
        engine = self.run_engine(steps + self.drag(1.5, [(project_point, 1.0)]), log=lines.append)
        self.assertEqual(self.server.focus_calls(), [], (lines, engine.status))
        self.assertEqual(lines[0], f'click on space {here}: config check, confirmed',
                         'the click is contained, and it does not move the origin (review N9)')
        self.assertTrue(all('space' not in kinds for _, kinds in engine.verified.values()), engine.verified)
        # A client started after the config was written shows it: Spaces work.
        self.client_started = time.time() + 60
        self.server.calls.clear()
        self.run_engine(self.drag(0.5, [(project_point, 1.0)]))
        self.assertEqual(self.server.focus_calls(), [('workspace.focus', {'workspace_id': away})])

    def test_stale_row_gap_is_not_confirmed_by_clicks_that_only_overlap(self):
        # Review N9, the reviewer's reproduction: calibrate, then set spaces.row_gap = 1
        # in the file without a reload. herdr still draws "assets" on row 3, the
        # replica now on row 4. Clicks in the lower part of herdr's row overlap the
        # stored origin; before the fix they returned 'agrees', narrowed the origin
        # half a row off and confirmed the Space rows.
        ws = {w['label']: w for w in self.server.snapshot['workspaces']}
        project, assets = ws['project']['workspace_id'], ws['assets']['workspace_id']
        x = self.window_xy[0] + 5 * CELL_W
        row_top = lambda row: self.window_xy[1] + TITLEBAR + row * CELL_H
        def click(at, target, y):
            return [(at, lambda: self.server.user_focus('workspace.focus', workspace_id=target)),
                    (at + 0.05, {'type': 'click', 'x': x, 'y': y, 'window': self.window()})]
        self.server.user_focus('workspace.focus', workspace_id=project)
        lines = []
        engine = self.run_engine(click(0.1, assets, row_top(3) + 2) + click(1.3, project, row_top(2) + 4), log=lines.append)
        self.assertEqual([l.split('calibration ')[1] for l in lines if 'calibration ' in l], ['learned', 'agrees'])
        key = next(iter(engine.calibration.data))
        before = engine.calibration.entry(key)

        self.client_started = time.time() - 3600
        (self.config/'config.toml').write_text(GOLDEN['config'] + '[ui.sidebar.spaces]\nrow_gap = 1\n')
        steps = click(0.1, assets, row_top(3) + 15) + click(1.3, project, row_top(2) + 15) \
            + click(2.5, assets, row_top(3) + 15)
        replica_assets = (x, row_top(4) + CELL_H / 2)
        lines = []
        self.server.calls.clear()
        engine = self.run_engine(steps + self.drag(3.5, [(replica_assets, 1.0)]), log=lines.append)
        self.assertEqual([l for l in lines if 'click on space' in l and 'agrees' in l], [], lines)
        self.assertIn(f'click on space {assets}: config check, not confirmed', lines)
        self.assertEqual(engine.calibration.entry(key), before, 'the stored origin did not move')
        self.assertEqual(engine.verified, {})
        self.assertEqual(self.server.focus_calls(), [])

    def test_stale_row_gap_with_two_row_spaces_needs_both_rows(self):
        # Review N9b: with a branch each Space is two rows high. Under a stale
        # spaces.row_gap = 1 the replica draws "assets" one row below herdr, so a
        # click in herdr's second "assets" row lies in the replica's first one and
        # its whole range contains the stored origin. Since N9c clicks never
        # confirm Space rows, under a stale config or the shown one.
        repo = self.temp/'repo'
        for folder in ('.git/objects', '.git/refs/heads'):
            (repo/folder).mkdir(parents=True)
        head = repo/'.git/HEAD'
        head.write_text('ref: refs/heads/main\n')
        os.utime(head, (time.time() - 3600, time.time() - 3600))
        for pane in self.server.snapshot['panes']:
            pane['cwd'] = str(repo)
        model = render.build_model(120, 40, self.server.snapshot, render.ui_from_config({'ui': {}}), render.Preferences())
        ws = {w['label']: w['workspace_id'] for w in self.server.snapshot['workspaces']}
        rect = model.find('space', ws['assets']).rect
        self.assertEqual((model.find('space', ws['project']).rect.y, rect.y, rect.height), (2, 4, 2), 'two-row Spaces')
        x = self.window_xy[0] + 5 * CELL_W
        row_top = lambda row: self.window_xy[1] + TITLEBAR + row * CELL_H
        def click(at, target, y):
            return [(at, lambda: self.server.user_focus('workspace.focus', workspace_id=ws[target])),
                    (at + 0.05, {'type': 'click', 'x': x, 'y': y, 'window': self.window()})]
        # Calibrate under a trusted config: the top of "assets", then the bottom of "project".
        self.server.user_focus('workspace.focus', workspace_id=ws['project'])
        lines = []
        engine = self.run_engine(click(0.1, 'assets', row_top(4) + 2) + click(1.3, 'project', row_top(3) + 17),
                                 log=lines.append)
        self.assertEqual([l.split('calibration ')[1] for l in lines if 'calibration ' in l][-1], 'agrees', lines)
        key = next(iter(engine.calibration.data))
        before = engine.calibration.entry(key)

        self.client_started = time.time() - 3600
        (self.config/'config.toml').write_text(GOLDEN['config'] + '[ui.sidebar.spaces]\nrow_gap = 1\n')
        # herdr's second "assets" row (5) is the replica's first: contained, but
        # only in the first row. herdr's first row (4) is the replica's gap.
        steps = click(0.1, 'assets', row_top(5) + 10) + click(1.3, 'project', row_top(2) + 10) \
            + click(2.5, 'assets', row_top(4) + 10) + click(3.7, 'project', row_top(2) + 10) \
            + click(4.9, 'assets', row_top(5) + 10)
        replica_assets = (x, row_top(6) + CELL_H / 2)
        lines = []
        self.server.calls.clear()
        engine = self.run_engine(steps + self.drag(6.0, [(replica_assets, 1.0)]), log=lines.append)
        self.assertIn(f'click on space {ws["assets"]}: config check, confirmed', lines, 'the reviewer\'s case')
        self.assertEqual(engine.calibration.entry(key), before, 'the stored origin did not move')
        self.assertTrue(all('space' not in kinds for _, kinds in engine.verified.values()), engine.verified)
        self.assertEqual(self.server.focus_calls(), [])

        # The same clicks under the config herdr shows still confirm nothing (N9c):
        # the plugin cannot tell that config from a stale one.
        (self.config/'config.toml').write_text(GOLDEN['config'] + '\n')
        self.server.user_focus('workspace.focus', workspace_id=ws['project'])
        steps = click(0.1, 'assets', row_top(4) + 10) + click(1.3, 'project', row_top(2) + 10) \
            + click(2.5, 'assets', row_top(5) + 10) + click(3.7, 'project', row_top(2) + 10)
        lines = []
        self.server.calls.clear()
        engine = self.run_engine(steps + self.drag(5.0, [((x, row_top(4) + CELL_H / 2), 1.0)]), log=lines.append)
        self.assertIn(f'click on space {ws["assets"]}: config check, confirmed', lines)
        self.assertEqual(self.server.focus_calls(), [])
        # Restarting the client (it now starts after the config was written) turns Spaces on.
        self.client_started = time.time() + 60
        self.server.calls.clear()
        self.run_engine(self.drag(0.5, [((x, row_top(4) + CELL_H / 2), 1.0)]))
        self.assertEqual(self.server.focus_calls(), [('workspace.focus', {'workspace_id': ws['assets']})])

    def test_stale_config_with_a_shorter_space_is_never_confirmed(self):
        # Review N9c: the first Space has no branch, the two below it do. The file
        # drops the branch row, herdr still draws it: herdr has "assets" on rows
        # 3-4 and "more" on 5-6, the replica "assets" on 3 and "more" on 4. A click
        # on "assets"'s name row lies in the replica's first and last row alike;
        # before the fix it confirmed the Space rows, and a hold on herdr's second
        # "assets" row then opened "more".
        s = self.server.snapshot
        s['workspaces'].append({**s['workspaces'][1], 'workspace_id': 'w3', 'number': 3, 'label': 'more',
                                'active_tab_id': 'w3:t1'})
        s['tabs'].append({**s['tabs'][-1], 'tab_id': 'w3:t1', 'workspace_id': 'w3'})
        s['panes'].append({**s['panes'][-1], 'pane_id': 'w3:p1', 'workspace_id': 'w3', 'tab_id': 'w3:t1',
                           'terminal_id': 'term_more'})
        s['layouts'].append({**copy.deepcopy(s['layouts'][-1]), 'workspace_id': 'w3', 'tab_id': 'w3:t1',
                             'focused_pane_id': 'w3:p1'})
        s['layouts'][-1]['panes'][0]['pane_id'] = 'w3:p1'
        repo, home = self.temp/'repo', self.temp/'home'
        home.mkdir()
        for folder in ('.git/objects', '.git/refs/heads'):
            (repo/folder).mkdir(parents=True)
        head = repo/'.git/HEAD'
        head.write_text('ref: refs/heads/main\n')
        os.utime(head, (time.time() - 3600, time.time() - 3600))
        for pane in s['panes']:
            pane['cwd'] = pane['foreground_cwd'] = str(home if pane['workspace_id'] == 'w1' else repo)
        stale = GOLDEN['config'] + '[ui.sidebar.spaces]\nrows = [["state_icon", "workspace"]]\n'
        rows = lambda config: {t.id: (t.rect.y, t.rect.height) for t in render.build_model(
            120, 40, s, render.ui_from_config(__import__('tomllib').loads(config)), render.Preferences()).targets
            if t.kind == 'space'}
        self.assertEqual(rows(GOLDEN['config']), {'w1': (2, 1), 'w2': (3, 2), 'w3': (5, 2)}, 'what herdr shows')
        self.assertEqual(rows(stale), {'w1': (2, 1), 'w2': (3, 1), 'w3': (4, 1)}, 'the stale replica')
        x = self.window_xy[0] + 5 * CELL_W
        row_top = lambda row: self.window_xy[1] + TITLEBAR + row * CELL_H
        def click(at, target, y):
            return [(at, lambda: self.server.user_focus('workspace.focus', workspace_id=target)),
                    (at + 0.05, {'type': 'click', 'x': x, 'y': y, 'window': self.window()})]
        # Calibrate under a trusted config.
        self.server.user_focus('workspace.focus', workspace_id='w1')
        lines = []
        engine = self.run_engine(click(0.1, 'w2', row_top(3) + 2) + click(1.3, 'w1', row_top(2) + 17), log=lines.append)
        self.assertEqual([l.split('calibration ')[1] for l in lines if 'calibration ' in l][-1], 'agrees', lines)

        self.client_started = time.time() - 3600
        (self.config/'config.toml').write_text(stale)
        self.server.user_focus('workspace.focus', workspace_id='w1')
        lines = []
        self.server.calls.clear()
        engine = self.run_engine(click(0.1, 'w2', row_top(3) + 10)
                                 + self.drag(1.5, [((x, row_top(4) + CELL_H / 2), 1.0), ((x, row_top(5) + CELL_H / 2), 1.0)]),
                                 log=lines.append)
        self.assertIn('click on space w2: config check, confirmed', lines, 'the click is contained')
        self.assertTrue(all('space' not in kinds for _, kinds in engine.verified.values()), engine.verified)
        self.assertEqual(self.server.focus_calls(), [])
        self.assertIn('config changed after this herdr client started', engine.status['reason'])

    def test_relearned_geometry_voids_config_confirmations(self):
        # Review N9: 'relearned' adopts a new origin; it never confirms, and it voids
        # what earlier clicks confirmed against the old one.
        engine = core.Engine(ScriptedSensor([]), self.temp/'support', env=self.env)
        model = render.build_model(120, 40, self.server.snapshot, render.ui_from_config({'ui': {}}), render.Preferences())
        tab = next(t for t in model.targets if t.kind == 'tab')
        binding = core.clients.bind(self.client(), {})
        key = (4242, str(binding.config_path))
        engine.verified[key] = (engine.config_stamp(binding.config_path), {'tab', 'pane'})
        engine.read_model = lambda binding, client: (model, None)
        engine.calibration.learn = lambda *args, **kwargs: 'relearned'
        engine.calibration.contains = lambda *args, **kwargs: True
        grid = geometry.Grid(120, 40, CELL_W, CELL_H, geometry.Interval(0, 2), geometry.Interval(0, 28))
        engine.learn({'binding': binding, 'client': self.client(), 'key': 'k', 'grid': grid, 'dx': 10, 'dy': 30},
                     (tab.kind, tab.id))
        self.assertNotIn(key, engine.verified)

    def test_space_clicks_never_confirm_space_rows_and_unknown_stamp_refuses(self):
        # Review H3: spaces.rows and row_gap move only the Spaces below the first.
        # Review N4: a config whose change time cannot be read is not trusted.
        engine = core.Engine(ScriptedSensor([]), self.temp/'support', env=self.env)
        model = render.build_model(120, 40, self.server.snapshot, render.ui_from_config({'ui': {}}), render.Preferences())
        spaces = sorted((t for t in model.targets if t.kind == 'space'), key=lambda t: t.rect.y)
        binding = core.clients.bind(self.client(), {})
        engine.calibration.contains = lambda *args, **kwargs: True
        grid = geometry.Grid(120, 40, CELL_W, CELL_H, geometry.Interval(0, 2), geometry.Interval(0, 28))
        click = {'binding': binding, 'client': self.client(), 'key': 'k', 'grid': grid, 'dx': 10, 'dy': 70}
        engine.confirm_config(click, spaces[0], model)
        stamp, kinds = engine.verified[(4242, str(binding.config_path))]
        self.assertEqual(kinds, set())
        engine.confirm_config(click, spaces[1], model)
        self.assertEqual(engine.verified[(4242, str(binding.config_path))][1], set(),
                         'no Space click confirms Space rows (review N9c)')
        engine.config_stamp = lambda path: None
        with self.assertRaises(core.Unavailable):
            engine.check_config(binding, self.client())

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

    def test_ambiguous_first_space_click_is_only_a_candidate(self):
        # Review N1: before calibration a click on "project" may be on the "assets" row
        # below it, so one click is not proof; a second agreeing click confirms it.
        s = self.server.snapshot
        ws = {w['label']: w for w in s['workspaces']}
        model = render.build_model(120, 40, s, render.ui_from_config({'ui': {}}), render.Preferences())
        y0 = model.find('space', ws['project']['workspace_id']).rect.y
        x0 = self.window_xy[0] + 5 * CELL_W
        top = self.window_xy[1] + TITLEBAR + y0 * CELL_H + 2
        away, here = ws['assets']['workspace_id'], ws['project']['workspace_id']
        steps = [(0.1, lambda: self.server.user_focus('workspace.focus', workspace_id=away)),
                 (0.75, lambda: self.server.user_focus('workspace.focus', workspace_id=here)),
                 (0.8, {'type': 'click', 'x': x0, 'y': top, 'window': self.window()}),
                 (1.6, lambda: self.server.user_focus('workspace.focus', workspace_id=away)),
                 (2.2, {'type': 'click', 'x': x0, 'y': top + CELL_H - 4, 'window': self.window()}),
                 (2.25, lambda: self.server.user_focus('workspace.focus', workspace_id=here))]
        lines = []
        engine = self.run_engine(steps, log=lines.append)
        self.assertEqual([l.split('calibration ')[1] for l in lines if 'calibration ' in l], ['candidate', 'learned'])
        self.assertEqual(engine.status['calibrated'], 1)

    def test_identity_baseline_is_taken_when_subscribing(self):
        # Review M2 residual: a cd between helper start and the first drag is a change.
        engine = core.Engine(ScriptedSensor([]), self.temp/'support', env=self.env)
        sock = self.config/'herdr.sock'
        engine.ensure_subscription(sock)
        self.addCleanup(lambda: [sub.close() for sub in engine.subscriptions.values()])
        workspace = self.server.snapshot['workspaces'][0]['workspace_id']
        cwd = render.identity_cwd(self.server.snapshot, workspace)
        self.assertIsNotNone(cwd)
        self.assertTrue(engine.branch_settled(sock, workspace, cwd))
        self.assertFalse(engine.branch_settled(sock, workspace, cwd + '/elsewhere'))

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

