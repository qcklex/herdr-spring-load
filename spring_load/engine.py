"""The spring-loading loop: sensor events in, at most one herdr focus call per
dwell out. It never touches the drag itself; the terminal and herdr take the
file wherever the pointer is released."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import time
import tomllib

from . import clients, geometry, herdr_api, registration, render
from .sensor import SensorError

EVENT_KIND = {'workspace_focused': 'space', 'tab_focused': 'tab', 'pane_focused': 'pane'}
EVENT_ID = {'space': 'workspace_id', 'tab': 'tab_id', 'pane': 'pane_id'}
LEVEL = {'space': 0, 'tab': 1, 'pane': 2}
CLICK_WINDOW = 0.4   # seconds after a click in which herdr's focus event must arrive
MODEL_TTL = 1.0      # re-read herdr state at least this often during a drag

@dataclass
class Settings:
    tab_dwell: float = 0.6
    pane_dwell: float = 0.25

class Dwell:
    """A target fires once after the pointer has stayed on it for its dwell
    time, and re-arms only after the pointer leaves it."""
    def __init__(self, settings):
        self.settings = settings
        self.reset()

    def reset(self):
        self.target = None
        self.since = 0.0
        self.fired = False

    def update(self, target, now):
        key = (target.kind, target.id) if target else None
        if key != self.target:
            self.target, self.since, self.fired = key, now, False
            return None
        if target is None or self.fired:
            return None
        needed = self.settings.pane_dwell if target.kind == 'pane' else self.settings.tab_dwell
        if now - self.since < needed:
            return None
        self.fired = True
        return target

@dataclass
class Context:
    """One hovered window bound to one herdr client."""
    window_id: str
    client_pid: int
    binding: clients.Binding
    grid: geometry.Grid
    key: str
    model: render.Model
    snapshot: dict
    read_at: float
    bounds: tuple

class Unavailable(Exception):
    """Spring-loading cannot act on this window; the message says why."""

class Engine:
    def __init__(self, sensor, support_dir, env=None, settings=None, log=None, clock=time.monotonic,
                 list_processes=clients.list_clients_ps, uninstall=None, platform=None):
        self.sensor = sensor
        self.support = Path(support_dir)
        self.env = dict(os.environ if env is None else env)
        self.settings = settings or Settings()
        self.log = log or (lambda message: None)
        self.clock = clock
        self.list_processes = list_processes
        self.uninstall = uninstall
        self.platform = platform or sys.platform
        self.calibration = geometry.Calibration(self.support/'calibration.json')
        self.dwell = Dwell(self.settings)
        self.events = queue.Queue()
        self.subscriptions = {}
        self.configs = {}
        self.status = {'state': 'starting', 'reason': None, 'sensor': None, 'last_action': None, 'calibrated': 0}
        self.dragging = False
        self.everyone = None
        self.context = None
        self.pending_click = None
        self.hover = None
        self.removed = False

    # MARK: status

    def report(self, state=None, reason=None, **extra):
        changed = False
        if state and self.status['state'] != state:
            self.status['state'] = state
            changed = True
        if reason != self.status['reason']:
            self.status['reason'] = reason
            changed = True
            if reason:
                self.log(reason)
        for key, value in extra.items():
            if self.status.get(key) != value:
                self.status[key] = value
                changed = True
        if changed:
            self.write_status()

    def write_status(self):
        data = dict(self.status, updated=time.time(), calibrated=len(self.calibration.data))
        try:
            self.support.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix='status.', dir=self.support)
            with os.fdopen(fd, 'w') as out:
                json.dump(data, out, indent=1)
            os.replace(temp, self.support/'status.json')
        except OSError:
            pass

    # MARK: loop

    def run(self, until=None):
        hello = self.sensor.next(timeout=10)
        if hello is None:
            raise SensorError('the sensor sent no hello')
        self.hello = hello
        if 'drag' not in hello['capabilities'] or 'clients' not in hello['capabilities']:
            self.report('inert', f'the {hello["sensor"]} sensor cannot report drags and herdr clients')
            return
        self.report('idle', None, sensor=f'{hello["sensor"]} {hello["version"]}')
        try:
            while not self.removed and (until is None or not until()):
                event = self.sensor.next(timeout=0.03)
                if event is not None:
                    self.handle(event)
                self.drain_herdr_events()
                self.tick()
        finally:
            for subscription in self.subscriptions.values():
                subscription.close()
            self.subscriptions.clear()

    def handle(self, event):
        kind = event['type']
        if kind == 'drag_start':
            self.start_drag(event)
        elif kind == 'drag_move' and self.dragging:
            self.move(event)
        elif kind == 'drag_end':
            self.end_drag()
        elif kind == 'click' and not self.dragging:
            self.click(event)
        elif kind == 'status' and event['level'] in ('warn', 'error'):
            self.report(reason=f'sensor: {event["message"]}')

    def start_drag(self, event):
        self.end_drag()
        if event.get('kind') == 'other':
            return
        if not self.registration_allows():
            return
        self.dragging = True
        self.everyone = event.get('all_clients')
        if self.everyone is None:
            self.everyone = self.list_processes()
        self.report('dragging', None)
        self.move(event)

    def end_drag(self):
        self.dragging = False
        self.everyone = None
        self.context = None
        self.hover = None
        self.dwell.reset()
        if self.status['state'] == 'dragging':
            self.report('idle', self.status['reason'])

    def move(self, event):
        window = event.get('window')
        try:
            if window is None:
                raise Unavailable(None)
            context = self.context_for(window, event['x'], event['y'])
            target = geometry.resolve(context.model, context.grid, event['x'] - window['bounds']['x'], event['y'] - window['bounds']['y'])
            if target is not None and self.is_focused(context.snapshot, target):
                target = None
            self.hover = target
            self.report(reason=None)
        except Unavailable as why:
            self.hover = None
            if why.args[0]:
                self.report(reason=str(why))
        self.tick()

    def tick(self):
        if not self.dragging:
            return
        fire = self.dwell.update(self.hover, self.clock())
        if fire is None or self.context is None:
            return
        context = self.context
        try:
            herdr_api.focus(context.binding.api_socket, fire.kind, fire.id, timeout=0.5)
        except (OSError, herdr_api.ApiError) as error:
            self.report(reason=f'herdr refused {fire.kind} focus: {error}')
            return
        self.log(f'focused {fire.kind} {fire.id}')
        self.report(last_action={'kind': fire.kind, 'id': fire.id, 'time': time.time()})
        context.read_at = -1e9   # the layout changed: re-read before the next target
        self.hover = None
        self.dwell.reset()

    # MARK: binding a window to a client

    def config_ui(self, path):
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            return render.ui_from_config({})
        except OSError:
            raise Unavailable(f'cannot read {path}')
        cached = self.configs.get(path)
        if cached and cached[0] == stamp:
            return cached[1]
        try:
            ui = render.ui_from_config(tomllib.loads(path.read_text()))
        except (OSError, ValueError, UnicodeDecodeError):
            raise Unavailable(f'cannot parse {path}')
        self.configs[path] = (stamp, ui)
        return ui

    def bound_client(self, window):
        listed = window.get('clients')
        if listed is None:
            raise Unavailable('the sensor cannot see the processes behind this window')
        bounds = (window['bounds']['width'], window['bounds']['height'])
        locals_ = [c for c in listed if (clients.classify(c['argv']) or ('',))[0] == 'local' and c.get('foreground') is not False]
        if not locals_:
            raise Unavailable(None)   # not a herdr window: nothing to say
        fitting = [(c, g) for c in locals_ for g in [geometry.fit(bounds, c, window.get('scale'))] if g]
        if len(fitting) != 1:
            raise Unavailable('cannot tell which herdr client this window shows' if fitting else
                              'no herdr client in this window matches its size (or the terminal reports no pixel size)')
        for peer in window.get('peers', []):
            if abs(peer['width'] - bounds[0]) <= 2 and abs(peer['height'] - bounds[1]) <= 2:
                raise Unavailable('another window of this terminal has the same size; it is ambiguous which one shows herdr')
        return fitting[0]

    def context_for(self, window, x, y):
        client, grid = self.bound_client(window)
        bounds = (window['bounds']['width'], window['bounds']['height'])
        context = self.context
        now = self.clock()
        if context and context.window_id == window['id'] and context.client_pid == client['pid'] and context.bounds == bounds \
                and now - context.read_at < MODEL_TTL:
            return context
        try:
            binding = clients.bind(client, self.env, self.platform)
        except clients.Unbound as why:
            raise Unavailable(str(why))
        everyone = self.everyone if self.everyone is not None else []
        if self.everyone is None:
            raise Unavailable('cannot list herdr clients on this system')
        count = clients.attached_count(binding, everyone + [c for c in [client] if c['pid'] not in {e['pid'] for e in everyone}],
                                       self.env, self.platform)
        if count != 1:
            raise Unavailable(f'{count} herdr clients are attached to this session; herdr focus calls would move all of them, '
                              'so spring-loading only works with exactly one')
        model, snapshot = self.read_model(binding, client)
        key = geometry.calibration_key(window.get('app'), bounds, client)
        self.context = Context(window['id'], client['pid'], binding, self.calibration.apply(key, grid), key, model, snapshot, now, bounds)
        self.ensure_subscription(binding.api_socket)
        return self.context

    def read_model(self, binding, client):
        try:
            snapshot = herdr_api.snapshot(binding.api_socket, timeout=0.5)
        except (OSError, ValueError, herdr_api.ApiError) as error:
            raise Unavailable(f'herdr at {binding.api_socket} did not answer: {error}')
        ui = self.config_ui(binding.config_path)
        prefs_file = render.preferences_path(binding.client_socket, binding.state_dir)
        try:
            prefs = render.preferences_from_json(json.loads(prefs_file.read_text()))
        except FileNotFoundError:
            prefs = render.Preferences()
        except (OSError, ValueError):
            raise Unavailable(f'cannot read {prefs_file}')
        remote = render.remote_endpoints_listed(binding.state_dir)
        model = render.build_model(client['cols'], client['rows'], snapshot, ui, prefs, remote_endpoints=remote)
        if model.refused:
            self.report(refused=dict(model.refused))
        elif self.status.get('refused'):
            self.report(refused=None)
        return model, snapshot

    @staticmethod
    def is_focused(snapshot, target):
        return {'space': snapshot.get('focused_workspace_id'), 'tab': snapshot.get('focused_tab_id'),
                'pane': snapshot.get('focused_pane_id')}[target.kind] == target.id

    # MARK: calibration from ordinary clicks

    def ensure_subscription(self, api_socket):
        current = self.subscriptions.get(api_socket)
        if current and current.alive:
            return
        try:
            self.subscriptions[api_socket] = herdr_api.Subscription(
                api_socket, lambda event, s=api_socket: self.events.put((self.clock(), s, event)))
        except (OSError, ValueError, herdr_api.ApiError) as error:
            self.log(f'no focus events from {api_socket}: {error}')

    def click(self, event):
        window = event.get('window')
        if window is None:
            return
        try:
            client, grid = self.bound_client(window)
            binding = clients.bind(client, self.env, self.platform)
        except (Unavailable, clients.Unbound):
            return
        self.ensure_subscription(binding.api_socket)
        bounds = (window['bounds']['width'], window['bounds']['height'])
        self.pending_click = {'at': self.clock(), 'socket': binding.api_socket, 'binding': binding, 'client': client,
                              'grid': grid, 'key': geometry.calibration_key(window.get('app'), bounds, client),
                              'dx': event['x'] - window['bounds']['x'], 'dy': event['y'] - window['bounds']['y'], 'best': None}

    def drain_herdr_events(self):
        while True:
            try:
                at, socket_path, event = self.events.get_nowait()
            except queue.Empty:
                break
            click = self.pending_click
            kind = EVENT_KIND.get(event.get('event'))
            if not click or kind is None or socket_path != click['socket'] or at - click['at'] > CLICK_WINDOW:
                continue
            target_id = (event.get('data') or {}).get(EVENT_ID[kind])
            if target_id and (click['best'] is None or LEVEL[kind] < LEVEL[click['best'][0]]):
                click['best'] = (kind, target_id)
        click = self.pending_click
        if click and self.clock() - click['at'] > CLICK_WINDOW:
            self.pending_click = None
            if click['best'] and not self.dragging:
                self.learn(click)

    def learn(self, click):
        try:
            model, _ = self.read_model(click['binding'], click['client'])
        except Unavailable:
            return
        target = model.find(*click['best'])
        if target is None:
            return
        if self.calibration.learn(click['key'], click['grid'], click['dx'], click['dy'], target.rect):
            self.log(f'calibrated from a click on {target.kind} {target.id}')
            self.report(calibrated=len(self.calibration.data))

    # MARK: plugin registration

    def registration_allows(self):
        config_dir, _ = clients.herdr_dirs(self.env, self.platform)
        state = registration.registration(config_dir)
        seen = self.support/'registered-seen'
        if state == 'registered' and not seen.exists():
            try:
                self.support.mkdir(parents=True, exist_ok=True)
                seen.touch()
            except OSError:
                pass
        decision = registration.action(state, seen.exists())
        if decision == 'run':
            return True
        if decision == 'remove':
            self.remove_self()
            return False
        self.report('paused', f'spring-loading is paused: {registration.REASONS[state]}')
        return False

    def remove_self(self):
        self.report('removing', 'the plugin was uninstalled from herdr; removing the helper')
        if self.uninstall is None:
            return
        try:
            result = self.uninstall()
        except OSError as error:
            self.report('paused', f'could not remove the helper: {error}')
            return
        if result == 0:
            self.removed = True
        else:
            self.report('paused', 'could not remove the helper; run the plugin action "Remove herdr-spring-load helper"')
