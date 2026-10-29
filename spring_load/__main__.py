"""python3 -m spring_load run|status [--tab-dwell-ms N] [--pane-dwell-ms N]"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

if sys.version_info < (3, 11):   # on Windows herdr runs this with whatever `python` is first on PATH
    sys.exit('herdr-spring-load needs Python 3.11 or newer (for tomllib).')

from .engine import Engine, Settings
from .lock import held, try_lock
from .paths import log_file, support_dir
from .sensor import SensorError, SensorProcess, sensor_command

LOG_LIMIT = 1_000_000
_log_lock = threading.Lock()

def make_log(path):
    def log(message):
        line = time.strftime('%Y-%m-%dT%H:%M:%S') + ' ' + message + '\n'
        with _log_lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists() and path.stat().st_size > LOG_LIMIT:
                    os.replace(path, path.with_suffix('.log.old'))
                with open(path, 'a') as out:
                    out.write(line)
            except OSError:
                pass
    return log

def dwell(text):
    value = int(text)
    if not 50 <= value <= 5000:
        raise argparse.ArgumentTypeError('wants 50..5000 ms')
    return value / 1000

def run(args):
    support = support_dir()
    log = make_log(log_file())
    argv = sensor_command(support)
    if argv is None:
        sys.exit('herdr-spring-load: no sensor is installed for this system; run the plugin setup again')
    # One helper per user. Held until exit; setup, uninstall and status read it
    # (on Windows, where no service manager tracks the process, also its pid).
    support.mkdir(parents=True, exist_ok=True)
    instance = open(support/'helper.lock', 'a')
    if not try_lock(instance):
        log('another herdr-spring-load helper is running; this one exits')
        return
    (support/'helper.pid').write_text(str(os.getpid()))
    sensor = None
    def uninstall():
        state = json.loads((support/'state.json').read_text(encoding='utf-8'))
        if sensor is not None:
            sensor.close()   # Windows cannot delete a running sensor executable
        return subprocess.run([state['python'], state['uninstall'], '--self'], stdin=subprocess.DEVNULL, timeout=120).returncode
    while True:
        sensor = SensorProcess(argv, log=log)
        engine = Engine(sensor, support, settings=Settings(args.tab_dwell, args.pane_dwell), log=log, uninstall=uninstall)
        try:
            log(f'watching file drags with {argv[0]}')
            engine.run()
            if engine.removed or engine.status['state'] == 'inert':
                return
        except SensorError as error:
            log(f'sensor stopped: {error}')
            engine.report('restarting', f'sensor stopped: {error}')
        finally:
            sensor.close()
        time.sleep(2)

def status(args):
    path = support_dir()/'status.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        print('herdr-spring-load: no status yet. Is the helper running? Open the "Set up herdr-spring-load" pane to (re)install it.')
        return
    age = time.time() - data.get('updated', 0)
    running = held(support_dir()/'helper.lock')
    print(f'herdr-spring-load: {data.get("state") if running else "not running"} '
          f'(sensor {data.get("sensor") or "?"}, updated {int(age)} s ago)')
    if not running:
        print('  the helper is not running; open the "Set up herdr-spring-load" pane to start it again')
    if data.get('reason'):
        print(f'  last note: {data["reason"]}')
    for kind, why in (data.get('refused') or {}).items():
        print(f'  {kind} targets off: {why}')
    print(f'  calibrated window shapes: {data.get("calibrated", 0)}'
          + ('' if data.get('calibrated') else ' (to turn on tabs and Spaces, click a different herdr tab once, or a different Space twice,'
             ' without dragging anything)'))
    action = data.get('last_action')
    if action:
        print(f'  last navigation: {action["kind"]} {action["id"]} at {time.strftime("%H:%M:%S", time.localtime(action["time"]))}')
    print(f'  log: {log_file()}')

def main(argv=None):
    parser = argparse.ArgumentParser(prog='python3 -m spring_load')
    sub = parser.add_subparsers(dest='command', required=True)
    runner = sub.add_parser('run', help='watch drags (started by the login service)')
    runner.add_argument('--tab-dwell-ms', dest='tab_dwell', type=dwell, default=0.6)
    runner.add_argument('--pane-dwell-ms', dest='pane_dwell', type=dwell, default=0.25)
    sub.add_parser('status', help='print what the helper is doing and why')
    args = parser.parse_args(argv)
    {'run': run, 'status': status}[args.command](args)

if __name__ == '__main__':
    main()
