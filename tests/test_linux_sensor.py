import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SENSOR = ROOT/'sensors/linux'
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SENSOR))
from spring_load import contract
import build as linux_build
import common
import hyprland
import main as linux_main
from tracker import Tracker

CLIENT = {'pid': 41, 'argv': ['herdr'], 'tty': '/dev/pts/3', 'rows': 36, 'cols': 120,
          'xpixel': 0, 'ypixel': 0, 'foreground': True}
WINDOW = {'id': '4194317', 'pid': 30, 'app': 'XTerm', 'bounds': {'x': 401, 'y': 171, 'width': 724, 'height': 472},
          'scale': 1.0, 'peers': [], 'clients': [CLIENT], 'frame': {'x': 400, 'y': 150, 'width': 726, 'height': 494}}


def stamped(events):
    """Contract lines from tracker output, with increasing t."""
    hello = {'type': 'hello', 't': 0, 'contract': 1, 'sensor': 'linux-x11', 'version': '0.3.0',
             'coords': 'x11-root', 'capabilities': ['drag', 'click', 'window', 'clients']}
    return [json.dumps(hello)] + [json.dumps({**e, 't': i + 1}) for i, e in enumerate(events)]


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.window = WINDOW
        self.tracker = Tracker(lambda x, y: self.window, lambda: 'files', self.events.append)

    def types(self):
        return [e['type'] for e in self.events]

    def test_xdnd_drag_gives_start_moves_and_released_end(self):
        t = self.tracker
        t.step(0.00, 10, 10, False)
        t.step(0.02, 10, 10, True)                      # press on the source
        t.step(0.04, 14, 12, True, 'started')           # the source took XdndSelection
        for i in range(1, 20):
            t.step(0.04 + i * 0.02, 14 + i * 20, 12 + i * 10, True)
        t.step(0.50, 400, 200, False)                   # release: the drop
        self.assertEqual(self.types()[0], 'drag_start')
        self.assertEqual(self.types()[-1], 'drag_end')
        self.assertEqual(self.events[0]['kind'], 'files')
        self.assertEqual(self.events[0]['window'], WINDOW)
        self.assertEqual(self.events[-1]['reason'], 'released')
        moves = [e for e in self.events if e['type'] == 'drag_move']
        self.assertTrue(moves and all(m['window'] == WINDOW for m in moves))
        self.assertNotIn('click', self.types())
        ok, message = contract.check_stream(stamped(self.events))
        self.assertTrue(ok, message)

    def test_drag_start_carries_all_clients(self):
        tracker = Tracker(lambda x, y: None, lambda: 'unknown', self.events.append, lambda: [CLIENT])
        tracker.step(0.0, 1, 1, True, 'started')
        self.assertEqual(self.events[0]['all_clients'], [CLIENT])
        parsed = contract.parse_event(json.dumps({**self.events[0], 't': 1}))
        self.assertEqual(parsed['all_clients'][0]['pid'], 41)

    def test_selection_owner_without_held_button_is_not_a_drag(self):
        self.tracker.step(0.0, 5, 5, False, 'started')
        self.tracker.step(0.1, 5, 5, True)              # a later press must not inherit the old owner
        self.tracker.step(0.2, 50, 5, True)
        self.assertNotIn('drag_start', self.types())

    def test_lost_source_cancels(self):
        t = self.tracker
        t.step(0.0, 5, 5, True, 'started')
        t.step(0.1, 6, 5, True, 'lost')
        self.assertEqual(self.events[-1], {'type': 'drag_end', 'x': 6, 'y': 5, 'reason': 'cancelled'})
        t.step(0.2, 6, 5, True)
        self.assertEqual(self.types().count('drag_end'), 1)

    def test_move_rate_limits(self):
        t = self.tracker
        t.step(0.000, 0, 0, True, 'started')
        t.step(0.010, 1, 0, True)                       # under 30 ms since the start: held back
        t.step(0.035, 2, 0, True)                       # changed and 30 ms passed: sent
        t.step(0.070, 2, 0, True)                       # unchanged: held back
        t.step(0.300, 2, 0, True)                       # unchanged but 250 ms passed: heartbeat
        moves = [e['x'] for e in self.events if e['type'] == 'drag_move']
        self.assertEqual(moves, [2, 2])

    def test_click_is_reported_only_for_short_still_presses(self):
        t = self.tracker
        t.step(0.0, 100, 100, True)
        t.step(0.1, 103, 102, False)                    # click
        t.step(1.0, 100, 100, True)
        t.step(1.1, 120, 100, False)                    # moved 20: not a click
        t.step(2.0, 100, 100, True)
        t.step(2.7, 100, 100, False)                    # held 700 ms: not a click
        clicks = [e for e in self.events if e['type'] == 'click']
        self.assertEqual(len(clicks), 1)
        self.assertEqual((clicks[0]['x'], clicks[0]['y']), (100, 100))

    def test_cancel_closes_an_open_drag_once(self):
        self.tracker.step(0.0, 1, 1, True, 'started')
        self.tracker.cancel(1, 1)
        self.tracker.cancel(1, 1)
        self.assertEqual(self.types(), ['drag_start', 'drag_end'])


class FakeProc:
    """A /proc tree in a temporary directory."""

    def __init__(self, root, btime=1_700_000_000):
        self.root = Path(root)
        (self.root/'stat').write_text('cpu  1 2 3\nbtime %d\nprocesses 9\n' % btime)

    def add(self, pid, comm, ppid, pgrp=None, tty_nr=0, tpgid=-1, argv=(), exe=None, environ=None, start=0):
        d = self.root/str(pid)
        (d/'fd').mkdir(parents=True)
        fields = ['S', ppid, pgrp or pid, pgrp or pid, tty_nr, tpgid] + [0] * 13 + [start] + [0] * 26
        (d/'stat').write_text('%d (%s) %s\n' % (pid, comm, ' '.join(map(str, fields))))
        (d/'cmdline').write_bytes(b''.join(a.encode() + b'\0' for a in argv))
        if exe:
            os.symlink(exe, d/'exe')
        if environ is not None:
            (d/'environ').write_bytes(b''.join(('%s=%s' % kv).encode() + b'\0' for kv in environ.items()))


PTS = lambda n: (136 + n // 256) << 8 | (n % 256)


class ProcTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = FakeProc(self.tmp.name)
        p = self.proc
        p.add(30, 'xterm', 1, argv=['xterm', '-e', 'herdr'])
        p.add(31, 'sh', 30, pgrp=31, tty_nr=PTS(3), tpgid=41, argv=['sh'])
        p.add(41, 'herdr', 31, pgrp=41, tty_nr=PTS(3), tpgid=41, argv=['herdr'], exe='/usr/local/bin/herdr',
              start=12345 * os.sysconf('SC_CLK_TCK'),
              environ={'HOME': '/home/t', 'HERDR_SESSION': 'work', 'SECRET_TOKEN': 'x', 'PATH': '/bin'})
        p.add(42, 'herdr', 41, pgrp=41, tty_nr=PTS(3), tpgid=41, argv=['herdr', 'plugin', 'x'])
        p.add(50, 'herdr', 1, argv=['herdr', 'server'])                     # no tty, not below xterm
        p.add(60, 'weird (name) x', 30, tty_nr=PTS(300), tpgid=99, argv=['/opt/herdr/herdr', '--session', 'w'])
        p.add(70, 'herdr-wrapper', 30, tty_nr=PTS(4), argv=['herdr-wrapper'], exe='/usr/bin/herdr-wrapper')

    def tearDown(self):
        self.tmp.cleanup()

    def clients(self):
        sizes = {'/dev/pts/3': (36, 120, 0, 0)}
        return common.herdr_clients(30, proc=self.tmp.name, size=lambda path: sizes.get(path))

    def test_finds_every_herdr_with_a_tty_below_the_window_owner(self):
        found = {c['pid']: c for c in self.clients()}
        self.assertEqual(sorted(found), [41, 42, 60])
        self.assertEqual(found[41], {'pid': 41, 'argv': ['herdr'], 'tty': '/dev/pts/3', 'rows': 36, 'cols': 120,
                                     'xpixel': 0, 'ypixel': 0, 'foreground': True, 'started': 1_700_012_345,
                                     'env': {'HOME': '/home/t', 'HERDR_SESSION': 'work'}})
        self.assertEqual(contract.validate_client(found[41])['started'], 1_700_012_345)
        self.assertIsNone(found[42]['env'])                             # unreadable environ: null
        self.assertEqual(found[42]['argv'], ['herdr', 'plugin', 'x'])   # reported, not classified
        self.assertEqual(found[60]['tty'], '/dev/pts/300')
        self.assertIsNone(found[60]['rows'])
        self.assertFalse(found[60]['foreground'])
        for client in found.values():
            contract.validate_client(client)

    def test_all_clients_lists_every_herdr_with_a_tty_of_this_user(self):
        everyone = common.all_herdr_clients(proc=self.tmp.name, size=lambda path: None)
        self.assertEqual([c['pid'] for c in everyone], [41, 42, 60])
        self.assertEqual(common.all_herdr_clients(proc=self.tmp.name, size=lambda path: None, uid=os.getuid() + 1), [])

    def test_unreadable_proc_gives_null(self):
        self.assertIsNone(common.herdr_clients(30, proc=self.tmp.name + '/missing'))

    def test_tty_path_decodes_unix98_ptys(self):
        self.assertEqual(common.tty_path(PTS(0)), '/dev/pts/0')
        self.assertEqual(common.tty_path(PTS(5)), '/dev/pts/5')
        self.assertEqual(common.tty_path(PTS(300)), '/dev/pts/300')
        self.assertIsNone(common.tty_path(0))

    def test_tab_ttys_stop_at_the_first_tty_on_each_branch(self):
        p = self.proc
        p.add(43, 'herdr', 41, tty_nr=0, argv=['herdr', 'server'])               # server below the client, no tty
        p.add(44, 'sh', 43, pgrp=44, tty_nr=PTS(9), tpgid=44, argv=['sh'])        # a herdr pane: not a tab
        p.add(80, 'helper', 30, argv=['helper'])                                  # no tty: look below it
        p.add(81, 'bash', 80, pgrp=81, tty_nr=PTS(5), tpgid=81, argv=['bash'])
        clients, ttys = common.terminal_processes(30, proc=self.tmp.name, size=lambda path: None)
        self.assertEqual([c['pid'] for c in clients], [41, 42, 60])
        self.assertEqual(ttys, {PTS(3), PTS(300), PTS(4), PTS(5)})

    def hidden_tab_terminal(self, *tabs, owner=90):
        """A single-process terminal with one tab per tty number; herdr in the first."""
        p = self.proc
        p.add(owner, 'kitty', 1, argv=['kitty'])
        for n, tty in enumerate(tabs):
            p.add(owner + 1 + 2 * n, 'sh', owner, pgrp=owner + 1 + 2 * n, tty_nr=PTS(tty), tpgid=owner + 1 + 2 * n,
                  argv=['sh'])
        p.add(owner + 2, 'herdr', owner + 1, pgrp=owner + 2, tty_nr=PTS(tabs[0]), tpgid=owner + 2, argv=['herdr'],
              exe='/usr/local/bin/herdr')
        return common.terminal_processes(owner, proc=self.tmp.name, size=lambda path: (36, 110, 990, 648))

    def test_a_hidden_tab_terminal_counts_its_tabs_not_herdr_panes(self):
        clients, ttys = self.hidden_tab_terminal(10, 11)       # herdr in one tab, a shell in the other
        self.assertEqual([(c['pid'], c['foreground']) for c in clients], [(92, True)])
        self.assertEqual(ttys, {PTS(10), PTS(11)})
        clients, ttys = self.hidden_tab_terminal(12, owner=190)
        self.assertEqual((len(clients), len(ttys)), (1, 1))

    def test_client_cache_refreshes_on_window_change_and_after_a_second(self):
        calls, clock = [], [0.0]
        cache = common.ClientCache(lookup=lambda pid: calls.append(pid) or [pid], clock=lambda: clock[0])
        cache.get('a', 1)
        clock[0] = 0.5
        cache.get('a', 1)
        cache.get('b', 1)
        clock[0] = 1.6
        cache.get('b', 1)
        cache.reset()
        cache.get('b', 1)
        self.assertEqual(len(calls), 4)


class FakeHyprland:
    def __init__(self, replies):
        self.dir = tempfile.mkdtemp(dir='/tmp')
        self.path = os.path.join(self.dir, 's')
        self.replies = replies
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(self.path)
        self.server.listen(8)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            with connection:
                command = connection.recv(1024).decode()
                connection.sendall(json.dumps(self.replies[command.removeprefix('j/')]).encode())

    def close(self):
        self.server.close()
        os.unlink(self.path)
        os.rmdir(self.dir)


def hypr_client(address, pid, at, size, workspace=1, **extra):
    return {'address': address, 'pid': pid, 'class': 'kitty', 'at': at, 'size': size, 'mapped': True,
            'hidden': False, 'workspace': {'id': workspace, 'name': str(workspace)}, 'floating': False,
            'fullscreen': False, 'focusHistoryID': 5, **extra}


class HyprlandTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHyprland({
            'cursorpos': {'x': 300, 'y': 200},
            'monitors': [{'scale': 1.5, 'activeWorkspace': {'id': 1}, 'specialWorkspace': {'id': 0}}],
            'clients': [
                hypr_client('0xa', 700, [0, 0], [960, 1080]),
                hypr_client('0xb', 700, [960, 0], [960, 1080]),
                hypr_client('0xc', 800, [200, 100], [400, 300], floating=True, focusHistoryID=0),
                hypr_client('0xd', 900, [0, 0], [1920, 1080], workspace=2),
                hypr_client('0xe', 700, [0, 0], [100, 100], hidden=True),
            ]})
        self.backend = hyprland.Hyprland(self.fake.path)
        self.backend.cache = common.ClientCache(lookup=lambda pid: ([], set()))

    def tearDown(self):
        self.fake.close()

    def test_floating_window_wins_and_other_workspaces_are_ignored(self):
        x, y = self.backend.cursor()
        window = self.backend.describe(x, y)
        self.assertEqual((window['id'], window['pid'], window['scale']), ('0xc', 800, 1.5))
        self.assertEqual(window['bounds'], {'x': 200, 'y': 100, 'width': 400, 'height': 300})
        contract.validate_window(window)

    def test_tiled_window_with_peer_of_the_same_pid(self):
        window = self.backend.describe(100, 900)
        self.assertEqual(window['id'], '0xa')
        self.assertEqual(window['peers'], [{'x': 960, 'y': 0, 'width': 960, 'height': 1080}])

    def test_window_reports_sessions_for_the_core_hidden_tab_rule(self):
        herdr = {'pid': 5, 'argv': ['herdr'], 'tty': '/dev/pts/1', 'rows': 36, 'cols': 110, 'xpixel': 990,
                 'ypixel': 648, 'foreground': True, 'started': 1.0, 'env': {}}
        self.backend.cache = common.ClientCache(lookup=lambda pid: ([herdr], {1, 2, 3}))
        window = self.backend.describe(100, 900)                # 0xa and its peer 0xb
        self.assertEqual((window['sessions'], len(window['peers']), window['clients']), (3, 1, [herdr]))
        self.assertEqual(contract.validate_window(window)['sessions'], 3)
        self.backend.cache = common.ClientCache(lookup=lambda pid: None)
        window = self.backend.describe(100, 900)
        self.assertEqual((window['sessions'], window['clients']), (None, None))

    def test_socket_path_prefers_xdg_runtime_dir(self):
        with tempfile.TemporaryDirectory() as runtime:
            os.makedirs(os.path.join(runtime, 'hypr', 'sig'))
            open(os.path.join(runtime, 'hypr', 'sig', '.socket.sock'), 'w').close()
            env = {'HYPRLAND_INSTANCE_SIGNATURE': 'sig', 'XDG_RUNTIME_DIR': runtime}
            self.assertEqual(hyprland.socket_path(env), os.path.join(runtime, 'hypr', 'sig', '.socket.sock'))
        self.assertIsNone(hyprland.socket_path({}))


class SessionTest(unittest.TestCase):
    def test_session_detection(self):
        self.assertEqual(linux_main.session({'DISPLAY': ':0'}), 'x11')
        self.assertEqual(linux_main.session({'WAYLAND_DISPLAY': 'w', 'DISPLAY': ':0'}), 'wayland')
        self.assertEqual(linux_main.session({'WAYLAND_DISPLAY': 'w', 'HYPRLAND_INSTANCE_SIGNATURE': 's'}),
                         'hyprland')
        self.assertIsNone(linux_main.session({}))


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)/'herdr-spring-load-sensor-linux'
        linux_build.build(self.out)

    def tearDown(self):
        self.tmp.cleanup()

    def run_sensor(self, env, *args, **kwargs):
        clean = {k: v for k, v in os.environ.items()
                 if k not in ('DISPLAY', 'WAYLAND_DISPLAY', 'XDG_SESSION_TYPE', 'HYPRLAND_INSTANCE_SIGNATURE')}
        return subprocess.run([str(self.out), *args], env={**clean, **env}, capture_output=True, text=True,
                              timeout=10, **kwargs)

    def test_check_prints_one_valid_hello_per_session(self):
        self.assertTrue(os.access(self.out, os.X_OK))
        x11 = self.run_sensor({'DISPLAY': ':99'}, '--check')
        self.assertEqual(x11.returncode, 0)
        self.assertEqual(len(x11.stdout.splitlines()), 1)
        self.assertEqual(contract.check_stream(x11.stdout.splitlines()), (True, 'ok'))
        self.assertEqual(json.loads(x11.stdout)['capabilities'], ['drag', 'click', 'window', 'clients'])
        none = self.run_sensor({}, '--check')
        self.assertEqual(json.loads(none.stdout)['capabilities'], [])

    def test_hyprland_check_hello_is_valid_and_inert(self):
        # Review N13: setup validates the --check hello, and the core needs a valid
        # one to report inert. Hyprland's hello was rejected (coords 'hyprland-layout').
        hyprland_env = {'WAYLAND_DISPLAY': 'wayland-1', 'XDG_SESSION_TYPE': 'wayland',
                        'HYPRLAND_INSTANCE_SIGNATURE': 'sig', 'XDG_RUNTIME_DIR': self.tmp.name}
        check = self.run_sensor(hyprland_env, '--check')
        self.assertEqual(check.returncode, 0)
        lines = check.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        hello = contract.validate_hello(json.loads(lines[0]))
        self.assertEqual((hello['sensor'], hello['capabilities']), ('linux-hyprland', []))

    def test_hyprland_run_is_inert_with_a_reason(self):
        with tempfile.TemporaryDirectory() as runtime:
            os.makedirs(os.path.join(runtime, 'hypr', 'sig'))
            open(os.path.join(runtime, 'hypr', 'sig', '.socket.sock'), 'w').close()
            for env in ({'HYPRLAND_INSTANCE_SIGNATURE': 'sig', 'XDG_RUNTIME_DIR': runtime},     # IPC socket found
                        {'HYPRLAND_INSTANCE_SIGNATURE': 'none', 'XDG_RUNTIME_DIR': runtime}):  # no socket
                result = self.run_sensor({'WAYLAND_DISPLAY': 'wayland-1', **env}, input='')
                self.assertEqual(result.returncode, 0)
                lines = result.stdout.splitlines()
                self.assertEqual(contract.check_stream(lines), (True, 'ok'))
                hello, why = map(json.loads, lines[:2])
                self.assertEqual(contract.validate_hello(hello)['capabilities'], [])
                self.assertEqual((why['type'], why['level']), ('status', 'error'))

    def test_inert_sensor_stays_up_until_stdin_closes(self):
        started = time.monotonic()
        result = self.run_sensor({'WAYLAND_DISPLAY': 'w'}, input='')
        self.assertEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 5)
        lines = result.stdout.splitlines()
        self.assertEqual(contract.check_stream(lines), (True, 'ok'))
        self.assertEqual(json.loads(lines[1])['level'], 'error')

    def test_x11_sensor_without_a_server_reports_why_and_waits(self):
        result = self.run_sensor({'DISPLAY': ':987'}, input='')
        lines = result.stdout.splitlines()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(lines[0])['capabilities'], [])
        self.assertIn('X11 sensor inert', json.loads(lines[1])['message'])


if __name__ == '__main__':
    unittest.main()
