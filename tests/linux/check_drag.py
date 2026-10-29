#!/usr/bin/env python3
"""Check the sensor output of a container drag test against its ground truth.

check_drag.py OUT_DIR   (reads sensor.ndjson, truth.json and sensor-exit.txt; exit 1 on any failure)"""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from spring_load import contract


def main(out):
    out = Path(out)
    lines = out.joinpath('sensor.ndjson').read_text().splitlines()
    truth = json.loads(out.joinpath('truth.json').read_text())
    events = [json.loads(line) for line in lines]
    failures = []

    def check(condition, message):
        print(('PASS ' if condition else 'FAIL ') + message)
        if not condition:
            failures.append(message)

    ok, why = contract.check_stream(lines)
    check(ok, 'contract.check_stream: %s (%d lines)' % (why, len(lines)))
    hello = events[0]
    check(hello['type'] == 'hello' and hello['coords'] == 'x11-root'
          and hello['capabilities'] == ['drag', 'click', 'window', 'clients'], 'hello: %s' % json.dumps(hello))
    types = [e['type'] for e in events]
    check(types.count('drag_start') == 1 and types.count('drag_end') == 1,
          'exactly one drag_start and one drag_end: %s' % {t: types.count(t) for t in sorted(set(types))})
    start = next((e for e in events if e['type'] == 'drag_start'), {})
    end = next((e for e in events if e['type'] == 'drag_end'), {})
    sx, sy = truth['source']
    check(abs(start.get('x', -99) - sx) < 40 and abs(start.get('y', -99) - sy) < 40,
          'drag_start near the source at %s,%s: x=%s y=%s kind=%s' % (sx, sy, start.get('x'), start.get('y'),
                                                                      start.get('kind')))
    check(start.get('kind') in ('files', 'unknown'), 'drag_start kind is files or unknown: %s' % start.get('kind'))
    over = [e for e in events if e['type'] == 'drag_move' and e['window']
            and e['window']['id'] == truth['window']]
    check(len(over) >= 3, '%d drag_move events over the xterm window %s' % (len(over), truth['window']))
    window = over[-1]['window'] if over else {}
    check(window.get('pid') == truth['xterm_pid'], 'window.pid %s == xterm pid %s' % (window.get('pid'),
                                                                                      truth['xterm_pid']))
    check(window.get('app') == 'XTerm', 'window.app: %s' % window.get('app'))
    clients = window.get('clients') or []
    match = [c for c in clients if c['pid'] == truth['client_pid']]
    check(len(match) == 1, 'herdr client pid %s among %s' % (truth['client_pid'],
                                                            [(c['pid'], c['argv']) for c in clients]))
    client = match[0] if match else {}
    check(client.get('tty') == truth['client_tty'], 'client tty %s == %s' % (client.get('tty'), truth['client_tty']))
    check(client.get('cols') == 120 and client.get('rows') == 36,
          'client grid %sx%s == 120x36 (xterm -geometry)' % (client.get('cols'), client.get('rows')))
    check(client.get('foreground') is True, 'client foreground: %s' % client.get('foreground'))
    b = window.get('bounds', {})
    tx, ty = truth['target']
    check(b and b['x'] <= tx < b['x'] + b['width'] and b['y'] <= ty < b['y'] + b['height'],
          'bounds %s contain the drop point %s,%s' % (b, tx, ty))
    check(end.get('reason') == 'released' and abs(end.get('x', -99) - (tx + 60)) <= 2,
          'drag_end released at the drop point: %s' % json.dumps(end))
    clicks = [e for e in events if e['type'] == 'click']
    check(len(clicks) == 1 and clicks[0]['window'] and clicks[0]['window']['id'] == truth['window'],
          'one click on the xterm window: %s' % [(c['x'], c['y'], c['window'] and c['window']['id']) for c in clicks])
    exit_note = out.joinpath('sensor-exit.txt').read_text().strip()
    check(exit_note == 'sensor exited with 0', 'stdin EOF: %s' % exit_note)
    print('RESULT: %s' % ('OK' if not failures else '%d FAILED' % len(failures)))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else '/out'))
