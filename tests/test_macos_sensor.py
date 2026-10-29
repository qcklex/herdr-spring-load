"""The macOS sensor: builds, speaks the contract, finds herdr clients on a tty,
replays drags and clicks correctly, and uses no permissioned API."""
import json
import os
from pathlib import Path
import pty
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from spring_load import contract

SOURCE = ROOT/'sensors/macos/main.swift'
FORBIDDEN = ['CGEventPost', 'CGEventTap', 'CGEventCreate', 'AXUIElement', 'AXIsProcessTrusted', 'NSAppleScript',
             'osascript', 'CGWindowListCreateImage', 'CGDisplayStream', 'SCStream', 'kCGWindowName',
             'CGRequestScreenCaptureAccess', 'CGPreflight', 'IOHIDManager', 'addGlobalMonitorForEvents',
             'setString', 'setData', 'clearContents', 'writeObjects', 'readObjects', 'data(forType', 'string(forType',
             'propertyList(forType', 'performDragOperation', 'TIOCSTI', 'O_WRONLY', 'O_RDWR', 'activate(', 'NSWorkspace']

def load_build():
    import importlib.util
    spec = importlib.util.spec_from_file_location('macos_build', ROOT/'sensors/macos/build.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class SourceAuditTest(unittest.TestCase):
    def test_no_permissioned_or_acting_api(self):
        code = '\n'.join(line.split('//')[0] for line in SOURCE.read_text().splitlines())
        for name in FORBIDDEN:
            self.assertNotIn(name, code, f'{name} would need a permission or act on the system')
        self.assertIn('O_RDONLY | O_NOCTTY | O_NONBLOCK', code)

@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc') and shutil.which('cc'), 'macOS toolchain only')
class MacSensorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='spring-load-sensor-'))
        cls.binary = cls.tmp/'herdr-spring-load-sensor-macos'
        load_build().build(cls.binary)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_sensor(self, *args, input=None):
        return subprocess.run([str(self.binary), *args], input=input, capture_output=True, text=True, timeout=30)

    def test_check_prints_a_valid_hello(self):
        result = self.run_sensor('--check')
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        hello = contract.validate_hello(json.loads(lines[0]))
        self.assertEqual((hello['sensor'], hello['coords']), ('macos', 'macos-points'))
        self.assertEqual(set(hello['capabilities']), {'drag', 'click', 'window', 'clients'})

    def test_exits_on_stdin_eof(self):
        process = subprocess.Popen([str(self.binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            first = process.stdout.readline()
            contract.validate_hello(json.loads(first))
            process.stdin.close()
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()

    def test_finds_herdr_clients_on_a_tty(self):
        fake = self.tmp/'herdr'
        c_source = self.tmp/'herdr.c'
        c_source.write_text('#include <unistd.h>\nint main(void) { pause(); return 0; }\n')
        subprocess.run(['cc', '-o', str(fake), str(c_source)], check=True, capture_output=True)
        master, slave = pty.openpty()
        import fcntl, struct, termios
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 960, 640))
        env = {'HOME': str(self.tmp), 'HERDR_SESSION': 'work', 'SECRET_TOKEN': 'nope', 'PATH': '/usr/bin:/bin'}
        def controlling_tty():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        child = subprocess.Popen([str(fake), '--session', 'work'], stdin=slave, stdout=slave, stderr=slave, env=env,
                                 preexec_fn=controlling_tty)
        try:
            deadline = time.monotonic() + 5
            while True:
                result = self.run_sensor('--clients-below', str(os.getpid()))
                clients = json.loads(result.stdout)['clients']
                if clients or time.monotonic() > deadline:
                    break
                time.sleep(0.05)
            self.assertEqual(len(clients), 1, result.stdout)
            client = contract.validate_client(clients[0])
            self.assertEqual(client['pid'], child.pid)
            self.assertEqual(client['argv'], [str(fake), '--session', 'work'])
            self.assertEqual((client['rows'], client['cols'], client['xpixel'], client['ypixel']), (40, 120, 960, 640))
            self.assertEqual(client['tty'], os.ttyname(slave))
            self.assertIs(client['foreground'], True)
            self.assertLess(abs(client['started'] - time.time()), 30, 'process start time in epoch seconds')
            self.assertEqual(client['env'], {'HOME': str(self.tmp), 'HERDR_SESSION': 'work'}, 'only the listed keys')
            everyone = json.loads(self.run_sensor('--all-clients').stdout)['clients']
            self.assertIn(child.pid, [c['pid'] for c in everyone])
            self.assertEqual(json.loads(self.run_sensor('--clients-below', str(child.pid)).stdout)['clients'], [])
        finally:
            child.kill()
            child.wait()
            os.close(master)
            os.close(slave)

    def test_window_lookup_reports_a_valid_window_or_null(self):
        for x, y in ((5, 5), (400, 300), (-100000, -100000)):
            result = self.run_sensor('--window-at', str(x), str(y))
            self.assertEqual(result.returncode, 0, result.stderr)
            window = contract.validate_window(json.loads(result.stdout)['window'])
            if window is not None:
                b = window['bounds']
                self.assertTrue(b['x'] <= x <= b['x'] + b['width'] and b['y'] <= y <= b['y'] + b['height'])
        self.assertIsNone(json.loads(self.run_sensor('--window-at', '-100000', '-100000').stdout)['window'])

    def replay(self, samples):
        result = self.run_sensor('--simulate', input='\n'.join(json.dumps(s) for s in samples) + '\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(contract.check_stream(result.stdout.splitlines()), (True, 'ok'), result.stdout)
        return [json.loads(line) for line in result.stdout.splitlines()[1:]]

    def test_drag_lifecycle_and_move_rates(self):
        samples = [{'t': 0.0, 'down': False, 'x': 0, 'y': 0, 'count': 5},
                   {'t': 0.02, 'down': True, 'x': 10, 'y': 10, 'count': 5}]
        t = 0.04
        samples.append({'t': t, 'down': True, 'x': 12, 'y': 10, 'count': 6, 'window': '7'})   # drag starts
        for i in range(10):                                                                     # moving every 20 ms
            t += 0.02
            samples.append({'t': round(t, 3), 'down': True, 'x': 20 + i, 'y': 10, 'count': 6, 'window': '7'})
        for _ in range(30):                                                                     # holding still 600 ms
            t += 0.02
            samples.append({'t': round(t, 3), 'down': True, 'x': 29, 'y': 10, 'count': 6, 'window': '7'})
        samples.append({'t': round(t + 0.02, 3), 'down': False, 'x': 29, 'y': 10, 'count': 6})
        events = self.replay(samples)
        kinds = [e['type'] for e in events]
        self.assertEqual(kinds[0], 'drag_start')
        self.assertEqual(kinds[-1], 'drag_end')
        self.assertEqual(events[-1]['reason'], 'released')
        self.assertNotIn('click', kinds, 'a press that becomes a drag is no click')
        self.assertEqual(events[0]['window']['id'], '7')
        moves = [e for e in events if e['type'] == 'drag_move']
        gaps = [b['t'] - a['t'] for a, b in zip([events[0]] + moves, moves)]
        self.assertTrue(all(g >= 0.03 - 1e-9 for g in gaps), gaps)
        self.assertTrue(all(g <= 0.25 + 0.02 + 1e-9 for g in gaps), gaps)
        still = [m for m in moves if m['x'] == 29]
        self.assertGreaterEqual(len(still), 2, 'a held-still drag still reports every 250 ms')

    def test_clicks(self):
        events = self.replay([
            {'t': 0.0, 'down': False, 'x': 0, 'y': 0, 'count': 1},
            {'t': 0.1, 'down': True, 'x': 100, 'y': 50, 'count': 1, 'window': '3'},
            {'t': 0.2, 'down': False, 'x': 104, 'y': 52, 'count': 1},     # a click
            {'t': 1.0, 'down': True, 'x': 100, 'y': 50, 'count': 1},
            {'t': 1.2, 'down': False, 'x': 140, 'y': 50, 'count': 1},     # moved too far
            {'t': 2.0, 'down': True, 'x': 100, 'y': 50, 'count': 1},
            {'t': 2.7, 'down': True, 'x': 100, 'y': 50, 'count': 1},
            {'t': 2.8, 'down': False, 'x': 100, 'y': 50, 'count': 1}])    # held too long
        self.assertEqual([e['type'] for e in events], ['click'])
        self.assertEqual((events[0]['x'], events[0]['y'], events[0]['window']['id']), (100, 50, '3'))

    def test_pasteboard_change_without_button_is_no_drag_and_stall_cancels(self):
        events = self.replay([
            {'t': 0.0, 'down': False, 'x': 0, 'y': 0, 'count': 1},
            {'t': 0.1, 'down': False, 'x': 0, 'y': 0, 'count': 2},
            {'t': 0.2, 'down': True, 'x': 0, 'y': 0, 'count': 2},
            {'t': 0.3, 'down': True, 'x': 5, 'y': 0, 'count': 3, 'kind': 'other'},
            {'t': 5.0, 'down': True, 'x': 5, 'y': 0, 'count': 3}])
        self.assertEqual([e['type'] for e in events], ['drag_start', 'drag_end'])
        self.assertEqual(events[0]['kind'], 'other')
        self.assertEqual(events[1]['reason'], 'cancelled')

if __name__ == '__main__':
    unittest.main()
