#!/usr/bin/env python3
"""Fake sensor for tests: replays a scripted session (docs/SENSOR.md, section 7).

fake_sensor.py SCRIPT.json [--check]   (or FAKE_SENSOR_SCRIPT=SCRIPT.json)"""
import json
import os
import sys
import time

def main(argv):
    args = [a for a in argv if not a.startswith('--')]
    path = args[0] if args else os.environ.get('FAKE_SENSOR_SCRIPT')
    script = json.loads(open(path).read()) if path else {}
    start = time.monotonic()
    def emit(event):
        line = dict(event)
        line.pop('after', None)
        line.setdefault('t', round(time.monotonic() - start, 4))
        try:
            sys.stdout.write(json.dumps(line, separators=(',', ':')) + '\n')
            sys.stdout.flush()
        except BrokenPipeError:
            sys.exit(0)
    hello = {'type': 'hello', 'contract': 1, 'sensor': 'fake', 'version': '0.3.0',
             'coords': 'macos-points', 'capabilities': ['drag', 'click', 'window', 'clients']}
    hello.update(script.get('hello', {}))
    emit(hello)
    if '--check' in argv:
        return
    for event in script.get('events', []):
        time.sleep(float(event.get('after', 0)))
        if 'raw' in event:
            # Deliberately malformed output, for the core's validation tests.
            sys.stdout.write(event['raw'] + '\n')
            sys.stdout.flush()
            continue
        emit(event)
    if script.get('exit', 'eof') == 'eof':
        sys.stdin.read()

if __name__ == '__main__':
    main(sys.argv[1:])
