"""Run a sensor for a few seconds and validate what it printed (CI):

    python tests/windows/sensor_check.py [--seconds N] [--name NAME] -- COMMAND...

Closes the sensor's stdin, expects it to exit (contract: exit on EOF), then
pipes the recorded NDJSON through `python -m spring_load.contract` and checks
the hello: sensor windows, coords windows-virtual, and every capability
(drag included, so per-monitor DPI awareness was enabled)."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from common import ROOT, Check, require_windows, write_report

def record(command, seconds, out_path, env=None):
    """Run `command`, then close its stdin. Returns (exit code or None, stderr)."""
    with open(out_path, 'wb') as out:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.PIPE, env=env)
        time.sleep(seconds)
        process.stdin.close()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            return None, process.stderr.read().decode('utf-8', 'replace')
        return process.returncode, process.stderr.read().decode('utf-8', 'replace')

def validate(check, out_path, label):
    with open(out_path, 'rb') as stream:
        result = subprocess.run([sys.executable, '-m', 'spring_load.contract'], stdin=stream, cwd=ROOT,
                                capture_output=True, text=True, timeout=30)
    check.ok(f'{label}: python -m spring_load.contract', result.returncode == 0, result.stdout.strip() or result.stderr.strip())
    lines = [json.loads(l) for l in Path(out_path).read_text(encoding='utf-8').splitlines() if l.strip()]
    hello = lines[0]
    check.ok(f'{label}: hello names the Windows sensor', hello['sensor'] == 'windows' and hello['coords'] == 'windows-virtual',
             hello)
    check.ok(f'{label}: all four capabilities (DPI awareness on)',
             set(hello['capabilities']) == {'drag', 'click', 'window', 'clients'}, hello['capabilities'])
    errors = [e for e in lines if e.get('type') == 'status' and e.get('level') == 'error']
    check.ok(f'{label}: no error status', not errors, errors)
    return lines

def check_sensor(check, command, seconds, name, env=None):
    out_path = Path(ROOT/'tests'/'windows'/'out')
    out_path.mkdir(parents=True, exist_ok=True)
    out_path = out_path/f'{name}.ndjson'
    code, stderr = record(command, seconds, out_path, env)
    check.ok(f'{name}: exits on stdin EOF', code == 0, {'exit': code, 'stderr': stderr[-2000:]})
    lines = validate(check, out_path, name)
    check.note(f'{name}: events', [e['type'] for e in lines])
    return lines

def main():
    require_windows()
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=float, default=4)
    parser.add_argument('--name', default='sensor')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('no sensor command')
    check = Check(args.name)
    check_sensor(check, command, args.seconds, args.name)
    check.done()

if __name__ == '__main__':
    main()
