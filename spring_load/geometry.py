"""From screen points to client grid cells without any permission.

The OS gives the window's outer frame; the client tty gives its grid in cells
and (usually) pixels. Where the grid sits inside the frame (titlebar, native
tab bar, padding) is unknown, so its origin is an interval. A point maps to a
cell range, and a target counts only when the whole range agrees. Ordinary
clicks narrow the interval (calibration, design section 2.3)."""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import tempfile

MAX_SIDE_LEFTOVER_CELLS = 1.5
MAX_TOP_LEFTOVER_ROWS = 6     # titlebar plus a native tab bar, plus padding
SCALES = (1.0, 2.0, 3.0)

@dataclass(frozen=True)
class Interval:
    low: float
    high: float

    @property
    def empty(self):
        return self.low > self.high

    @property
    def width(self):
        return self.high - self.low

    def intersect(self, other):
        return Interval(max(self.low, other.low), min(self.high, other.high))

@dataclass(frozen=True)
class Grid:
    """A client grid placed in a window: cell size and origin intervals, in
    the sensor's coordinate units, relative to the window's top-left corner."""
    cols: int
    rows: int
    cell_width: float
    cell_height: float
    origin_x: Interval
    origin_y: Interval

    def cells(self, dx, dy):
        """(columns, rows) ranges the point (dx, dy) relative to the window may be in."""
        def span(position, origin, size, count):
            first = math.floor((position - origin.high) / size)
            last = math.floor((position - origin.low) / size)
            return first, last
        c0, c1 = span(dx, self.origin_x, self.cell_width, self.cols)
        r0, r1 = span(dy, self.origin_y, self.cell_height, self.rows)
        return (c0, c1), (r0, r1)

    def narrowed(self, origin_x, origin_y):
        return Grid(self.cols, self.rows, self.cell_width, self.cell_height, origin_x, origin_y)

def fit(bounds, client, scale_hint=None):
    """The client's grid in a window of `bounds` (width, height), or None when it
    cannot be this window's grid. Pixel sizes may be in coordinate units or in
    backing pixels; the scale that fits is used."""
    width, height = bounds
    cols, rows = client.get('cols'), client.get('rows')
    xpixel, ypixel = client.get('xpixel') or 0, client.get('ypixel') or 0
    if not cols or not rows or xpixel <= 0 or ypixel <= 0:
        return None
    scales = sorted({*SCALES, *( [scale_hint] if scale_hint else [])}, key=lambda s: (s != scale_hint, s))
    fits = []
    for scale in scales:
        grid_w, grid_h = xpixel / scale, ypixel / scale
        cell_w, cell_h = grid_w / cols, grid_h / rows
        left_w, left_h = width - grid_w, height - grid_h
        if -0.5 <= left_w <= MAX_SIDE_LEFTOVER_CELLS * cell_w and -0.5 <= left_h <= MAX_TOP_LEFTOVER_ROWS * cell_h:
            fits.append(Grid(cols, rows, cell_w, cell_h, Interval(0, max(0, left_w)), Interval(0, max(0, left_h))))
    # Two scales fitting the same window means the unit is ambiguous: refuse.
    return fits[0] if len(fits) == 1 else None

def calibration_key(app, bounds, client):
    return '|'.join(str(v) for v in (app or '?', round(bounds[0]), round(bounds[1]), client.get('cols'), client.get('rows'),
                                     client.get('xpixel'), client.get('ypixel')))

def _interval_pair(value):
    if isinstance(value, list) and len(value) == 4 and all(isinstance(v, (int, float)) for v in value):
        return Interval(value[0], value[1]), Interval(value[2], value[3])
    return None

class Calibration:
    """Learned origin intervals per window shape, kept in a small JSON file.
    Each entry is {"origin": [x0, x1, y0, y1], "candidate": [...] or null}. A
    click that disagrees with the stored origin becomes a candidate and replaces
    the origin only when a second click agrees with it, so one misattributed
    click cannot overwrite a good calibration."""
    def __init__(self, path):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(self.data, dict):
                self.data = {}
        except (OSError, ValueError):
            self.data = {}

    def entry(self, key):
        value = self.data.get(key)
        if isinstance(value, list):   # the first 0.3.0 format: a bare origin
            value = {'origin': value, 'candidate': None}
        if not isinstance(value, dict):
            return None, None
        return _interval_pair(value.get('origin')), _interval_pair(value.get('candidate'))

    def calibrated(self):
        """How many window shapes have a confirmed origin."""
        return sum(1 for key in self.data if self.entry(key)[0] is not None)

    def apply(self, key, grid):
        origin, _ = self.entry(key)
        if origin is None:
            return grid
        x = grid.origin_x.intersect(origin[0])
        y = grid.origin_y.intersect(origin[1])
        return grid if x.empty or y.empty else grid.narrowed(x, y)

    @staticmethod
    def constraint(grid, dx, dy, rect):
        """Origins (x, y intervals) under which (dx, dy) lies inside `rect`."""
        bx = grid.origin_x.intersect(Interval(dx - rect.right * grid.cell_width, dx - rect.x * grid.cell_width))
        by = grid.origin_y.intersect(Interval(dy - rect.bottom * grid.cell_height, dy - rect.y * grid.cell_height))
        return bx, by

    def contains(self, key, grid, dx, dy, rect, tolerance=0.05):
        """Whether every origin stored for `key` puts (dx, dy) inside `rect`
        (within `tolerance` cells). Only such a click proves the replica draws
        `rect` where herdr does: one whose range merely overlaps the stored
        origin fits a replica one row off as well (review N9)."""
        origin, _ = self.entry(key)
        if origin is None:
            return False
        bx, by = self.constraint(grid, dx, dy, rect)
        tx, ty = tolerance * grid.cell_width, tolerance * grid.cell_height
        return (not bx.empty and not by.empty and bx.low <= origin[0].low + tx and bx.high >= origin[0].high - tx
                and by.low <= origin[1].low + ty and by.high >= origin[1].high - ty)

    def learn(self, key, grid, dx, dy, rect, provisional=False):
        """The user clicked (dx, dy) and herdr focused the target with cell rect
        `rect`: the origin must put that point inside the rect. Returns
        'impossible' (no origin the window allows does that), 'learned' (first
        calibration), 'agrees' (narrowed the stored origin), 'disagrees' (kept
        the stored origin, remembered this as a candidate) or 'relearned' (a
        second click agreed with the candidate, which replaced the origin).
        With `provisional` (the click could also have meant another target),
        a first calibration is kept as a candidate ('candidate') until a
        second click agrees with it."""
        bx, by = self.constraint(grid, dx, dy, rect)
        if bx.empty or by.empty:
            return 'impossible'
        origin, candidate = self.entry(key)
        outcome, new_candidate = 'learned', None
        if origin is None and candidate is not None and not candidate[0].intersect(bx).empty \
                and not candidate[1].intersect(by).empty:
            bx, by = candidate[0].intersect(bx), candidate[1].intersect(by)
        elif origin is None and provisional:
            self.data[key] = {'origin': None, 'candidate': [bx.low, bx.high, by.low, by.high]}
            self.save()
            return 'candidate'
        elif origin is not None:
            nx, ny = origin[0].intersect(bx), origin[1].intersect(by)
            if not nx.empty and not ny.empty:
                bx, by, outcome = nx, ny, 'agrees'
            elif candidate is not None and not candidate[0].intersect(bx).empty and not candidate[1].intersect(by).empty:
                bx, by, outcome = candidate[0].intersect(bx), candidate[1].intersect(by), 'relearned'
            else:
                bx, by, outcome, new_candidate = origin[0], origin[1], 'disagrees', (bx, by)
        self.data[key] = {'origin': [bx.low, bx.high, by.low, by.high],
                          'candidate': None if new_candidate is None else
                          [new_candidate[0].low, new_candidate[0].high, new_candidate[1].low, new_candidate[1].high]}
        self.save()
        return outcome

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix='calibration.', dir=self.path.parent)
            with os.fdopen(fd, 'w') as out:
                json.dump(self.data, out)
            os.replace(temp, self.path)
        except OSError:
            pass

def resolve(model, grid, dx, dy, row_precision=0.5):
    """The target under (dx, dy) when every cell the point may be in belongs to
    it. Spaces and tabs also need the row known to within `row_precision` rows
    (usually after a calibrating click); panes are large enough without."""
    (c0, c1), (r0, r1) = grid.cells(dx, dy)
    corners = {model.at(c, r) for c in (c0, c1) for r in (r0, r1)}
    if len(corners) != 1:
        return None
    target = corners.pop()
    if target is None:
        return None
    if target.kind in ('space', 'tab') and grid.origin_y.width > row_precision * grid.cell_height:
        return None
    return target
