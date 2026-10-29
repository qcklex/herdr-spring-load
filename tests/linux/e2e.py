#!/usr/bin/env python3
"""Linux end-to-end test, run inside the test image (tests/linux/run_e2e.sh).

Real pieces only: Xvfb and openbox, alacritty running the herdr 0.9.3 linux
binary, the plugin linked into herdr, the core (python3 -m spring_load run)
with the linux-x11 sensor, and a GTK file drag driven through XTest.

1. Session: space alpha (2 tabs, alpha-2 focused) and space beta (tabs beta-1
   and beta-2, beta-2 split into two panes); alpha is focused.
2. Calibration: one ordinary click on tab alpha-1.
3. One drag: hold on beta's sidebar row, then on tab beta-2, then on the
   unfocused pane of beta-2, then drop there. Every step is checked through
   the herdr API; the drop is checked passively by reading that pane.
4. Two clients: a second alacritty attaches to the same session; the same
   hover must change nothing.

The core's renderer is used only to know where to point; every assertion
reads herdr's own state."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

REPO = Path('/repo')
sys.path.insert(0, str(REPO))
from spring_load import clients, herdr_api, render

OUT = Path(os.environ.get('OUT', '/out'))
HOME = Path.home()
API = HOME/'.config/herdr/herdr.sock'
SUPPORT = HOME/'.local/share/herdr-spring-load'
DROP_FILE = '/tmp/drop-me.txt'
ENV = dict(os.environ, LIBGL_ALWAYS_SOFTWARE='1', PYTHONDONTWRITEBYTECODE='1')
results = []
processes = []


def check(condition, message):
    results.append((bool(condition), message))
    print(('PASS ' if condition else 'FAIL ') + message, flush=True)
    return condition


def spawn(argv, log, **kwargs):
    process = subprocess.Popen(argv, stdout=open(OUT/log, 'w'), stderr=subprocess.STDOUT, env=ENV, **kwargs)
    processes.append(process)
    return process


def sh(*argv):
    return subprocess.run(argv, capture_output=True, text=True, env=ENV, timeout=30).stdout


def wait(condition, timeout, step=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            value = condition()
        except (OSError, ValueError, herdr_api.ApiError):
            value = None
        if value:
            return value
        time.sleep(step)
    return None


def api(method, **params):
    return herdr_api.request(API, method, params, timeout=2)


def snap():
    return herdr_api.snapshot(API, timeout=2)


def screenshot(name):
    subprocess.run(['import', '-window', 'root', str(OUT/(name + '.png'))], capture_output=True, env=ENV)


# MARK: windows, clients and target positions

def geometry(window_id):
    """Root-relative bounds from xwininfo. (xdotool getwindowgeometry adds the WM frame offset twice.)"""
    info = {}
    for line in sh('xwininfo', '-id', window_id).splitlines():
        key, _, value = line.strip().partition(':')
        info[key] = value.strip()
    return {'x': int(info['Absolute upper-left X']), 'y': int(info['Absolute upper-left Y']),
            'width': int(info['Width']), 'height': int(info['Height'])}


def herdr_windows():
    """{window id: (pid, bounds)} of the alacritty windows."""
    found = {}
    for line in sh('xdotool', 'search', '--class', 'Alacritty').split():
        bounds = geometry(line)
        pid = sh('xdotool', 'getwindowpid', line).strip()
        if bounds['width'] > 50 and pid:
            found[line] = (int(pid), bounds)
    return found


def local_clients():
    sys.path.insert(0, str(REPO/'sensors/linux'))
    import common
    return [c for c in common.all_herdr_clients() if (clients.classify(c['argv']) or ('',))[0] == 'local']


def client_in(window_pid):
    sys.path.insert(0, str(REPO/'sensors/linux'))
    import common
    found = common.herdr_clients(window_pid)
    return found[0] if found else None


def model_for(client):
    binding = clients.bind(client, os.environ)
    snapshot = herdr_api.snapshot(binding.api_socket)
    import tomllib
    try:
        ui = render.ui_from_config(tomllib.loads(binding.config_path.read_text()))
    except FileNotFoundError:
        ui = render.ui_from_config({})
    try:
        prefs = render.preferences_from_json(json.loads(render.preferences_path(binding.client_socket,
                                                                              binding.state_dir).read_text()))
    except FileNotFoundError:
        prefs = render.Preferences()
    return render.build_model(client['cols'], client['rows'], snapshot, ui, prefs,
                              remote_endpoints=render.remote_endpoints_listed(binding.state_dir))


def point(window, client, rect, fx=0.5, fy=0.5):
    """Screen point inside cell rect `rect` of `client`'s grid in `window`."""
    _, bounds = window
    cell_w, cell_h = client['xpixel'] / client['cols'], client['ypixel'] / client['rows']
    column = rect.x + min(rect.width - 1, max(0, int(rect.width * fx)))
    row = rect.y + min(rect.height - 1, max(0, int(rect.height * fy)))
    return int(bounds['x'] + (column + 0.5) * cell_w), int(bounds['y'] + (row + 0.5) * cell_h)


def glide(x0, y0, x1, y1, steps=15, delay=0.03):
    for i in range(1, steps + 1):
        sh('xdotool', 'mousemove', str(x0 + (x1 - x0) * i // steps), str(y0 + (y1 - y0) * i // steps))
        time.sleep(delay)


def sensor_starts():
    try:
        return sum('"drag_start"' in line for line in (OUT/'sensor.ndjson').read_text().splitlines())
    except OSError:
        return 0


def status():
    try:
        return json.loads((SUPPORT/'status.json').read_text())
    except (OSError, ValueError):
        return {}


# MARK: the test

def setup_session():
    first = snap()
    alpha = first['focused_workspace_id']
    api('workspace.rename', workspace_id=alpha, label='alpha')
    alpha_1 = first['focused_tab_id']
    api('tab.rename', tab_id=alpha_1, label='alpha-1')
    alpha_2 = api('tab.create', workspace_id=alpha, label='alpha-2', focus=False)['tab']['tab_id']
    created = api('workspace.create', label='beta', focus=False, cwd=str(HOME))
    beta = created['workspace']['workspace_id']
    beta_1 = created['tab']['tab_id']
    api('tab.rename', tab_id=beta_1, label='beta-1')
    made = api('tab.create', workspace_id=beta, label='beta-2', focus=False)
    beta_2, left = made['tab']['tab_id'], made['root_pane']['pane_id']
    right = api('pane.split', target_pane_id=left, direction='right', focus=False)
    right = (right.get('pane') or right.get('new_pane') or {}).get('pane_id') or next(
        p['pane_id'] for p in snap()['panes'] if p.get('tab_id') == beta_2 and p['pane_id'] != left)
    api('workspace.focus', workspace_id=alpha)
    api('tab.focus', tab_id=alpha_2)
    ids = dict(alpha=alpha, beta=beta, alpha_1=alpha_1, alpha_2=alpha_2, beta_1=beta_1, beta_2=beta_2,
               left=left, right=right)
    (OUT/'ids.json').write_text(json.dumps(ids, indent=1))
    return ids


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    os.chdir(HOME)
    spawn(['Xvfb', ':99', '-screen', '0', '1280x800x24', '-nolisten', 'tcp'], 'xvfb.log')
    wait(lambda: subprocess.run(['xdpyinfo'], capture_output=True, env=ENV).returncode == 0, 10)
    spawn(['openbox'], 'openbox.log')
    time.sleep(1)
    Path(DROP_FILE).write_text('drop me\n')
    spawn(['python3', str(REPO/'tests/linux/drag_source.py'), DROP_FILE], 'source.log')
    terminal = ['alacritty', '-o', 'window.dimensions.columns=110', '-o', 'window.dimensions.lines=36',
                '-o', 'window.position.x=200', '-o', 'window.position.y=120', '-e', 'herdr']
    spawn(terminal, 'alacritty-1.log')
    if not check(wait(lambda: API.exists() and snap(), 20), 'herdr is up in alacritty (%s)' % API):
        return
    time.sleep(2)
    ids = setup_session()
    print('session: ' + json.dumps(ids), flush=True)
    out = sh('herdr', 'plugin', 'link', str(REPO))
    check('plugin_linked' in out, 'herdr plugin link /repo registered the plugin')

    sensor = Path('/tmp/herdr-spring-load-sensor-linux')
    subprocess.run(['python3', str(REPO/'sensors/linux/build.py'), str(sensor)], check=True)
    # The core reads the sensor through tee, so the test keeps a copy of every event.
    tee = 'exec %s 2>%s | tee %s' % (sensor, OUT/'sensor.err', OUT/'sensor.ndjson')
    core_env = dict(ENV, SPRING_LOAD_SENSOR=json.dumps(['sh', '-c', tee]))
    core = subprocess.Popen(['python3', '-m', 'spring_load', 'run'], cwd=str(REPO), env=core_env,
                            stdout=open(OUT/'core.out', 'w'), stderr=subprocess.STDOUT)
    processes.append(core)
    check(wait(lambda: status().get('state') == 'idle', 15), 'core is idle with sensor %s' % status().get('sensor'))

    windows = herdr_windows()
    window_id, window = next(iter(windows.items()))
    client = client_in(window[0])
    check(client and client['xpixel'] > 0, 'client %s in window %s (pid %s): %sx%s cells, %sx%s px, bounds %s' % (
        client and client['pid'], window_id, window[0], client and client['cols'], client and client['rows'],
        client and client['xpixel'], client and client['ypixel'], window[1]))
    screenshot('0-setup')

    # 2. calibration: an ordinary click on tab alpha-1
    target = model_for(client).find('tab', ids['alpha_1'])
    if not check(target, 'renderer places tab alpha-1 (refused: %s)' % model_for(client).refused):
        return
    x, y = point(window, client, target.rect)
    seen = []
    recorder = herdr_api.Subscription(API, seen.append)
    sh('xdotool', 'mousemove', str(x), str(y))
    time.sleep(0.2)
    sh('xdotool', 'mousedown', '1')
    time.sleep(0.08)
    sh('xdotool', 'mouseup', '1')
    check(wait(lambda: snap()['focused_tab_id'] == ids['alpha_1'], 3), 'click focused tab alpha-1 (herdr API)')
    time.sleep(0.5)
    recorder.close()
    print('herdr events after the tab click: %s' % json.dumps([e.get('data') for e in seen]), flush=True)
    check(wait(lambda: status().get('calibrated', 0) >= 1, 3),
          'core learned calibration from the click: calibrated=%s' % status().get('calibrated'))
    # 3. one drag: Space row, then tab, then pane, then drop
    source = next(iter(sh('xdotool', 'search', '--name', 'drag-source').split()), None)
    box = geometry(source)
    sx, sy = box['x'] + box['width'] // 2, box['y'] + box['height'] // 2
    sh('xdotool', 'mousemove', str(sx), str(sy))
    time.sleep(0.3)
    sh('xdotool', 'mousedown', '1')
    space = model_for(client).find('space', ids['beta'])
    if not check(space, 'renderer places space beta (refused: %s)' % model_for(client).refused):
        sh('xdotool', 'mouseup', '1')
        return
    x1, y1 = point(window, client, space.rect, fy=0.0)
    glide(sx, sy, x1, y1)
    changed = wait(lambda: snap()['focused_workspace_id'] == ids['beta'], 3)
    screenshot('1-space')
    check(changed, 'hold on space beta row -> focused workspace is beta (herdr API: %s)' % snap()['focused_workspace_id'])
    tab = wait(lambda: model_for(client).find('tab', ids['beta_2']), 3)
    if check(tab, 'renderer places tab beta-2'):
        x2, y2 = point(window, client, tab.rect)
        glide(x1, y1, x2, y2)
        changed = wait(lambda: snap()['focused_tab_id'] == ids['beta_2'], 3)
        screenshot('2-tab')
        check(changed, 'hold on tab beta-2 -> focused tab is beta-2 (herdr API: %s)' % snap()['focused_tab_id'])
        before = snap()['focused_pane_id']
        other = ids['right'] if before == ids['left'] else ids['left']
        pane = wait(lambda: model_for(client).find('pane', other), 3)
        if check(pane, 'renderer places the unfocused pane %s (focused: %s)' % (other, before)):
            x3, y3 = point(window, client, pane.rect)
            glide(x2, y2, x3, y3)
            changed = wait(lambda: snap()['focused_pane_id'] == other, 3)
            screenshot('3-pane')
            check(changed, 'hold on pane %s -> focused pane is %s (herdr API: %s)' % (other, other,
                                                                                     snap()['focused_pane_id']))
            time.sleep(0.3)
            sh('xdotool', 'mouseup', '1')
            read = lambda pane_id: api('pane.read', pane_id=pane_id, source='visible', strip_ansi=True)['read']
            pasted = wait(lambda: 'drop-me.txt' in json.dumps(read(other)), 4)
            screenshot('4-drop')
            check(pasted, 'native drop pasted %s into pane %s (pane.read, passive)' % (DROP_FILE, other))
            check('drop-me.txt' not in json.dumps(read(before)), 'the previously focused pane %s did not get it' % before)
    sh('xdotool', 'mouseup', '1')
    time.sleep(0.5)
    last = status().get('last_action') or {}
    print('core status after drag: %s' % json.dumps({k: status().get(k) for k in ('state', 'reason', 'last_action')}),
          flush=True)
    check(last.get('kind') == 'pane', 'core reports its last navigation as a pane focus')

    # 4. two clients: nothing may change
    spawn(terminal[:5] + ['-o', 'window.position.x=60', '-o', 'window.position.y=60', '-e', 'herdr'], 'alacritty-2.log')
    if not check(wait(lambda: len(local_clients()) == 2, 15), 'second alacritty attached: %s local clients'
                 % len(local_clients())):
        return
    time.sleep(2)
    api('workspace.focus', workspace_id=ids['alpha'])
    # A fresh drag source: GTK did not start a second drag from the first one here.
    sh('pkill', '-f', 'drag_source.py')
    spawn(['python3', str(REPO/'tests/linux/drag_source.py'), DROP_FILE], 'source-2.log')
    wait(lambda: sh('xdotool', 'search', '--name', 'drag-source').split(), 10)
    time.sleep(1)
    source = sh('xdotool', 'search', '--name', 'drag-source').split()[0]
    box = geometry(source)
    sx, sy = box['x'] + box['width'] // 2, box['y'] + box['height'] // 2
    starts_before = sensor_starts()
    before = snap()
    sh('xdotool', 'mousemove', str(sx), str(sy))
    time.sleep(0.3)
    sh('xdotool', 'mousedown', '1')
    space = model_for(client).find('space', ids['beta'])
    x1, y1 = point(window, client, space.rect, fy=0.0)
    glide(sx, sy, x1, y1)
    time.sleep(2.0)
    after = snap()
    screenshot('5-two-clients')
    reason = status().get('reason') or ''
    check(sensor_starts() == starts_before + 1, 'sensor reported the two-client drag (drag_start)')
    sh('xdotool', 'mouseup', '1')
    time.sleep(0.5)
    check(after['focused_workspace_id'] == before['focused_workspace_id'] == ids['alpha'],
          'two clients: hold on space beta changes nothing (workspace %s)' % after['focused_workspace_id'])
    check('2 herdr clients' in reason, 'core says why: %r' % reason)


if __name__ == '__main__':
    try:
        main()
    finally:
        for name in ('status.json', 'calibration.json', 'herdr-spring-load.log'):
            try:
                (OUT/('core-' + name)).write_bytes((SUPPORT/name).read_bytes())
            except OSError:
                pass
        for process in reversed(processes):
            process.terminate()
        failed = [m for ok, m in results if not ok]
        print('RESULT: %s (%d checks)' % ('PASS' if results and not failed else 'FAIL', len(results)), flush=True)
        sys.exit(0 if results and not failed else 1)
