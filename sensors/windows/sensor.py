"""Experimental Windows sensor for herdr-spring-load (docs/SENSOR.md, contract 1).

Observes only: pointer, the primary button, the drag heuristic, the window
under the pointer and the herdr clients behind it. Prints NDJSON on stdout,
exits on stdin EOF or a broken stdout. No hooks, no input, no permissions.

Flags: --check (print the hello and exit), --verbose (diagnostics on stderr).
--console-probe PID is internal: the sensor runs itself with it to read a
console size without leaving its own console."""
import json
import sys
import threading
import time

from . import logic

VERSION = '0.3.0'
CAPABILITIES = ['drag', 'click', 'window', 'clients']
POLL = 0.015
REFRESH = 1.0
REASONS = {
    'ambiguous_sessions': 'this terminal window hosts more than one tab or pane, and Windows cannot tell which '
                          'one is visible; spring-loading is off here. Run herdr alone in its window '
                          '(Windows Terminal: one tab, no splits).',
    'ambiguous_clients': 'more than one herdr client runs under this terminal; spring-loading is off here.',
    'unknown_terminal': 'this terminal starts no console host the sensor recognises; spring-loading is off here.',
}

def hello(capabilities=CAPABILITIES):
    return {'type': 'hello', 't': 0.0, 'contract': 1, 'sensor': 'windows', 'version': VERSION,
            'coords': 'windows-virtual', 'capabilities': list(capabilities)}

class Stopped(Exception):
    pass

class Sensor:
    def __init__(self, win, out=None, clock=time.monotonic, verbose=False):
        self.win = win
        self.out = out or sys.stdout
        self.clock = clock
        self.start = clock()
        self.verbose = verbose
        self.detector = logic.DragDetector()
        self.throttle = logic.MoveThrottle()
        self.screen = None
        self.clients = {} # hwnd -> (time, owner exe, clients, reason)
        self.peers = {} # hwnd -> (time, peer rects)
        self.warned = set()
        self.press_tid = None
        self.window = None # window under the pointer during a drag

    def now(self):
        return round(self.clock() - self.start, 4)

    def emit(self, event):
        line = json.dumps(event, separators=(',', ':'))
        try:
            self.out.write(line + '\n')
            self.out.flush()
        except (BrokenPipeError, OSError, ValueError):
            raise Stopped()

    def debug(self, message):
        if self.verbose:
            print('windows sensor: ' + message, file=sys.stderr, flush=True)

    def status(self, level, message, key=None):
        if key is not None:
            if key in self.warned:
                return
            self.warned.add(key)
        self.emit({'type': 'status', 't': self.now(), 'level': level, 'message': message})

    def begin(self):
        """Hello, then the one-time setup. Returns False when the sensor cannot work."""
        if not self.win.set_dpi_awareness():
            self.emit(hello(['window', 'clients']))
            self.status('error', 'per-monitor DPI awareness could not be enabled; drag detection is off')
            return False
        self.emit(hello())
        self.screen = self.win.virtual_screen()
        self.detector.threshold = self.win.drag_threshold()
        return True

    # Windows and clients

    def hit(self, x, y):
        """(TopWindow, peers) under the point, skipping overlays and the drag image."""
        root = self.win.root_at(x, y)
        window = self.win.window(root) if root else None
        if window is not None and logic.normal_window(window, self.screen):
            cached = self.peers.get(window.hwnd)
            if cached and self.clock() - cached[0] < REFRESH:
                return window, cached[1]
            windows = self.win.top_windows()
            peers = [o.bounds for o in windows
                     if o.hwnd != window.hwnd and o.pid == window.pid and logic.normal_window(o, self.screen)]
        else:
            window, peers = logic.pick_window(self.win.top_windows(), x, y, self.screen)
            if window is None:
                return None, []
        self.peers[window.hwnd] = (self.clock(), peers)
        return window, peers

    def lookup(self, window):
        processes = self.win.processes()
        if processes is None:
            return None, None, None
        owner = next((p for p in processes if p.pid == window.pid), None)
        if owner is None:
            return None, [], None
        times = {}
        def created(pid):
            if pid not in times:
                times[pid] = self.win.created(pid)
            return times[pid]
        clients, reason = logic.find_clients(owner, window.cls, processes, created, self.win.argv,
                                             self.win.console_size, self.win.environment, self.win.started)
        return owner.exe, clients, reason

    def all_clients(self):
        """drag_start's all_clients, or None when the process list cannot be read."""
        processes = self.win.processes()
        if processes is None:
            return None
        return logic.all_clients(processes, self.win.session_of, self.win.own_session(), self.win.argv,
                                 self.win.environment, self.win.started)

    def describe(self, window, peers, refresh):
        if window is None:
            return None
        cached = self.clients.get(window.hwnd)
        if refresh or cached is None:
            exe, clients, reason = self.lookup(window)
            cached = (self.clock(), exe, clients, reason)
            self.clients[window.hwnd] = cached
            self.debug(f'window {window.hwnd} ({exe}): {len(clients or [])} clients, {reason or "ok"}')
            if reason:
                self.status('warn', REASONS[reason], key=(window.hwnd, reason))
        _, exe, clients, _ = cached
        return {'id': str(window.hwnd), 'pid': window.pid, 'app': exe, 'bounds': window.bounds.as_json(),
                'scale': self.win.scale(window.hwnd), 'peers': [p.as_json() for p in peers], 'clients': clients}

    # Polling

    def tick(self):
        position = self.win.cursor_pos()
        if position is None:
            return
        x, y = position
        held = self.win.primary_held()
        state = self.detector.state
        tick = logic.Tick(t=self.clock(), x=x, y=y, held=held)
        if held and state == 'idle':
            # Press: remember the pressed window and its thread for the capture check.
            window, _ = self.hit(x, y)
            tick.window = window.hwnd if window else None
            self.press_tid = window.tid if window else None
        elif held and state in ('pressed', 'dragging'):
            tick.escape = self.win.escape_held()
            tick.window = self.win.root_at(x, y)
            tick.capture_hidden = self.win.capture_hidden(self.press_tid)
            if state == 'pressed':
                tick.cursor_standard = self.win.cursor_standard()
        for event in self.detector.update(tick):
            self.handle(event, x, y)
        if self.detector.state == 'dragging' and held:
            self.move(x, y)

    def handle(self, event, x, y):
        kind = event[0]
        if kind == 'click':
            press = event[1]
            window, peers = self.hit(press.x, press.y)
            cached = self.clients.get(window.hwnd) if window else None
            refresh = cached is None or self.clock() - cached[0] >= REFRESH
            self.emit({'type': 'click', 't': self.now(), 'x': press.x, 'y': press.y,
                       'window': self.describe(window, peers, refresh)})
        elif kind == 'drag_start':
            self.clients.clear()
            window, peers = self.hit(x, y)
            self.window = window.hwnd if window else None
            self.emit({'type': 'drag_start', 't': self.now(), 'x': x, 'y': y, 'kind': 'unknown',
                       'window': self.describe(window, peers, True), 'all_clients': self.all_clients()})
            self.throttle.reset()
            self.throttle.due(self.clock(), (x, y, self.window))
        elif kind == 'drag_end':
            self.window = None
            self.emit({'type': 'drag_end', 't': self.now(), 'x': x, 'y': y, 'reason': event[2]})

    def move(self, x, y):
        root = self.win.root_at(x, y)
        if not self.throttle.due(self.clock(), (x, y, root)):
            return
        window, peers = self.hit(x, y)
        hwnd = window.hwnd if window else None
        changed = hwnd != self.window
        self.window = hwnd
        self.emit({'type': 'drag_move', 't': self.now(), 'x': x, 'y': y,
                   'window': self.describe(window, peers, changed and hwnd not in self.clients)})

    def run(self, stop):
        if not self.begin():
            stop.wait()
            return
        while not stop.is_set():
            self.tick()
            stop.wait(POLL)

def watch_stdin(stop, stream=None):
    def read():
        source = stream or sys.stdin.buffer
        try:
            while source.read(4096):
                pass
        except (OSError, ValueError):
            pass
        stop.set()
    threading.Thread(target=read, daemon=True).start()

def main(argv=None, win_factory=None, platform=None):
    argv = sys.argv[1:] if argv is None else argv
    platform = platform or sys.platform
    if '--console-probe' in argv:
        from .win32 import probe_console
        size = probe_console(int(argv[argv.index('--console-probe') + 1]))
        print(json.dumps({'cols': size[0], 'rows': size[1]} if size else None))
        return 0
    sensor = None
    stop = threading.Event()
    try:
        if '--check' in argv:
            Sensor(None).emit(hello())
            return 0
        if platform != 'win32':
            sensor = Sensor(None)
            sensor.emit(hello([]))
            sensor.status('error', 'the Windows sensor runs only on Windows')
            watch_stdin(stop)
            stop.wait()
            return 0
        if win_factory is None:
            from .win32 import RealWin32
            win_factory = lambda: RealWin32(probe_argv=[sys.executable, sys.argv[0], '--console-probe'])
        sensor = Sensor(win_factory(), verbose='--verbose' in argv)
        watch_stdin(stop)
        sensor.run(stop)
    except Stopped:
        pass
    return 0
