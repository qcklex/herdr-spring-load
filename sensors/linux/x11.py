"""X11 backend: stdlib ctypes over libX11 and libXfixes. Needs no permission.

- pointer and buttons: XQueryPointer on the root window, plus XInput2 raw
  button events so presses shorter than a poll (touchpad taps) are not lost;
- drag start: XFixes SetSelectionOwnerNotify on XdndSelection, which every XDND
  source (GTK, Qt, Electron, dragon) takes when a drag begins;
- drag kind: the source's XdndTypeList atom names, when the source publishes
  them (reading the offered types is allowed, the data never is);
- window: the topmost mapped, non-override-redirect child of the root at the
  pointer (so drag icons are skipped), then its WM_STATE client window, that
  window's _NET_WM_PID, WM_CLASS and root-relative geometry; peers from
  _NET_CLIENT_LIST.

bounds is the client window, without the window manager's decorations. The
decorated frame is reported as the extra field 'frame'.
"""
import ctypes
import ctypes.util
import select
import time

import common
from tracker import Tracker

Window = ctypes.c_ulong
Atom = ctypes.c_ulong

BUTTON1_MASK = 1 << 8
IS_VIEWABLE = 2
ANY_PROPERTY_TYPE = 0
SET_SELECTION_OWNER_NOTIFY_MASK = 1 << 0
SELECTION_NOTIFY = 0
POLL_INTERVAL = 0.020
GENERIC_EVENT = 35
XI_ALL_MASTER_DEVICES = 1
XI_RAW_BUTTON_PRESS = 15
XI_RAW_BUTTON_RELEASE = 16
FILE_TYPES = {'text/uri-list', 'x-special/gnome-copied-files', 'application/x-kde4-urilist'}


class SelectionNotify(ctypes.Structure):
    _fields_ = [('type', ctypes.c_int), ('serial', ctypes.c_ulong), ('send_event', ctypes.c_int),
                ('display', ctypes.c_void_p), ('window', Window), ('subtype', ctypes.c_int),
                ('owner', Window), ('selection', Atom), ('timestamp', ctypes.c_ulong),
                ('selection_timestamp', ctypes.c_ulong)]


class GenericEventCookie(ctypes.Structure):
    _fields_ = [('type', ctypes.c_int), ('serial', ctypes.c_ulong), ('send_event', ctypes.c_int),
                ('display', ctypes.c_void_p), ('extension', ctypes.c_int), ('evtype', ctypes.c_int),
                ('cookie', ctypes.c_uint), ('data', ctypes.c_void_p)]


class XEvent(ctypes.Union):
    _fields_ = [('type', ctypes.c_int), ('xfixes', SelectionNotify), ('xcookie', GenericEventCookie),
                ('pad', ctypes.c_long * 24)]


class XIRawEvent(ctypes.Structure):
    _fields_ = [('type', ctypes.c_int), ('serial', ctypes.c_ulong), ('send_event', ctypes.c_int),
                ('display', ctypes.c_void_p), ('extension', ctypes.c_int), ('evtype', ctypes.c_int),
                ('time', ctypes.c_ulong), ('deviceid', ctypes.c_int), ('sourceid', ctypes.c_int),
                ('detail', ctypes.c_int), ('flags', ctypes.c_int)]


class XIEventMask(ctypes.Structure):
    _fields_ = [('deviceid', ctypes.c_int), ('mask_len', ctypes.c_int), ('mask', ctypes.POINTER(ctypes.c_ubyte))]


class Attributes(ctypes.Structure):
    _fields_ = [('x', ctypes.c_int), ('y', ctypes.c_int), ('width', ctypes.c_int),
                ('height', ctypes.c_int), ('border_width', ctypes.c_int), ('depth', ctypes.c_int),
                ('visual', ctypes.c_void_p), ('root', Window), ('class_', ctypes.c_int),
                ('bit_gravity', ctypes.c_int), ('win_gravity', ctypes.c_int),
                ('backing_store', ctypes.c_int), ('backing_planes', ctypes.c_ulong),
                ('backing_pixel', ctypes.c_ulong), ('save_under', ctypes.c_int),
                ('colormap', ctypes.c_ulong), ('map_installed', ctypes.c_int),
                ('map_state', ctypes.c_int), ('all_event_masks', ctypes.c_long),
                ('your_event_mask', ctypes.c_long), ('do_not_propagate_mask', ctypes.c_long),
                ('override_redirect', ctypes.c_int), ('screen', ctypes.c_void_p)]


def _library(name, soname):
    return ctypes.CDLL(ctypes.util.find_library(name) or soname)


def _bind(xlib, xfixes):
    p = ctypes.POINTER
    sig = {
        'XOpenDisplay': (ctypes.c_void_p, [ctypes.c_char_p]),
        'XDefaultRootWindow': (Window, [ctypes.c_void_p]),
        'XInternAtom': (Atom, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]),
        'XGetAtomName': (ctypes.c_void_p, [ctypes.c_void_p, Atom]),
        'XGetSelectionOwner': (Window, [ctypes.c_void_p, Atom]),
        'XConnectionNumber': (ctypes.c_int, [ctypes.c_void_p]),
        'XPending': (ctypes.c_int, [ctypes.c_void_p]),
        'XNextEvent': (ctypes.c_int, [ctypes.c_void_p, p(XEvent)]),
        'XQueryPointer': (ctypes.c_int, [ctypes.c_void_p, Window, p(Window), p(Window), p(ctypes.c_int),
                                         p(ctypes.c_int), p(ctypes.c_int), p(ctypes.c_int), p(ctypes.c_uint)]),
        'XGetWindowProperty': (ctypes.c_int, [ctypes.c_void_p, Window, Atom, ctypes.c_long, ctypes.c_long,
                                              ctypes.c_int, Atom, p(Atom), p(ctypes.c_int), p(ctypes.c_ulong),
                                              p(ctypes.c_ulong), p(ctypes.c_void_p)]),
        'XGetWindowAttributes': (ctypes.c_int, [ctypes.c_void_p, Window, p(Attributes)]),
        'XTranslateCoordinates': (ctypes.c_int, [ctypes.c_void_p, Window, Window, ctypes.c_int, ctypes.c_int,
                                                 p(ctypes.c_int), p(ctypes.c_int), p(Window)]),
        'XQueryTree': (ctypes.c_int, [ctypes.c_void_p, Window, p(Window), p(Window), p(ctypes.c_void_p),
                                      p(ctypes.c_uint)]),
        'XFree': (ctypes.c_int, [ctypes.c_void_p]),
        'XSync': (ctypes.c_int, [ctypes.c_void_p, ctypes.c_int]),
        'XSetErrorHandler': (ctypes.c_void_p, [ctypes.c_void_p]),
        'XQueryExtension': (ctypes.c_int, [ctypes.c_void_p, ctypes.c_char_p, p(ctypes.c_int), p(ctypes.c_int),
                                           p(ctypes.c_int)]),
        'XGetEventData': (ctypes.c_int, [ctypes.c_void_p, p(GenericEventCookie)]),
        'XFreeEventData': (None, [ctypes.c_void_p, p(GenericEventCookie)]),
    }
    for name, (restype, argtypes) in sig.items():
        fn = getattr(xlib, name)
        fn.restype, fn.argtypes = restype, argtypes
    xfixes.XFixesQueryExtension.argtypes = [ctypes.c_void_p, p(ctypes.c_int), p(ctypes.c_int)]
    xfixes.XFixesSelectSelectionInput.argtypes = [ctypes.c_void_p, Window, Atom, ctypes.c_ulong]


# Windows vanish between calls. Ignore BadWindow and friends instead of letting
# Xlib's default handler exit the process. Kept at module level so it is never collected.
_ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(lambda display, event: 0)


class X11:
    def __init__(self, display_name=None):
        self.x = _library('X11', 'libX11.so.6')
        self.xfixes = _library('Xfixes', 'libXfixes.so.3')
        _bind(self.x, self.xfixes)
        self.x.XSetErrorHandler(ctypes.cast(_ERROR_HANDLER, ctypes.c_void_p))
        self.dpy = self.x.XOpenDisplay(display_name.encode() if display_name else None)
        if not self.dpy:
            raise RuntimeError('cannot open the X display')
        self.root = self.x.XDefaultRootWindow(self.dpy)
        self.atoms = {}
        base, error = ctypes.c_int(), ctypes.c_int()
        if not self.xfixes.XFixesQueryExtension(self.dpy, ctypes.byref(base), ctypes.byref(error)):
            raise RuntimeError('the XFixes extension is missing, so drags cannot be seen')
        self.selection_event = base.value + SELECTION_NOTIFY
        self.xdnd = self.atom('XdndSelection')
        self.xfixes.XFixesSelectSelectionInput(self.dpy, self.root, self.xdnd, SET_SELECTION_OWNER_NOTIFY_MASK)
        self.xi_opcode = self.select_raw_buttons()
        self.x.XSync(self.dpy, 0)
        self.owner = self.x.XGetSelectionOwner(self.dpy, self.xdnd)
        self.cache = common.ClientCache()
        self.described = (None, 0.0, None)

    def select_raw_buttons(self):
        """Ask for XInput2 raw button events on the root, so presses shorter than a poll
        (touchpad taps, synthetic clicks) are still seen. None when XI2 is missing."""
        opcode, event, error = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        if not self.x.XQueryExtension(self.dpy, b'XInputExtension', ctypes.byref(opcode), ctypes.byref(event),
                                      ctypes.byref(error)):
            return None
        try:
            xi = _library('Xi', 'libXi.so.6')
        except OSError:
            return None
        xi.XIQueryVersion.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        xi.XISelectEvents.argtypes = [ctypes.c_void_p, Window, ctypes.POINTER(XIEventMask), ctypes.c_int]
        major, minor = ctypes.c_int(2), ctypes.c_int(0)
        if xi.XIQueryVersion(self.dpy, ctypes.byref(major), ctypes.byref(minor)) != 0:
            return None
        bits = (ctypes.c_ubyte * 4)()
        for evtype in (XI_RAW_BUTTON_PRESS, XI_RAW_BUTTON_RELEASE):
            bits[evtype >> 3] |= 1 << (evtype & 7)
        mask = XIEventMask(XI_ALL_MASTER_DEVICES, len(bits), bits)
        xi.XISelectEvents(self.dpy, self.root, ctypes.byref(mask), 1)
        return opcode.value

    def fileno(self):
        return self.x.XConnectionNumber(self.dpy)

    def atom(self, name):
        if name not in self.atoms:
            self.atoms[name] = self.x.XInternAtom(self.dpy, name.encode(), 0)
        return self.atoms[name]

    def atom_name(self, atom):
        raw = self.x.XGetAtomName(self.dpy, atom)
        if not raw:
            return None
        try:
            return ctypes.string_at(raw).decode('utf-8', 'replace')
        finally:
            self.x.XFree(raw)

    def events(self):
        """Pending events in order: ('drag', 'started' | 'lost') and ('button', held)."""
        found = []
        event = XEvent()
        while self.x.XPending(self.dpy):
            self.x.XNextEvent(self.dpy, ctypes.byref(event))
            if event.type == self.selection_event and event.xfixes.selection == self.xdnd:
                self.owner = event.xfixes.owner
                found.append(('drag', 'started' if self.owner else 'lost'))
            elif event.type == GENERIC_EVENT and event.xcookie.extension == self.xi_opcode:
                cookie = event.xcookie
                if self.x.XGetEventData(self.dpy, ctypes.byref(cookie)):
                    try:
                        raw = ctypes.cast(cookie.data, ctypes.POINTER(XIRawEvent))[0]
                        if raw.detail == 1 and cookie.evtype in (XI_RAW_BUTTON_PRESS, XI_RAW_BUTTON_RELEASE):
                            found.append(('button', cookie.evtype == XI_RAW_BUTTON_PRESS))
                    finally:
                        self.x.XFreeEventData(self.dpy, ctypes.byref(cookie))
        return found

    def pointer(self):
        """(x, y, button mask) in root coordinates."""
        root, child = Window(), Window()
        rx, ry, wx, wy = (ctypes.c_int() for _ in range(4))
        mask = ctypes.c_uint()
        self.x.XQueryPointer(self.dpy, self.root, ctypes.byref(root), ctypes.byref(child), ctypes.byref(rx),
                             ctypes.byref(ry), ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(mask))
        return rx.value, ry.value, mask.value

    def property(self, window, name, length=1024):
        """(format, values) of a window property. format 32 gives integers, 8 gives bytes."""
        actual_type, actual_format = Atom(), ctypes.c_int()
        count, after, data = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_void_p()
        status = self.x.XGetWindowProperty(self.dpy, window, self.atom(name), 0, length, 0, ANY_PROPERTY_TYPE,
                                           ctypes.byref(actual_type), ctypes.byref(actual_format),
                                           ctypes.byref(count), ctypes.byref(after), ctypes.byref(data))
        try:
            if status != 0 or not data.value or not actual_type.value:
                return None
            if actual_format.value == 32:
                return 32, list(ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[:count.value])
            if actual_format.value == 8:
                return 8, ctypes.string_at(data, count.value)
            return actual_format.value, []
        finally:
            if data.value:
                self.x.XFree(data)

    def cardinal(self, window, name):
        value = self.property(window, name, 1)
        return value[1][0] if value and value[0] == 32 and value[1] else None

    def client_at(self, x, y):
        """The managed window at root point (x, y): (client, frame), or (None, None).

        Scans the root's children top-down in stacking order and skips
        override-redirect windows, so the drag icon a source floats under the
        pointer (GTK, Qt) and menus or tooltips never hide the window below."""
        for frame in reversed(self.children(self.root)):
            attrs = Attributes()
            if not self.x.XGetWindowAttributes(self.dpy, frame, ctypes.byref(attrs)):
                continue
            if attrs.override_redirect or attrs.map_state != IS_VIEWABLE:
                continue
            border = attrs.border_width
            if not (attrs.x <= x < attrs.x + attrs.width + 2 * border
                    and attrs.y <= y < attrs.y + attrs.height + 2 * border):
                continue
            client = self.find_client(frame, 4)
            return client, frame
        return None, None

    def find_client(self, window, depth):
        """window itself or the first descendant with WM_STATE (the client inside a WM frame)."""
        if self.property(window, 'WM_STATE', 1):
            return window
        if depth:
            for child in reversed(self.children(window)):
                found = self.find_client(child, depth - 1)
                if found:
                    return found
        return None

    def bounds(self, window):
        attrs = Attributes()
        if not self.x.XGetWindowAttributes(self.dpy, window, ctypes.byref(attrs)):
            return None, None
        x, y, child = ctypes.c_int(), ctypes.c_int(), Window()
        if not self.x.XTranslateCoordinates(self.dpy, window, self.root, 0, 0, ctypes.byref(x), ctypes.byref(y),
                                            ctypes.byref(child)):
            return None, None
        return {'x': x.value, 'y': y.value, 'width': attrs.width, 'height': attrs.height}, attrs.map_state

    def app(self, window):
        value = self.property(window, 'WM_CLASS', 256)
        if not value or value[0] != 8:
            return None
        parts = [part.decode('utf-8', 'replace') for part in value[1].split(b'\0') if part]
        return parts[-1] if parts else None

    def hidden(self, window):
        state = self.property(window, '_NET_WM_STATE', 64)
        return bool(state and state[0] == 32 and self.atom('_NET_WM_STATE_HIDDEN') in state[1])

    def on_current_desktop(self, window):
        current = self.cardinal(self.root, '_NET_CURRENT_DESKTOP')
        desktop = self.cardinal(window, '_NET_WM_DESKTOP')
        return current is None or desktop is None or desktop in (current, 0xFFFFFFFF)

    def top_level_clients(self):
        listed = self.property(self.root, '_NET_CLIENT_LIST', 4096)
        if listed and listed[0] == 32:
            return listed[1]
        # No EWMH window manager: WM_STATE windows among the root's children and grandchildren.
        found = []
        for child in self.children(self.root):
            if self.property(child, 'WM_STATE', 1):
                found.append(child)
            else:
                found.extend(w for w in self.children(child) if self.property(w, 'WM_STATE', 1))
        return found

    def children(self, window):
        root, parent, data, count = Window(), Window(), ctypes.c_void_p(), ctypes.c_uint()
        if not self.x.XQueryTree(self.dpy, window, ctypes.byref(root), ctypes.byref(parent), ctypes.byref(data),
                                 ctypes.byref(count)):
            return []
        try:
            return list(ctypes.cast(data, ctypes.POINTER(Window))[:count.value]) if data.value else []
        finally:
            if data.value:
                self.x.XFree(data)

    def peers(self, window, pid):
        rects = []
        for other in self.top_level_clients():
            if other == window or self.cardinal(other, '_NET_WM_PID') != pid:
                continue
            rect, map_state = self.bounds(other)
            if rect and map_state == IS_VIEWABLE and not self.hidden(other) and self.on_current_desktop(other):
                rects.append(rect)
        return rects

    def describe(self, x, y):
        """The window object at root point (x, y) (docs/SENSOR.md, section 2), or None."""
        window, frame = self.client_at(x, y)
        if not window:
            return None
        cached_id, cached_at, cached = self.described
        at = time.monotonic()
        if cached_id == window and at - cached_at < common.MOVE_MAX_INTERVAL:
            return cached
        pid = self.cardinal(window, '_NET_WM_PID')
        rect, _ = self.bounds(window)
        if not pid or not rect:
            return None
        frame_rect = self.bounds(frame)[0] if frame and frame != window else rect
        described = {'id': str(window), 'pid': pid, 'app': self.app(window), 'bounds': rect, 'scale': 1.0,
                     'peers': self.peers(window, pid), 'clients': self.cache.get(window, pid),
                     'frame': frame_rect}
        self.described = (window, at, described)
        return described

    def kind(self):
        """'files', 'other' or 'unknown' from the source's XdndTypeList, without reading any data."""
        if not self.owner:
            return 'unknown'
        listed = self.property(self.owner, 'XdndTypeList', 256)
        if not listed or listed[0] != 32 or not listed[1]:
            return 'unknown'
        names = {self.atom_name(atom) for atom in listed[1]}
        return 'files' if names & FILE_TYPES else 'other'


CAPABILITIES = ['drag', 'click', 'window', 'clients']


def run(display_name=None):
    try:
        backend = X11(display_name)
    except (OSError, RuntimeError) as error:
        common.hello('linux-x11', 'x11-root', [])
        common.status('error', 'X11 sensor inert: %s' % error)
        return
    common.hello('linux-x11', 'x11-root', CAPABILITIES)
    tracker = Tracker(backend.describe, backend.kind, common.emit, common.all_herdr_clients)
    fd = backend.fileno()
    x = y = 0
    while True:
        try:
            select.select([fd], [], [], POLL_INTERVAL)
        except (OSError, ValueError):
            tracker.cancel(x, y)
            return
        x, y, mask = backend.pointer()
        signal = None
        for what, value in backend.events():
            if what == 'drag':
                signal = value
                if value == 'started':
                    backend.cache.reset()
                    backend.described = (None, 0.0, None)
            else:
                # Replay button edges a poll would miss, then the drag signal with the polled state.
                tracker.step(time.monotonic(), x, y, value)
        tracker.step(time.monotonic(), x, y, bool(mask & BUTTON1_MASK), signal)
