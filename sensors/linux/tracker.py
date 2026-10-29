"""Drag and click state machine shared by the Linux backends (docs/SENSOR.md, section 3).

A backend supplies the pointer, the drag signal and window descriptions; the
tracker turns them into contract events. It holds no OS handles, so it is unit
tested with a scripted backend.
"""
from common import MoveThrottle

CLICK_DISTANCE = 8
CLICK_TIME = 0.5


class Tracker:
    """Call step(at, x, y, held, drag_signal) once per poll.

    drag_signal is 'started' when the backend saw a new drag source this poll
    (X11: XdndSelection got an owner), 'lost' when the source went away
    without a release, and None otherwise."""

    def __init__(self, describe, kind, emit, everyone=None):
        self.describe = describe    # (x, y) -> window dict or None
        self.kind = kind            # () -> 'files' | 'other' | 'unknown'
        self.emit = emit
        self.everyone = everyone    # () -> every herdr client of this user, or None
        self.dragging = False
        self.armed = False
        self.press = None
        self.held = False
        self.throttle = MoveThrottle()

    def step(self, at, x, y, held, drag_signal=None):
        if drag_signal == 'started':
            self.armed = True
        if self.dragging:
            if not held:
                self.end(x, y, 'released')
            elif drag_signal == 'lost':
                self.end(x, y, 'cancelled')
            else:
                window = self.describe(x, y)
                key = (round(x, 1), round(y, 1), window and window['id'],
                       window and tuple(window['bounds'].values()))
                if self.throttle.due(key, at):
                    self.emit({'type': 'drag_move', 'x': x, 'y': y, 'window': window})
        elif self.armed and held:
            self.dragging = True
            self.press = None
            self.throttle = MoveThrottle()
            self.throttle.due(None, at)
            event = {'type': 'drag_start', 'x': x, 'y': y, 'kind': self.kind(), 'window': self.describe(x, y)}
            if self.everyone:
                event['all_clients'] = self.everyone()
            self.emit(event)
        else:
            self.track_click(at, x, y, held)
        if not held:
            self.armed = False
        self.held = held

    def end(self, x, y, reason):
        self.dragging = False
        self.armed = False
        self.press = None
        self.emit({'type': 'drag_end', 'x': x, 'y': y, 'reason': reason})

    def cancel(self, x, y):
        """The sensor lost track (display gone, suspend): close an open drag."""
        if self.dragging:
            self.end(x, y, 'cancelled')

    def track_click(self, at, x, y, held):
        if held and not self.held:
            self.press = (at, x, y)
        elif not held and self.held and self.press:
            t0, x0, y0 = self.press
            self.press = None
            if at - t0 <= CLICK_TIME and abs(x - x0) <= CLICK_DISTANCE and abs(y - y0) <= CLICK_DISTANCE:
                self.emit({'type': 'click', 'x': x0, 'y': y0, 'window': self.describe(x0, y0)})
