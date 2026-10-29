"""The spring-loading loop: sensor events in, at most one herdr focus call per
dwell out. It never touches the drag itself; the terminal and herdr take the
file wherever the pointer is released."""
from collections import deque
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
SNAPSHOT_ID = {'space': 'focused_workspace_id', 'tab': 'focused_tab_id', 'pane': 'focused_pane_id'}
LEVEL = {'space': 0, 'tab': 1, 'pane': 2}
# herdr focuses a clicked Space or tab on mouse up, and the sensor reports the
# click after it sees the release, so the focus event often arrives first.
# A click is matched against focus changes from CLICK_BEFORE before it to
# CLICK_AFTER after it.
CLICK_BEFORE = 0.5
CLICK_AFTER = 0.4
BURST_GAP = 0.15     # herdr's workspace, tab and pane events of one focus change
AFTER_DRAG = 1.0     # clicks right after a drag may meet the plugin's own focus calls
MODEL_TTL = 1.0      # re-read herdr state at least this often during a drag
SETTLE = 5.0         # seconds herdr's git cache may lag a changed cwd or HEAD
DISCOVER_EVERY = 10.0
ALL_KINDS = frozenset(('space', 'tab', 'pane'))
UNCONFIRMED = {
    'tab': "herdr's config changed after this herdr client started, so the plugin cannot tell which config it shows. "
           'Click a different tab twice (or restart the client) to confirm',
    'pane': "herdr's config changed after this herdr client started; click a different tab twice to confirm the panes",
    'space': "herdr's config changed after this herdr client started, and no click can prove which Space rows "
             'it shows: Spaces stay off until the herdr client restarts'}
REMOVAL_CONFIRM = 3.0

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
        self.hover = None
        self.removed = False
        self.focus = {}            # api socket -> {'space': id, 'tab': id, 'pane': id}, from events
        self.changes = deque(maxlen=256)   # (at, socket, kind, id) focus changes herdr reported
        self.pending_clicks = []
        self.last_drag_end = -1e9
        self.next_discover = -1e9
        self.verified = {}         # (client pid, config path) -> (config stamp, kinds clicks confirmed)
        self.stale = {}            # calibration key -> why the replica disagreed with herdr
        self.identities = {}       # (api socket, workspace id) -> (identity cwd, when it changed)
        self.starts = {}           # client pid -> process start (epoch seconds) or None
        self.missing_since = None

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
        data = dict(self.status, updated=time.time(), calibrated=self.calibration.calibrated())
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
                if self.clock() >= self.next_discover:
                    self.next_discover = self.clock() + DISCOVER_EVERY
                    self.discover_sessions()
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
        if self.dragging:
            self.last_drag_end = self.clock()
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
            ui = render.ui_from_config(tomllib.loads(path.read_text(encoding='utf-8')))
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
        # A terminal app that runs more sessions than it has windows has tabs or
        # splits: herdr in a hidden tab fits the window as well as a visible one
        # (review N10, the Windows sensor's one-session rule).
        sessions, windows = window.get('sessions'), 1 + len(window.get('peers', []))
        if isinstance(sessions, int) and sessions > windows:
            raise Unavailable(f'this terminal runs {sessions} sessions in {windows} window(s), so it has tabs or splits '
                              'and herdr may be in a hidden one; run herdr alone in its window (no tabs, no splits)')
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
        key = geometry.calibration_key(window.get('app'), bounds, client)
        now = self.clock()
        if context and context.window_id == window['id'] and context.client_pid == client['pid'] and context.bounds == bounds \
                and context.key == key and now - context.read_at < MODEL_TTL:
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
        key = geometry.calibration_key(window.get('app'), bounds, client)
        if key in self.stale:
            raise Unavailable(self.stale[key])
        allowed = self.check_config(binding, client)
        model, snapshot = self.read_model(binding, client)
        if allowed != ALL_KINDS:
            model.targets = [t for t in model.targets if t.kind in allowed]
            for kind in ALL_KINDS - allowed:
                model.refused.setdefault(kind, UNCONFIRMED[kind])
            self.report(refused=dict(model.refused))
        self.context = Context(window['id'], client['pid'], binding, self.calibration.apply(key, grid), key, model, snapshot, now, bounds)
        self.ensure_subscription(binding.api_socket)
        return self.context

    def identity_roots(self, api_socket, snapshot):
        path = Path(api_socket).parent/'session.json'
        try:
            # Persistence holds metadata only; avoid an unbounded read per move.
            if path.stat().st_size > 8_000_000:
                return {}
            with path.open(encoding='utf-8') as source:
                text = source.read(8_000_001)
            if len(text) > 8_000_000:
                return {}
            saved = json.loads(text)
        except (OSError, ValueError, UnicodeDecodeError):
            return {}
        return render.saved_identity_roots(snapshot, saved)

    def read_model(self, binding, client):
        try:
            snapshot = herdr_api.snapshot(binding.api_socket, timeout=0.5)
        except (OSError, ValueError, herdr_api.ApiError) as error:
            raise Unavailable(f'herdr at {binding.api_socket} did not answer: {error}')
        ui = self.config_ui(binding.config_path)
        prefs_file = render.preferences_path(binding.client_socket, binding.state_dir)
        try:
            prefs = render.preferences_from_json(json.loads(prefs_file.read_text(encoding='utf-8')))
        except FileNotFoundError:
            prefs = render.Preferences()
        except (OSError, ValueError):
            raise Unavailable(f'cannot read {prefs_file}')
        remote = render.remote_endpoints_listed(binding.state_dir)
        model = render.build_model(client['cols'], client['rows'], snapshot, ui, prefs, remote_endpoints=remote,
                                   branch_settled=lambda ws, cwd: self.branch_settled(binding.api_socket, ws, cwd),
                                   identity_roots=self.identity_roots(binding.api_socket, snapshot))
        if model.refused:
            self.report(refused=dict(model.refused))
        elif self.status.get('refused'):
            self.report(refused=None)
        return model, snapshot

    @staticmethod
    def is_focused(snapshot, target):
        return {'space': snapshot.get('focused_workspace_id'), 'tab': snapshot.get('focused_tab_id'),
                'pane': snapshot.get('focused_pane_id')}[target.kind] == target.id

    # MARK: which config the client shows (herdr has no API for it)

    def config_stamp(self, path):
        try:
            return path.stat().st_mtime
        except FileNotFoundError:
            try:
                return path.parent.stat().st_mtime   # a deleted config shows in its folder
            except OSError:
                return None
        except OSError:
            return None

    def client_start(self, client):
        started = client.get('started')
        if isinstance(started, (int, float)):
            return float(started)
        pid = client['pid']
        if pid not in self.starts:
            self.starts[pid] = clients.process_start(pid, self.platform)
        return self.starts[pid]

    def trusted_kinds(self, binding, client):
        try:
            return self.check_config(binding, client)
        except Unavailable:
            return frozenset()

    def check_config(self, binding, client):
        """Which target kinds the replica's config can be trusted for. herdr's
        client reads config.toml when it starts and on a reload, and no API
        says which. Everything is trusted when the file is older than the client
        process. Otherwise a calibration click that agreed with an origin stored
        earlier confirms only what it can prove: a tab click the tab bar and
        the panes below it. Space rows are never confirmed by clicks (review
        N9c): a replica Space shorter than herdr's lies inside herdr's, so a
        click contained in it proves nothing about the Spaces below."""
        stamp = self.config_stamp(binding.config_path)
        if stamp is None:
            raise Unavailable(f'cannot tell when {binding.config_path} last changed')
        start = self.client_start(client)
        if start is not None and stamp < start - 1:
            return ALL_KINDS
        confirmed = self.verified.get((client['pid'], str(binding.config_path)))
        allowed = frozenset(confirmed[1]) if confirmed and confirmed[0] == stamp else frozenset()
        if not allowed:
            raise Unavailable(UNCONFIRMED['tab'])
        return allowed

    def confirm_config(self, click, target, model):
        """The click contains the stored origin for `target`. A tab is one row
        high, so one such click proves the tab bar and the panes below it.
        Clicks never confirm Space rows (review N9b, N9c): spaces.rows and
        row_gap can shift, grow or shrink every Space below the first, and a
        replica Space shorter than herdr's lies inside it, so no contained
        click tells them apart."""
        stamp = self.config_stamp(click['binding'].config_path)
        if stamp is None:
            return
        key = (click['client']['pid'], str(click['binding'].config_path))
        previous = self.verified.get(key)
        kinds = set(previous[1]) if previous and previous[0] == stamp else set()
        if target.kind == 'tab':
            kinds |= {'tab', 'pane'}
        self.verified[key] = (stamp, kinds)

    def branch_settled(self, api_socket, workspace_id, cwd):
        """False while herdr's asynchronous git cache may lag the disk: the
        workspace's identity cwd changed, or its HEAD moved, within SETTLE seconds."""
        now = time.time()
        key = (api_socket, workspace_id)
        previous = self.identities.get(key)
        if previous is None or previous[0] != cwd:
            self.identities[key] = (cwd, -1e18 if previous is None else now)
        if now - self.identities[key][1] < SETTLE:
            return False
        head = render.git_head_path(cwd)
        try:
            if head is not None and now - head.stat().st_mtime < SETTLE:
                return False
        except OSError:
            pass
        return True

    # MARK: calibration from ordinary clicks

    def discover_sessions(self):
        """Subscribe to focus events of every session found in the core's own
        herdr directories before any click, so the first click is not lost."""
        config_dir, _ = clients.herdr_dirs(self.env, self.platform)
        sockets = [config_dir/'herdr.sock']
        sessions = config_dir/'sessions'
        try:
            if sessions.is_dir():
                sockets += sorted(p/'herdr.sock' for p in sessions.iterdir() if p.is_dir())
        except OSError:
            pass
        for path in sockets:
            if path.exists():
                self.ensure_subscription(path)

    def ensure_subscription(self, api_socket):
        current = self.subscriptions.get(api_socket)
        if current and current.alive:
            return
        try:
            self.subscriptions[api_socket] = herdr_api.Subscription(
                api_socket, lambda event, s=api_socket: self.events.put((self.clock(), s, event)))
            snapshot = herdr_api.snapshot(api_socket, timeout=0.5)
        except (OSError, ValueError, herdr_api.ApiError) as error:
            self.log(f'no focus events from {api_socket}: {error}')
            return
        # Baseline of every workspace's identity cwd, so a cd before the first drag
        # counts as a change (review M2).
        roots = self.identity_roots(api_socket, snapshot)
        for workspace in snapshot.get('workspaces', []):
            cwd = render.identity_cwd(snapshot, workspace.get('workspace_id'), roots)
            if cwd is not None:
                self.identities.setdefault((api_socket, workspace['workspace_id']), (cwd, -1e18))
        # Queued after any event that came first, so the main thread applies it in order.
        self.events.put((self.clock(), api_socket, {'seed': {k: snapshot.get(v) for k, v in SNAPSHOT_ID.items()}}))

    def click(self, event):
        window = event.get('window')
        if window is None or self.clock() - self.last_drag_end < AFTER_DRAG:
            return
        try:
            client, grid = self.bound_client(window)
            binding = clients.bind(client, self.env, self.platform)
        except (Unavailable, clients.Unbound):
            return
        self.ensure_subscription(binding.api_socket)
        bounds = (window['bounds']['width'], window['bounds']['height'])
        self.pending_clicks.append({'at': self.clock(), 'socket': binding.api_socket, 'binding': binding, 'client': client,
                                    'grid': grid, 'key': geometry.calibration_key(window.get('app'), bounds, client),
                                    'dx': event['x'] - window['bounds']['x'], 'dy': event['y'] - window['bounds']['y']})

    def drain_herdr_events(self):
        while True:
            try:
                at, socket_path, event = self.events.get_nowait()
            except queue.Empty:
                break
            if 'seed' in event:
                self.focus[socket_path] = dict(event['seed'])
                continue
            kind = EVENT_KIND.get(event.get('event'))
            target_id = (event.get('data') or {}).get(EVENT_ID[kind]) if kind else None
            if not target_id:
                continue
            known = self.focus.setdefault(socket_path, {})
            # herdr sends all three events on any focus change (app/api.rs), so
            # only the levels whose id changed say what the user clicked.
            if known.get(kind) is not None and known[kind] != target_id:
                self.changes.append((at, socket_path, kind, target_id))
            known[kind] = target_id
        now = self.clock()
        while self.changes and self.changes[0][0] < now - 10:
            self.changes.popleft()
        ready = [c for c in self.pending_clicks if now - c['at'] > CLICK_AFTER]
        self.pending_clicks = [c for c in self.pending_clicks if c not in ready]
        for click in ready:
            best = self.attribute(click)
            if best and not self.dragging:
                self.learn(click, best)

    def attribute(self, click):
        """(kind, id) the click focused: the shallowest level that changed in the
        one burst of focus changes near it, before or after. None when there is
        no burst or more than one."""
        near = sorted((c for c in self.changes if c[1] == click['socket']
                       and click['at'] - CLICK_BEFORE <= c[0] <= click['at'] + CLICK_AFTER), key=lambda c: c[0])
        if not near:
            return None
        if any(b[0] - a[0] > BURST_GAP for a, b in zip(near, near[1:])):
            return None
        for change in near:
            self.changes.remove(change)
        kind = min((c[2] for c in near), key=LEVEL.get)
        ids = {c[3] for c in near if c[2] == kind}
        if len(ids) != 1:
            return None   # two Spaces (or tabs) focused in one burst: which one was clicked is unknown
        return kind, ids.pop()

    def learn(self, click, best):
        try:
            model, _ = self.read_model(click['binding'], click['client'])
        except Unavailable:
            return
        target = model.find(*best)
        if target is None:
            return
        # Before any calibration the click's cell is uncertain by up to a few rows.
        # Unless no other target of that kind lies in that range, one click is
        # not proof: keep it provisional until a second click agrees.
        (c0, c1), (r0, r1) = click['grid'].cells(click['dx'], click['dy'])
        others = [t for t in model.targets if t.kind == target.kind and t.id != target.id
                  and t.rect.x <= c1 and t.rect.right > c0 and t.rect.y <= r1 and t.rect.bottom > r0]
        args = (click['key'], click['grid'], click['dx'], click['dy'], target.rect)
        # Only a click whose whole range contains the stored origin proves the
        # replica draws this target where herdr does (review N9).
        contains = self.calibration.contains(*args)
        origin, _ = self.calibration.entry(click['key'])
        if target.kind in ('space', 'tab') and origin is not None \
                and target.kind not in self.trusted_kinds(click['binding'], click['client']):
            # The replica's rect for this kind comes from a config the client may
            # not show: the click can confirm that config, never move the origin.
            self.log(f'click on {target.kind} {target.id}: config check, '
                     + ('confirmed' if contains else 'not confirmed'))
            if contains:
                self.stale.pop(click['key'], None)
                self.confirm_config(click, target, model)
            return
        outcome = self.calibration.learn(*args, provisional=bool(others))
        self.log(f'click on {target.kind} {target.id}: calibration {outcome}')
        if outcome in ('impossible', 'disagrees'):
            # herdr focused this target, but not where the replica draws it.
            self.stale[click['key']] = ('herdr focused a tab or Space where this replica does not draw it; '
                                        'spring-loading is off for this window until a click agrees again')
        elif outcome == 'relearned':
            # The geometry changed under earlier confirmations: they are void.
            self.verified.pop((click['client']['pid'], str(click['binding'].config_path)), None)
            self.stale.pop(click['key'], None)
        elif target.kind in ('space', 'tab'):
            self.stale.pop(click['key'], None)
            # A first calibration fits almost any config (review H3), and a click
            # that only overlaps the stored origin fits a shifted replica too.
            if outcome == 'agrees' and contains:
                self.confirm_config(click, target, model)
        self.report(calibrated=self.calibration.calibrated())

    # MARK: plugin registration

    def registration_allows(self):
        config_dir, _ = clients.herdr_dirs(self.env, self.platform)
        state = registration.registration(config_dir)
        seen = self.support/'registered-seen'
        if state == 'registered':
            self.missing_since = None
            manifest = registration.manifest_path(config_dir)
            try:
                recorded = json.loads(seen.read_text(encoding='utf-8')).get('manifest') if seen.exists() else None
            except (OSError, ValueError, AttributeError):
                recorded = None
            if recorded != manifest:
                try:
                    self.support.mkdir(parents=True, exist_ok=True)
                    seen.write_text(json.dumps({'manifest': manifest}))
                except OSError:
                    pass
            return True
        decision = registration.action(state, seen.exists())
        if decision == 'remove':
            # herdr plugin update or a reinstall may rewrite plugins.json without
            # the entry for a moment: confirm twice, and only once the plugin's
            # install directory is gone.
            now = self.clock()
            if self.missing_since is None:
                self.missing_since = now
            try:
                recorded = json.loads(seen.read_text(encoding='utf-8')).get('manifest')
            except (OSError, ValueError, AttributeError):
                recorded = None
            if now - self.missing_since >= REMOVAL_CONFIRM and recorded and not Path(recorded).parent.exists():
                self.remove_self()
                return False
            self.report('paused', 'the plugin is no longer registered with herdr; the helper removes itself once '
                                  'that is confirmed and the plugin folder is gone')
            return False
        self.missing_since = None
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
