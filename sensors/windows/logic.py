"""Pure decision logic of the Windows sensor: no ctypes, testable anywhere.

- DragDetector: drag and click detection from polled button, pointer, capture
  and cursor state (the heuristic is documented in README.md).
- pick_window: the topmost normal top-level window under the pointer.
- find_clients: herdr client processes below a window owner, with the
  fail-safe rule for terminals that host several sessions in one process."""
from dataclasses import dataclass

CLICK_DISTANCE = 8
CLICK_TIME = 0.5
MOVE_MIN = 0.03
MOVE_MAX = 0.25

WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
# Shell surfaces and the drag image are never targets.
SKIP_CLASSES = {'Progman', 'WorkerW', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd', 'SysDragImage',
                'NotifyIconOverflowWindow', 'Windows.UI.Core.CoreWindow'}
CONSOLE_CLASS = 'ConsoleWindowClass'
CONSOLE_HOSTS = {'openconsole.exe', 'conhost.exe'}
HERDR_NAMES = {'herdr', 'herdr.exe'}

@dataclass
class Rect:
    x: float
    y: float
    width: float
    height: float

    def contains(self, px, py):
        return self.x <= px < self.x + self.width and self.y <= py < self.y + self.height

    def intersects(self, other):
        return (self.x < other.x + other.width and other.x < self.x + self.width
                and self.y < other.y + other.height and other.y < self.y + self.height)

    def as_json(self):
        return {'x': self.x, 'y': self.y, 'width': self.width, 'height': self.height}

@dataclass
class TopWindow:
    """One top-level window, in z-order, as the facade reports it."""
    hwnd: int
    pid: int
    tid: int
    cls: str
    bounds: Rect
    visible: bool = True
    iconic: bool = False
    cloaked: bool = False
    exstyle: int = 0
    alpha: int | None = None # layered window alpha, None when not layered

@dataclass
class Process:
    pid: int
    ppid: int
    exe: str

def normal_window(w, screen=None):
    if not w.visible or w.iconic or w.cloaked or w.cls in SKIP_CLASSES:
        return False
    if w.exstyle & (WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE):
        return False
    if w.alpha == 0 or w.bounds.width <= 0 or w.bounds.height <= 0:
        return False
    return screen is None or w.bounds.intersects(screen)

def pick_window(windows, x, y, screen=None):
    """(window, peers): the first normal window in z-order containing the point,
    and the bounds of the same process's other normal windows."""
    for w in windows:
        if normal_window(w, screen) and w.bounds.contains(x, y):
            peers = [o.bounds for o in windows if o.hwnd != w.hwnd and o.pid == w.pid and normal_window(o, screen)]
            return w, peers
    return None, []

def descends(pid, ancestor, parents, created, depth=64):
    """True when `pid` is below `ancestor`. Every link must have a parent that
    was created no later than its child; an unknown time breaks the chain,
    because Windows reuses parent pids."""
    current = pid
    for _ in range(depth):
        parent = parents.get(current)
        if parent is None or parent == 0 or parent == current:
            return False
        child_time, parent_time = created(current), created(parent)
        if child_time is None or parent_time is None or parent_time > child_time:
            return False
        if parent == ancestor:
            return True
        current = parent
    return False

def session_count(owner, owner_cls, processes):
    """How many terminal sessions the owner process hosts. A classic console
    window shows exactly one. ConPTY terminals (Windows Terminal, WezTerm,
    Alacritty) start one console host per tab or pane as a direct child.
    None means the structure is not recognised."""
    if owner_cls == CONSOLE_CLASS:
        return 1
    hosts = [p for p in processes if p.ppid == owner.pid and p.exe.lower() in CONSOLE_HOSTS]
    return len(hosts) or None

def find_clients(owner, owner_cls, processes, created, argv_of, console_size):
    """Client objects (docs/SENSOR.md 2.1) for the herdr processes below `owner`.

    `foreground` is true only when the window provably shows that client: it
    is the only herdr client found and the owner hosts exactly one session.
    Otherwise every client is reported with foreground false, so the core
    refuses. This is the Windows Terminal multi-tab rule: hidden tabs keep
    their console size, so fitting the window proves nothing. Returns
    (clients, reason) where reason explains a refusal, or None."""
    parents = {p.pid: p.ppid for p in processes}
    candidates = [p for p in processes if p.exe.lower() in HERDR_NAMES
                  and (descends(p.pid, owner.pid, parents, created)
                       or (owner_cls == CONSOLE_CLASS and p.pid == owner.pid))]
    clients = []
    for p in candidates:
        size = console_size(p.pid)
        if size is None:
            continue # no console: the server or a detached CLI, not a client
        cols, rows = size
        clients.append({'pid': p.pid, 'argv': argv_of(p.pid) or [], 'tty': None, 'rows': rows, 'cols': cols,
                        'xpixel': 0, 'ypixel': 0, 'foreground': False})
    sessions = session_count(owner, owner_cls, processes)
    reason = None
    if len(clients) > 1:
        reason = 'ambiguous_clients'
    elif clients and sessions != 1:
        reason = 'ambiguous_sessions' if sessions else 'unknown_terminal'
    if clients and reason is None:
        clients[0]['foreground'] = True
    return clients, reason

@dataclass
class Tick:
    """One poll of the input state."""
    t: float
    x: float
    y: float
    held: bool
    escape: bool = False
    window: int | None = None # root window under the pointer
    capture_hidden: bool | None = None # the pressed window's thread holds capture with a hidden window
    cursor_standard: bool | None = None # the cursor is one of the system IDC_* cursors

@dataclass
class DragDetector:
    """Turns polled ticks into drag_start / drag_end / click events.

    A drag starts while the primary button is held, the pointer has moved past
    the system drag threshold, and either the pressed window's thread holds
    the mouse capture with a hidden window (OLE's DoDragDrop tracking window)
    or the cursor is not a system cursor while over a different window than
    the one pressed. A text selection or window move inside one window never
    counts."""
    threshold: tuple = (4, 4)
    state: str = 'idle' # idle, pressed, dragging, blocked
    press: Tick | None = None
    capture_seen: bool = False

    def update(self, tick):
        out = []
        if self.state == 'idle':
            if tick.held:
                self.state, self.press, self.capture_seen = 'pressed', tick, False
        elif self.state == 'pressed':
            if not tick.held:
                p = self.press
                if (abs(tick.x - p.x) <= CLICK_DISTANCE and abs(tick.y - p.y) <= CLICK_DISTANCE
                        and tick.t - p.t <= CLICK_TIME):
                    out.append(('click', p))
                self.state = 'idle'
            elif tick.escape:
                self.state = 'blocked'
            elif self._started(tick):
                self.state = 'dragging'
                self.capture_seen = bool(tick.capture_hidden)
                out.append(('drag_start', tick))
        elif self.state == 'dragging':
            if not tick.held:
                self.state = 'idle'
                out.append(('drag_end', tick, 'released'))
            elif tick.escape or (self.capture_seen and tick.capture_hidden is False):
                # Esc cancels an OLE drag; capture gone while held means its loop ended.
                self.state = 'blocked'
                out.append(('drag_end', tick, 'cancelled'))
            else:
                self.capture_seen = self.capture_seen or bool(tick.capture_hidden)
        elif self.state == 'blocked' and not tick.held:
            self.state = 'idle'
        return out

    def _started(self, tick):
        p = self.press
        if abs(tick.x - p.x) <= self.threshold[0] and abs(tick.y - p.y) <= self.threshold[1]:
            return False
        if tick.capture_hidden:
            return True
        return tick.cursor_standard is False and tick.window is not None and tick.window != p.window

@dataclass
class MoveThrottle:
    """drag_move pacing: at most every 30 ms, on any change, and at least every 250 ms."""
    last_t: float | None = None
    last_key: tuple | None = None

    def due(self, t, key):
        if self.last_t is not None and t - self.last_t < MOVE_MIN:
            return False
        if self.last_t is None or key != self.last_key or t - self.last_t >= MOVE_MAX:
            self.last_t, self.last_key = t, key
            return True
        return False

    def reset(self):
        self.last_t = self.last_key = None
