import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from spring_load import contract
from spring_load.sensor import SensorError, SensorProcess, platform_key, sensor_command

FAKE = ROOT/'sensors/fake/fake_sensor.py'
CLIENT = {'pid': 56533, 'argv': ['herdr'], 'tty': '/dev/ttys078', 'rows': 68, 'cols': 239,
          'xpixel': 1916, 'ypixel': 1160, 'foreground': True}
WINDOW = {'id': '8231', 'pid': 665, 'app': 'com.mitchellh.ghostty',
          'bounds': {'x': 0, 'y': 25, 'width': 1920, 'height': 1164}, 'scale': 2.0, 'peers': [], 'clients': [CLIENT]}
HELLO = '{"type":"hello","t":0,"contract":1,"sensor":"x","version":"0.3.0","coords":"x11-root","capabilities":["drag"]}'

class ContractTest(unittest.TestCase):
    def test_documented_examples_validate(self):
        doc = (ROOT/'docs/SENSOR.md').read_text()
        self.assertIn('"contract": 1', doc)
        for line in [HELLO,
                     json.dumps({'type': 'drag_start', 't': 1, 'x': 1, 'y': 2, 'kind': 'files', 'window': WINDOW}),
                     json.dumps({'type': 'drag_move', 't': 1.1, 'x': 1.5, 'y': 2, 'window': None}),
                     json.dumps({'type': 'drag_end', 't': 2, 'x': 1, 'y': 2, 'reason': 'released'}),
                     json.dumps({'type': 'click', 't': 3, 'x': 1, 'y': 2, 'window': WINDOW}),
                     json.dumps({'type': 'status', 't': 4, 'level': 'warn', 'message': 'm'})]:
            self.assertIsNotNone(contract.parse_event(line), line)
        parsed = contract.parse_event(json.dumps({'type': 'drag_move', 't': 1, 'x': 1, 'y': 2, 'window': WINDOW}))
        self.assertEqual(parsed['window']['clients'][0]['tty'], '/dev/ttys078')

    def test_invalid_events_are_refused(self):
        for line in ['not json', '[]', '{"type":"drag_move","t":1,"x":"1","y":2,"window":null}',
                     '{"type":"drag_end","t":1,"x":1,"y":2,"reason":"dropped"}',
                     '{"type":"drag_move","t":1,"x":1,"y":2,"window":{"id":"1","pid":1}}',
                     '{"type":"drag_move","t":1,"x":NaN,"y":2,"window":null}',
                     '{"type":"click","t":1,"x":1,"y":2,"window":' + json.dumps({**WINDOW, 'clients': [{'pid': 1}]}) + '}',
                     'x' * (contract.MAX_LINE + 1)]:
            self.assertIsNone(contract.parse_event(line), line[:80])
        self.assertIsNone(contract.parse_event('{"type":"future","t":1}'), 'unknown types are ignored')

    def test_optional_env_and_all_clients(self):
        client = {**CLIENT, 'env': {'HERDR_SESSION': 'work', 'SECRET_TOKEN': 'x'}}
        event = contract.parse_event(json.dumps({'type': 'drag_start', 't': 1, 'x': 1, 'y': 2, 'kind': 'files',
                                                 'window': None, 'all_clients': [client]}))
        self.assertEqual(event['all_clients'][0]['env'], {'HERDR_SESSION': 'work'}, 'only the listed keys are kept')
        plain = contract.parse_event(json.dumps({'type': 'drag_start', 't': 1, 'x': 1, 'y': 2, 'window': None}))
        self.assertIsNone(plain['all_clients'])
        self.assertIsNone(contract.parse_event(json.dumps({'type': 'drag_start', 't': 1, 'x': 1, 'y': 2, 'window': None, 'all_clients': {}})))

    def test_optional_started(self):
        event = contract.parse_event(json.dumps({'type': 'drag_move', 't': 1, 'x': 1, 'y': 2,
                                                 'window': {**WINDOW, 'clients': [{**CLIENT, 'started': 1793280547.5}]}}))
        self.assertEqual(event['window']['clients'][0]['started'], 1793280547.5)
        self.assertIsNone(contract.parse_event(json.dumps({'type': 'drag_move', 't': 1, 'x': 1, 'y': 2,
                                                           'window': {**WINDOW, 'clients': [{**CLIENT, 'started': 'x'}]}})))

    def test_unknown_fields_are_ignored(self):
        event = contract.parse_event('{"type":"drag_end","t":1,"x":1,"y":2,"reason":"released","extra":5}')
        self.assertNotIn('extra', event)

    def test_hello_version_gate(self):
        with self.assertRaises(contract.ContractError):
            contract.validate_hello(json.loads(HELLO.replace('"contract":1', '"contract":2')))
        with self.assertRaises(contract.ContractError):
            contract.validate_hello(json.loads(HELLO.replace('x11-root', 'wayland')))

    def test_stream_checker(self):
        good = [HELLO, '{"type":"drag_start","t":1,"x":1,"y":1,"kind":"unknown","window":null}',
                '{"type":"drag_move","t":1,"x":1,"y":1,"window":null}', '{"type":"drag_end","t":2,"x":1,"y":1,"reason":"released"}']
        self.assertEqual(contract.check_stream(good), (True, 'ok'))
        self.assertFalse(contract.check_stream(good[1:])[0], 'hello must come first')
        self.assertFalse(contract.check_stream([HELLO, good[2]])[0], 'move outside a drag')
        self.assertFalse(contract.check_stream([HELLO, good[1], good[1]])[0], 'two starts')
        self.assertFalse(contract.check_stream([HELLO, good[3].replace('"t":2', '"t":-1')])[0], 'clock went back')
        result = subprocess.run([sys.executable, '-m', 'spring_load.contract'], cwd=ROOT, input='\n'.join(good),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout)

class SensorLoadingTest(unittest.TestCase):
    def script(self, data):
        handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False)
        json.dump(data, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_platform_key(self):
        self.assertEqual([platform_key(p) for p in ('darwin', 'linux', 'win32')], ['macos', 'linux', 'windows'])

    def test_discovery(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(sensor_command(temp, environ={}, platform='linux'))
            binary = Path(temp)/'sensors/herdr-spring-load-sensor-linux'
            binary.parent.mkdir()
            binary.write_text('#!/bin/sh\n')
            self.assertIsNone(sensor_command(temp, environ={}, platform='linux'), 'not executable')
            binary.chmod(0o755)
            self.assertEqual(sensor_command(temp, environ={}, platform='linux'), [str(binary)])
            self.assertEqual(sensor_command(temp, environ={'SPRING_LOAD_SENSOR': '["a","b"]'}), ['a', 'b'])
            with self.assertRaises(SensorError):
                sensor_command(temp, environ={'SPRING_LOAD_SENSOR': '"a"'})

    def test_fake_sensor_round_trip(self):
        path = self.script({'events': [
            {'after': 0.01, 'type': 'drag_start', 'x': 1, 'y': 1, 'kind': 'files', 'window': WINDOW},
            {'after': 0.01, 'raw': '{broken'},
            {'after': 0.01, 'type': 'drag_move', 'x': 5, 'y': 6, 'window': WINDOW},
            {'after': 0.01, 'type': 'drag_end', 'x': 5, 'y': 6, 'reason': 'released'}]})
        logs = []
        sensor = SensorProcess([sys.executable, str(FAKE), path], log=logs.append)
        try:
            types = [sensor.next(timeout=5)['type'] for _ in range(4)]
            self.assertEqual(types, ['hello', 'drag_start', 'drag_move', 'drag_end'])
            self.assertTrue(any('invalid JSON' in l for l in logs))
            self.assertIsNone(sensor.next(timeout=0.2), 'a real or fake sensor waits for stdin EOF')
        finally:
            sensor.close()
        self.assertEqual(sensor.process.returncode, 0, 'EOF on stdin ends the sensor')

    def test_bad_hello_is_refused(self):
        path = self.script({'hello': {'contract': 2}, 'exit': 'now'})
        sensor = SensorProcess([sys.executable, str(FAKE), path])
        self.addCleanup(sensor.close)
        with self.assertRaises(SensorError):
            sensor.next(timeout=5)

    def test_exit_is_reported(self):
        sensor = SensorProcess([sys.executable, str(FAKE), self.script({'exit': 'now'})])
        self.addCleanup(sensor.close)
        self.assertEqual(sensor.next(timeout=5)['type'], 'hello')
        with self.assertRaises(SensorError):
            sensor.next(timeout=5)

    def test_check_flag(self):
        result = subprocess.run([sys.executable, str(FAKE), '--check'], capture_output=True, text=True, timeout=5)
        self.assertEqual(contract.check_stream(result.stdout.splitlines()), (True, 'ok'))
