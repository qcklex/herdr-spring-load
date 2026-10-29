"""Windows end-to-end against a real herdr 0.9.3 (CI only):

    python tests/windows/e2e.py

In a scratch profile it starts a herdr client in a classic console window
(conhost.exe started explicitly, never the default-terminal handoff; the
server autospawns), then checks, with the real Win32 calls (RealWin32, no fakes):

1. the sensor's process reads on this process itself: process list, argv,
   PEB environment, start time;
2. the herdr client as the sensor describes it: argv, PEB environment (the
   scratch XDG_CONFIG_HOME), start time and console size (through the
   --console-probe child), and all_clients;
3. workspace.focus, tab.focus and pane.focus round-trip over the named pipe;
4. the real console window as the sensor describes it: a non-zero text
   area (xpixel/ypixel) for the client, and a grid the core's geometry.fit
   accepts for the window's real bounds;
5. the core, fed that real window description, focuses a Space, a tab and
   a pane through the API with one client attached;
6. with a second client attached, it refuses;
7. a client under ConPTY (a pseudo console this test creates, as Windows
   Terminal does) reports pixel size 0, and the core stays idle with the
   documented status: no client matches the window, the terminal reports no
   pixel size. The terminal window around it is synthesized (a Windows
   Terminal window class owned by this process, the ConPTY host).

Pointer events are scripted (FeedSensor, as tests/test_isolated_herdr.py),
and the calibration is seeded from the console's real client origin
(ClientToScreen) instead of learned from a click, since the test cannot
click into the console. Nothing else is patched."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import shutil
import sys
import threading
import time
import tomllib

from common import ROOT, CONFIG, Check, profile_env, require_windows, scratch, wait, write_config

require_windows()
from launch import ClassicClient, ConPtyClient
from spring_load import contract, engine as core, geometry, herdr_api, render
from sensors.windows import logic, sensor as windows_sensor
from sensors.windows.win32 import RealWin32

T0 = time.time()

def client_origin(hwnd):
    """Screen position of a window's client area (physical pixels: this
    process is per-monitor DPI aware once the sensor's reads have run)."""
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    point = wintypes.POINT(0, 0)
    return (point.x, point.y) if user32.ClientToScreen(hwnd, ctypes.byref(point)) else None

class FeedSensor:
    def __init__(self):
        self.queue = queue.Queue()

    def put(self, event):
        self.queue.put(contract.validate_event(dict(event, t=time.monotonic())))

    def next(self, timeout=None):
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

def main():
    check = Check('e2e')
    herdr = shutil.which('herdr')
    check.ok('herdr.exe on PATH', herdr, herdr)
    win = RealWin32(probe_argv=[sys.executable, str(ROOT/'sensors/windows/run_sensor.py'), '--console-probe'])

    # 1. The sensor's reads on this process.
    check.note('per-monitor DPI awareness', win.set_dpi_awareness())
    check.note('virtual screen', win.virtual_screen())
    check.note('drag threshold', win.drag_threshold())
    check.note('cursor position', win.cursor_pos())
    processes = win.processes()
    check.ok('process snapshot lists this process', processes and any(p.pid == os.getpid() for p in processes),
             f'{len(processes or [])} processes')
    own_argv = win.argv(os.getpid())
    check.ok('argv of this process', own_argv and own_argv[-1].endswith('e2e.py'), own_argv)
    own_env = win.environment(os.getpid())
    check.ok('PEB environment of this process', own_env is not None and own_env.get('LOCALAPPDATA') == os.environ.get('LOCALAPPDATA'),
             own_env)
    started = win.started(os.getpid())
    check.ok('start time of this process', started is not None and T0 - 60 < started <= T0 + 1,
             f'started {started}, script began {T0}')
    check.note('logon session', win.own_session())

    base = scratch('e2e')
    env = profile_env(base)
    config = write_config(env)
    sock = str(config.parent/'herdr.sock')
    api = lambda method, **params: herdr_api.request(sock, method, params, timeout=3)
    snap = lambda: herdr_api.snapshot(sock, timeout=3)
    first = second = pty = runner = None
    running = threading.Event()
    try:
        first = ClassicClient(herdr, env, str(base), win.processes)
        check.ok('herdr started by an explicit conhost.exe', first.pid, {'conhost': first.host.pid, 'herdr': first.pid})
        check.ok('isolated server answers over the named pipe',
                 wait(lambda: api('workspace.list').get('workspaces'), 20, (OSError, ValueError, KeyError, herdr_api.ApiError)),
                 herdr_api.pipe_name(sock))
        check.ok('marker file at the socket path', Path(sock).exists(), sock)

        # 2. The client as the real sensor reads it.
        def herdr_processes():
            return [p for p in win.processes() or [] if p.exe.lower() in logic.HERDR_NAMES]
        check.ok('client process listed', wait(lambda: any(p.pid == first.pid for p in herdr_processes())), first.pid)
        size = wait(lambda: win.console_size(first.pid), 10)
        check.ok('console size and console window through the probe child', size and size[2], size)
        cols, rows, console_hwnd, font_cell = size
        check.ok('console reports positive font-cell dimensions',
                 font_cell and all(v > 0 for v in font_cell), font_cell)
        argv = win.argv(first.pid)
        check.ok('client argv', argv and Path(argv[0]).name.lower() == 'herdr.exe' and len(argv) == 1, argv)
        client_env = win.environment(first.pid)
        check.ok('client PEB environment names the scratch profile',
                 client_env and client_env.get('XDG_CONFIG_HOME') == env['XDG_CONFIG_HOME'], client_env)
        client_started = win.started(first.pid)
        check.ok('client start time', client_started and client_started > config.stat().st_mtime, client_started)
        others = [p for p in herdr_processes() if p.pid != first.pid]
        check.note('other herdr processes (the server)', [(p.pid, win.argv(p.pid)) for p in others])
        owner = next(p for p in win.processes() if p.pid == os.getpid())
        found, reason = logic.find_clients(owner, None, win.processes(), win.created, win.argv, win.console_size,
                                           win.environment, win.started)
        check.ok('find_clients finds the client below this process', first.pid in [c['pid'] for c in found],
                 {'clients': found, 'reason': reason})
        reader = windows_sensor.Sensor(win)
        everyone = reader.all_clients()
        check.ok('all_clients lists the client with its environment',
                 any(c['pid'] == first.pid and c['env'] and c['env'].get('XDG_CONFIG_HOME') == env['XDG_CONFIG_HOME']
                     for c in everyone or []), everyone)

        # 4. The real console window: pixel size and geometry.
        console = win.window(console_hwnd)
        check.ok('the console window is a classic console window', console.cls == logic.CONSOLE_CLASS,
                 {'class': console.cls, 'pid': console.pid, 'bounds': console.bounds.as_json()})
        check.note('console window', {'pid': console.pid, 'client pid': first.pid, 'bounds': console.bounds.as_json(),
                                      'client area': win.text_area(console_hwnd), 'scale': win.scale(console_hwnd), 'raw font cell': font_cell,
                                      'text origin': win.text_origin(console_hwnd)})
        described = contract.validate_window(reader.describe(console, [], True))
        listed = [c for c in described['clients'] or [] if c['pid'] == first.pid]
        check.ok('the sensor finds the client behind the console window', listed,
                 {'clients': described['clients'], 'reason': reader.clients.get(console_hwnd, (None,) * 4)[3]})
        client = listed[0]
        check.ok('real console: non-zero pixel size', client['xpixel'] > 0 and client['ypixel'] > 0,
                 {k: client[k] for k in ('cols', 'rows', 'xpixel', 'ypixel', 'foreground')})
        bounds = described['bounds']
        grid = geometry.fit((bounds['width'], bounds['height']), client, described['scale'])
        check.note('measured text grid', {'font cell': font_cell, 'pixels': (client['xpixel'], client['ypixel']),
                                           'origin': client.get('grid_origin'), 'scale': described['scale']})
        check.ok('core geometry fits the real console window', grid and (grid.cols, grid.rows) == (cols, rows),
                 {'grid': grid, 'bounds': bounds, 'leftover': (bounds['width'] - client['xpixel'],
                                                             bounds['height'] - client['ypixel'])})
        origin = client_origin(console_hwnd)
        check.ok('ClientToScreen of the console window', origin, origin)
        ox, oy = origin[0] - bounds['x'], origin[1] - bounds['y']
        check.ok('the real text origin lies in the fitted grid',
                 grid.origin_x.low - 0.5 <= ox <= grid.origin_x.high + 0.5 and grid.origin_y.low - 0.5 <= oy <= grid.origin_y.high + 0.5,
                 {'origin': (ox, oy), 'grid x': grid.origin_x, 'grid y': grid.origin_y})
        cell_w, cell_h = grid.cell_width, grid.cell_height

        # 3. The public focus calls round-trip.
        project = snap()['focused_workspace_id']
        api('workspace.rename', workspace_id=project, label='project')
        project_2 = api('tab.create', workspace_id=project, label='2', focus=False)['tab']['tab_id']
        assets = api('workspace.create', cwd=str(base), label='assets', focus=False)['workspace']['workspace_id']
        files = api('tab.create', workspace_id=assets, label='files', cwd=str(base), focus=False)
        files_tab, files_root = files['tab']['tab_id'], files['root_pane']['pane_id']
        destination = api('pane.split', target_pane_id=files_root, direction='right', focus=False)['pane']['pane_id']
        for kind, target, key in (('space', assets, 'focused_workspace_id'), ('tab', files_tab, 'focused_tab_id'),
                                  ('pane', destination, 'focused_pane_id'), ('pane', files_root, 'focused_pane_id'),
                                  ('space', project, 'focused_workspace_id'), ('tab', project_2, 'focused_tab_id')):
            herdr_api.focus(sock, kind, target, timeout=3)
            check.ok(f'{herdr_api.FOCUS_METHODS[kind][0]} {target} round-trips', wait(lambda: snap()[key] == target, 5))

        # 5. The core with one client, on the sensor's description of the real window.
        def window(clients):
            return dict(described, bounds=dict(bounds), clients=clients)
        def point(rect):
            return (bounds['x'] + ox + (rect.x + rect.width / 2) * cell_w,
                    bounds['y'] + oy + (rect.y + rect.height / 2) * cell_h)
        support = base/'support'
        support.mkdir()
        key = geometry.calibration_key(described['app'], (bounds['width'], bounds['height']), client)
        (support/'calibration.json').write_text(json.dumps(
            {key: {'origin': [ox - 0.5, ox + 0.5, oy - 0.5, oy + 0.5], 'candidate': None}}))
        ui = render.ui_from_config(tomllib.loads(CONFIG))
        model = lambda: render.build_model(cols, rows, snap(), ui, render.Preferences())
        feed = FeedSensor()
        engine = core.Engine(feed, support, env=env, list_processes=lambda: None)
        feed.put({'type': 'hello', 'contract': 1, 'sensor': 'windows', 'version': '0.3.0', 'coords': 'windows-virtual',
                  'capabilities': ['drag', 'click', 'window', 'clients']})
        runner = threading.Thread(target=lambda: engine.run(until=running.is_set), daemon=True)
        runner.start()
        check.ok('core subscribes to the session over the pipe', wait(lambda: sock in map(str, engine.subscriptions), 10),
                 [str(s) for s in engine.subscriptions])

        def hold(target, done, seconds=4.0):
            until = time.time() + seconds
            while time.time() < until:
                feed.put({'type': 'drag_move', 'x': target[0], 'y': target[1], 'window': window([client])})
                if done():
                    return True
                time.sleep(0.05)
            return False

        feed.put({'type': 'drag_start', 'x': 0, 'y': 0, 'kind': 'files', 'window': None, 'all_clients': everyone})
        space = wait(lambda: model().find('space', assets))
        check.ok('core focuses the Space (workspace.focus)',
                 hold(point(space.rect), lambda: snap()['focused_workspace_id'] == assets), engine.status)
        tab = wait(lambda: model().find('tab', files_tab))
        check.ok('core focuses the tab (tab.focus)', hold(point(tab.rect), lambda: snap()['focused_tab_id'] == files_tab),
                 engine.status)
        target = destination if snap()['focused_pane_id'] != destination else files_root
        pane = wait(lambda: model().find('pane', target))
        check.ok('core focuses the pane (pane.focus)', pane and hold(point(pane.rect), lambda: snap()['focused_pane_id'] == target),
                 engine.status)
        feed.put({'type': 'drag_end', 'x': 0, 'y': 0, 'reason': 'released'})

        # 6. A second client: refuse.
        second = ClassicClient(herdr, env, str(base), win.processes)
        def both():
            listed = [c for c in reader.all_clients() or [] if len(c['argv']) == 1]
            return listed if len(listed) == 2 else None
        everyone = wait(both, 15)
        check.ok('all_clients sees both clients', everyone, everyone)
        time.sleep(1.2)   # past the core's after-drag window
        before = snap()['focused_workspace_id']
        feed.put({'type': 'drag_start', 'x': 0, 'y': 0, 'kind': 'files', 'window': None, 'all_clients': everyone})
        back = model().find('space', project)
        changed = hold(point(back.rect), lambda: snap()['focused_workspace_id'] != before, 2.5)
        feed.put({'type': 'drag_end', 'x': 0, 'y': 0, 'reason': 'released'})
        check.ok('two clients: the core does not act', not changed)
        check.ok('two clients: the status says why', '2 herdr clients are attached' in (engine.status['reason'] or ''),
                 engine.status['reason'])

        # 7. ConPTY (the Windows Terminal limit): pixel size 0, the core stays idle.
        first.close()
        second.close()
        check.ok('classic clients closed', wait(lambda: not [p for p in herdr_processes() if p.pid in (first.pid, second.pid)]))
        pty = ConPtyClient(herdr, env, str(base), cols=120, rows=30)
        size = wait(lambda: win.console_size(pty.pid), 10)
        check.ok('ConPTY client: console size through the probe child', size and size[:2] == (120, 30), size)
        pseudo = win.window(size[2]) if size[2] else None
        check.ok('ConPTY client: its console window is the pseudo window',
                 pseudo and pseudo.cls == 'PseudoConsoleWindow', pseudo and {'class': pseudo.cls, 'bounds': pseudo.bounds.as_json()})
        host = logic.TopWindow(hwnd=0x7FFF0001, pid=os.getpid(), tid=0, cls='CASCADIA_HOSTING_WINDOW_CLASS',
                               bounds=logic.Rect(100, 50, 120 * 8 + 2, 30 * 16 + 40))
        reader.clients.clear()
        conpty = contract.validate_window(reader.describe(host, [], True))
        listed = [c for c in conpty['clients'] or [] if c['pid'] == pty.pid]
        check.ok('ConPTY client: the sensor finds it below its host', listed,
                 {'clients': conpty['clients'], 'reason': reader.clients.get(host.hwnd, (None,) * 4)[3]})
        client = listed[0]
        check.ok('ConPTY client: pixel size 0', (client['xpixel'], client['ypixel']) == (0, 0),
                 {k: client[k] for k in ('cols', 'rows', 'xpixel', 'ypixel', 'foreground')})
        check.ok('ConPTY client: shown as the foreground client (one session), so only the pixel size refuses',
                 client['foreground'] is True, client['foreground'])
        check.ok('ConPTY client: no core geometry',
                 geometry.fit((conpty['bounds']['width'], conpty['bounds']['height']), client, conpty['scale']) is None)
        everyone = wait(lambda: (lambda found: found if any(c['pid'] == pty.pid for c in found) else None)(
            [c for c in reader.all_clients() or [] if len(c['argv']) == 1]), 10)
        check.ok('ConPTY client: all_clients lists it alone', everyone and len(everyone) == 1, everyone)
        time.sleep(1.2)
        before = (snap()['focused_workspace_id'], snap()['focused_tab_id'], snap()['focused_pane_id'])
        middle = (host.bounds.x + host.bounds.width / 2, host.bounds.y + host.bounds.height / 2)
        feed.put({'type': 'drag_start', 'x': middle[0], 'y': middle[1], 'kind': 'files', 'window': conpty,
                  'all_clients': everyone})
        until = time.time() + 2.5
        while time.time() < until:
            feed.put({'type': 'drag_move', 'x': middle[0], 'y': middle[1], 'window': conpty})
            time.sleep(0.05)
        during = engine.status.get('reason') or ''
        feed.put({'type': 'drag_end', 'x': middle[0], 'y': middle[1], 'reason': 'released'})
        after = (snap()['focused_workspace_id'], snap()['focused_tab_id'], snap()['focused_pane_id'])
        check.ok('ConPTY client: the core does not act', after == before, {'before': before, 'after': after})
        check.ok('ConPTY client: the status says the terminal reports no pixel size',
                 'reports no pixel size' in during, during)
        check.ok('ConPTY client: the core is idle after the drag', wait(lambda: engine.status['state'] == 'idle', 3),
                 engine.status)
    finally:
        running.set()
        if runner:
            runner.join(3)
        try:
            api('server.stop')
        except (OSError, ValueError, herdr_api.ApiError):
            pass
        for client in (first, second, pty):
            if client:
                client.close()
    check.done({'patched': ['seeded calibration (from ClientToScreen)']})
    print('windows e2e: real Win32 reads, real console pixel size and geometry, focus round-trip, '
          'core with one client, refusal with two, ConPTY inert with a clear status PASS')

if __name__ == '__main__':
    main()
