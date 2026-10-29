"""Windows sensor tests. They run on macOS and Linux: Win32 is faked at two levels,
fake DLL objects under RealWin32 (ctypes marshalling) and a FakeWin32 facade under
the sensor loop (behaviour)."""
import ctypes
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from spring_load import contract
from sensors.windows import build, logic, sensor, structs, win32
from sensors.windows.logic import DragDetector, MoveThrottle, Process, Rect, Tick, TopWindow

# Fake DLLs: plain objects whose attributes are Python functions. ctypes.byref
# arguments arrive as CArgObject; ._obj is the structure to fill in.

class FakeDLL:
    def __init__(self, **functions):
        for name, fn in functions.items():
            setattr(self, name, fn)

def obj(arg):
    return arg._obj

def all_dlls(**overrides):
    """Fake DLLs with every bound function present (a stub returning 0)."""
    dlls = {}
    for dll, table in win32.SIGNATURES.items():
        functions = {name: (lambda *a: 0) for name in table}
        functions.update(overrides.get(dll, {}))
        dlls[dll] = FakeDLL(**functions)
    return dlls

class StructLayoutTest(unittest.TestCase):
    def test_sizes_match_64_bit_windows(self):
        expected = {structs.POINT: 8, structs.RECT: 16, structs.CURSORINFO: 24, structs.GUITHREADINFO: 72,
                    structs.PROCESSENTRY32W: 568, structs.CONSOLE_SCREEN_BUFFER_INFO: 22, structs.FILETIME: 8,
                    structs.UNICODE_STRING: 16}
        for struct, size in expected.items():
            self.assertEqual(ctypes.sizeof(struct), size, struct.__name__)

    def test_offsets(self):
        self.assertEqual(structs.PROCESSENTRY32W.szExeFile.offset, 44)
        self.assertEqual(structs.PROCESSENTRY32W.th32ParentProcessID.offset, 32)
        self.assertEqual(structs.GUITHREADINFO.hwndCapture.offset, 24)
        self.assertEqual(structs.CURSORINFO.hCursor.offset, 8)
        self.assertEqual(structs.CONSOLE_SCREEN_BUFFER_INFO.srWindow.offset, 10)

    def test_no_platform_dependent_field_types(self):
        for struct in (structs.POINT, structs.RECT, structs.CURSORINFO, structs.GUITHREADINFO,
                       structs.PROCESSENTRY32W, structs.CONSOLE_SCREEN_BUFFER_INFO):
            for name, kind in struct._fields_:
                self.assertIsNot(kind, ctypes.c_wchar, name)
                if kind in (ctypes.c_long, ctypes.c_ulong):
                    # Only ULONG_PTR (c_size_t, an alias of c_ulong on LP64) may be one.
                    self.assertEqual((name, ctypes.sizeof(kind)), ('th32DefaultHeapID', ctypes.sizeof(ctypes.c_void_p)))

    def test_wide_text(self):
        entry = structs.PROCESSENTRY32W()
        for i, ch in enumerate('herdr.exe'):
            entry.szExeFile[i] = ord(ch)
        self.assertEqual(structs.wide_text(entry.szExeFile), 'herdr.exe')

class BindingTest(unittest.TestCase):
    def test_every_signature_is_declared(self):
        fn = win32.bind(all_dlls())
        for table in win32.SIGNATURES.values():
            for name, (restype, argtypes) in table.items():
                self.assertIs(fn[name].restype, restype)
                self.assertEqual(fn[name].argtypes, argtypes)

    def test_struct_arguments_are_typed_pointers(self):
        # byref() is only portable for POINTER(...) argtypes.
        for name in ('GetCursorPos', 'GetCursorInfo', 'GetGUIThreadInfo', 'GetWindowRect'):
            argtypes = win32.SIGNATURES['user32'][name][1]
            self.assertTrue(any(issubclass(a, ctypes._Pointer) for a in argtypes), name)

    @unittest.skipIf(sys.platform == 'win32', 'checks the non-Windows behaviour')
    def test_loading_real_dlls_is_lazy(self):
        # The modules imported above; only constructing RealWin32 needs ctypes.WinDLL.
        with self.assertRaises(AttributeError):
            win32.RealWin32()

class RealWin32Test(unittest.TestCase):
    def test_cursor_and_buttons(self):
        def pos(p):
            obj(p).x, obj(p).y = -1200, 340
            return 1
        swapped = {'flag': 0}
        def metric(i):
            return {win32.SM_SWAPBUTTON: swapped['flag'], win32.SM_CXDRAG: 4, win32.SM_CYDRAG: 4}.get(i, 0)
        keys = {win32.VK_LBUTTON: -32768, win32.VK_RBUTTON: 0}
        w = win32.RealWin32(all_dlls(user32={'GetCursorPos': pos, 'GetSystemMetrics': metric,
                                             'GetAsyncKeyState': lambda vk: keys.get(vk, 0)}))
        self.assertEqual(w.cursor_pos(), (-1200, 340))
        self.assertTrue(w.primary_held())
        swapped['flag'] = 1 # left-handed: the primary button is the right one
        self.assertFalse(w.primary_held())
        self.assertEqual(w.drag_threshold(), (4, 4))

    def test_cursor_standard(self):
        current = {'h': 0x10003}
        def info(p):
            obj(p).flags, obj(p).hCursor = 1, current['h']
            return 1
        loads = lambda module, cursor: 0x10003 if cursor == 32512 else 0x20000 + cursor
        w = win32.RealWin32(all_dlls(user32={'GetCursorInfo': info, 'LoadCursorW': loads}))
        self.assertTrue(w.cursor_standard())
        current['h'] = 0x5555 # an OLE drag cursor
        self.assertFalse(w.cursor_standard())

    def test_capture_hidden(self):
        capture = {'hwnd': 0}
        def thread_info(tid, p):
            obj(p).hwndCapture = capture['hwnd']
            return 1
        visible = {0x77: 1, 0x88: 0}
        w = win32.RealWin32(all_dlls(user32={'GetGUIThreadInfo': thread_info,
                                             'IsWindowVisible': lambda h: visible.get(h, 0)}))
        self.assertFalse(w.capture_hidden(12))
        capture['hwnd'] = 0x77
        self.assertFalse(w.capture_hidden(12))
        capture['hwnd'] = 0x88
        self.assertTrue(w.capture_hidden(12))
        self.assertIsNone(w.capture_hidden(None))

    def test_windows_in_z_order(self):
        names = {0x10: 'CASCADIA_HOSTING_WINDOW_CLASS', 0x20: 'Progman'}
        def enum(callback, lparam):
            for h in (0x10, 0x20):
                callback(h, lparam)
            return 1
        def pid(h, out):
            obj(out).value = 4242 if h == 0x10 else 1
            return 7
        def class_name(h, buffer, size):
            ctypes.memmove(buffer, names[h].encode('utf-16-le') + b'\0\0', 2 * len(names[h]) + 2)
            return len(names[h])
        def dwm(h, attribute, value, size):
            if attribute == win32.DWMWA_EXTENDED_FRAME_BOUNDS:
                r = obj(value)
                r.left, r.top, r.right, r.bottom = 100, 50, 1100, 850
                return 0
            return 1 # cloaked query fails: not cloaked
        w = win32.RealWin32(all_dlls(user32={'EnumWindows': enum, 'GetWindowThreadProcessId': pid,
                                             'GetClassNameW': class_name, 'IsWindowVisible': lambda h: 1,
                                             'IsIconic': lambda h: 0, 'GetWindowLongW': lambda h, i: 0},
                                     dwmapi={'DwmGetWindowAttribute': dwm}))
        windows = w.top_windows()
        self.assertEqual([x.hwnd for x in windows], [0x10, 0x20])
        self.assertEqual(windows[0].cls, 'CASCADIA_HOSTING_WINDOW_CLASS')
        self.assertEqual((windows[0].pid, windows[0].tid), (4242, 7))
        self.assertEqual(windows[0].bounds, Rect(100, 50, 1000, 800))
        self.assertFalse(windows[0].cloaked)

    def test_root_at_passes_point_by_value(self):
        seen = []
        def from_point(point):
            seen.append((point.x, point.y))
            return 0x31
        w = win32.RealWin32(all_dlls(user32={'WindowFromPoint': from_point, 'GetAncestor': lambda h, f: 0x30}))
        self.assertEqual(w.root_at(5.7, 9), 0x30)
        self.assertEqual(seen, [(5, 9)])

    def test_process_snapshot(self):
        table = [(4, 0, 'System'), (900, 4, 'WindowsTerminal.exe'), (950, 900, 'herdr.exe')]
        cursor = {'i': 0}
        closed = []
        def fill(p):
            if cursor['i'] >= len(table):
                return 0
            pid, ppid, exe = table[cursor['i']]
            entry = obj(p)
            self.assertEqual(entry.dwSize, 568)
            entry.th32ProcessID, entry.th32ParentProcessID = pid, ppid
            for i in range(len(entry.szExeFile)):
                entry.szExeFile[i] = ord(exe[i]) if i < len(exe) else 0
            cursor['i'] += 1
            return 1
        w = win32.RealWin32(all_dlls(kernel32={'CreateToolhelp32Snapshot': lambda f, p: 0x99,
                                               'Process32FirstW': lambda h, p: fill(p),
                                               'Process32NextW': lambda h, p: fill(p),
                                               'CloseHandle': lambda h: closed.append(h) or 1}))
        self.assertEqual(w.processes(), [Process(*row) for row in table])
        self.assertEqual(closed, [0x99])
        w = win32.RealWin32(all_dlls(kernel32={'CreateToolhelp32Snapshot': lambda f, p: win32.INVALID_HANDLE}))
        self.assertIsNone(w.processes())

    def test_creation_time_and_unopenable_process(self):
        def times(h, created, *rest):
            obj(created).dwHighDateTime, obj(created).dwLowDateTime = 1, 5
            return 1
        closed = []
        w = win32.RealWin32(all_dlls(kernel32={'OpenProcess': lambda access, inherit, pid: 0x44 if pid == 1 else 0,
                                               'GetProcessTimes': times,
                                               'CloseHandle': lambda h: closed.append(h) or 1}))
        self.assertEqual(w.created(1), (1 << 32) | 5)
        self.assertIsNone(w.created(2)) # elevated or gone: unknown
        self.assertEqual(closed, [0x44])

    def test_command_line(self):
        text = '"C:\\Program Files\\herdr\\herdr.exe" --session work'.encode('utf-16-le')
        keep = ctypes.create_string_buffer(text, len(text))
        def query(handle, kind, buffer, size, needed):
            self.assertEqual(kind, win32.PROCESS_COMMAND_LINE_INFORMATION)
            if size < 64:
                obj(needed).value = 64
                return -1073741820 # STATUS_INFO_LENGTH_MISMATCH
            value = structs.UNICODE_STRING(len(text), len(text), ctypes.addressof(keep))
            ctypes.memmove(buffer, ctypes.byref(value), ctypes.sizeof(value))
            return 0
        w = win32.RealWin32(all_dlls(kernel32={'OpenProcess': lambda *a: 0x45, 'CloseHandle': lambda h: 1},
                                     ntdll={'NtQueryInformationProcess': query}))
        self.assertEqual(w.argv(10), ['C:\\Program Files\\herdr\\herdr.exe', '--session', 'work'])

    def test_console_size_runs_the_probe_detached(self):
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, stdout='{"cols": 239, "rows": 68}\n', stderr='')
        w = win32.RealWin32(all_dlls(), probe_argv=['pythonw.exe', 'sensor.exe', '--console-probe'], run=run)
        self.assertEqual(w.console_size(950), (239, 68))
        self.assertEqual(calls[0][0], ['pythonw.exe', 'sensor.exe', '--console-probe', '950'])
        self.assertEqual(calls[0][1]['creationflags'], win32.DETACHED_PROCESS)
        for output in ('null\n', 'garbage', '{"cols": 0, "rows": 5}'):
            w.run = lambda argv, **k: subprocess.CompletedProcess(argv, 0, stdout=output, stderr='')
            self.assertIsNone(w.console_size(950), output)
        def timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 2)
        w.run = timeout
        self.assertIsNone(w.console_size(950))

    def test_probe_console(self):
        log = []
        def info(handle, p):
            window = obj(p).srWindow
            window.Left, window.Top, window.Right, window.Bottom = 0, 100, 238, 167
            return 1
        kernel32 = all_dlls(kernel32={'FreeConsole': lambda: log.append('free') or 1,
                                      'AttachConsole': lambda pid: log.append(('attach', pid)) or 1,
                                      'CreateFileW': lambda name, *a: log.append(name) or 0x70,
                                      'GetConsoleScreenBufferInfo': info,
                                      'CloseHandle': lambda h: log.append(('close', h)) or 1})['kernel32']
        self.assertEqual(win32.probe_console(950, kernel32), (239, 68))
        self.assertEqual(log, ['free', ('attach', 950), 'CONOUT$', ('close', 0x70), 'free'])
        kernel32.AttachConsole = lambda pid: 0 # no console: the herdr server
        self.assertIsNone(win32.probe_console(951, kernel32))

class CommandLineTest(unittest.TestCase):
    def test_windows_rules(self):
        cases = {
            'herdr': ['herdr'],
            'herdr.exe --remote host': ['herdr.exe', '--remote', 'host'],
            '"C:\\a b\\herdr.exe" "x y" z': ['C:\\a b\\herdr.exe', 'x y', 'z'],
            'p a\\\\"b c"': ['p', 'a\\b c'],
            'p a\\"b': ['p', 'a"b'],
            'p a\\\\b': ['p', 'a\\\\b'],
            'p "a""b"': ['p', 'a"b'],
            '  p   ': ['p'],
            '': [],
        }
        for text, argv in cases.items():
            self.assertEqual(win32.split_command_line(text), argv, text)

def top(hwnd, pid, cls='CASCADIA_HOSTING_WINDOW_CLASS', bounds=(0, 0, 1000, 800), tid=1, **kw):
    return TopWindow(hwnd=hwnd, pid=pid, tid=tid, cls=cls, bounds=Rect(*bounds), **kw)

class PickWindowTest(unittest.TestCase):
    def test_skips_overlays_and_shell(self):
        windows = [top(1, 5, cls='SysDragImage'), top(2, 6, exstyle=logic.WS_EX_TRANSPARENT),
                   top(3, 7, exstyle=logic.WS_EX_TOOLWINDOW), top(4, 8, alpha=0), top(5, 9, cloaked=True),
                   top(6, 10, visible=False), top(7, 11, iconic=True), top(8, 900), top(9, 900, bounds=(1000, 0, 500, 500)),
                   top(10, 1, cls='Progman')]
        window, peers = logic.pick_window(windows, 10, 10)
        self.assertEqual(window.hwnd, 8)
        self.assertEqual(peers, [Rect(1000, 0, 500, 500)])
        self.assertEqual(logic.pick_window(windows, 5000, 5000), (None, []))

    def test_off_screen_window(self):
        screen = Rect(0, 0, 1920, 1080)
        window, _ = logic.pick_window([top(1, 2, bounds=(-5000, -5000, 100, 100))], -4990, -4990, screen)
        self.assertIsNone(window)

TIMES = lambda pid: pid # creation time grows with pid: parents are older

def wt(*sessions, herdr=()):
    """A Windows Terminal process tree: one OpenConsole per session."""
    procs = [Process(900, 1, 'WindowsTerminal.exe')]
    for i in range(sessions[0] if sessions else 1):
        procs.append(Process(910 + i, 900, 'OpenConsole.exe'))
        procs.append(Process(920 + i, 900, 'pwsh.exe'))
    for pid, parent in herdr:
        procs.append(Process(pid, parent, 'herdr.exe'))
    return procs

def sizes(table):
    return lambda pid: table.get(pid)

class FindClientsTest(unittest.TestCase):
    owner = Process(900, 1, 'WindowsTerminal.exe')

    def find(self, procs, console, owner=None, cls='CASCADIA_HOSTING_WINDOW_CLASS', created=TIMES):
        return logic.find_clients(owner or self.owner, cls, procs, created, lambda pid: ['herdr'], sizes(console))

    def test_windows_terminal_one_tab(self):
        clients, reason = self.find(wt(1, herdr=[(950, 920)]), {950: (239, 68)})
        self.assertIsNone(reason)
        self.assertEqual(clients, [{'pid': 950, 'argv': ['herdr'], 'tty': None, 'rows': 68, 'cols': 239,
                                    'xpixel': 0, 'ypixel': 0, 'foreground': True}])

    def test_windows_terminal_second_tab_is_ambiguous(self):
        # herdr in one tab, a plain shell in another: the visible tab is unknown.
        clients, reason = self.find(wt(2, herdr=[(950, 920)]), {950: (239, 68)})
        self.assertEqual(reason, 'ambiguous_sessions')
        self.assertEqual([c['foreground'] for c in clients], [False])

    def test_two_herdr_tabs(self):
        clients, reason = self.find(wt(2, herdr=[(950, 920), (951, 921)]), {950: (239, 68), 951: (239, 68)})
        self.assertEqual(reason, 'ambiguous_clients')
        self.assertEqual([c['foreground'] for c in clients], [False, False])

    def test_split_pane_counts_as_a_second_session(self):
        _, reason = self.find(wt(2, herdr=[(950, 920)]), {950: (120, 68)})
        self.assertEqual(reason, 'ambiguous_sessions')

    def test_server_without_console_is_not_a_client(self):
        procs = wt(1, herdr=[(950, 920), (960, 950)]) # the client started a server below it
        clients, reason = self.find(procs, {950: (239, 68)})
        self.assertEqual([c['pid'] for c in clients], [950])
        self.assertIsNone(reason)

    def test_reused_parent_pid_breaks_the_chain(self):
        procs = wt(1, herdr=[(950, 920)])
        created = lambda pid: 10_000 if pid == 920 else pid # the "parent" is newer than its child
        clients, _ = self.find(procs, {950: (239, 68)}, created=created)
        self.assertEqual(clients, [])

    def test_unknown_creation_time_breaks_the_chain(self):
        clients, _ = self.find(wt(1, herdr=[(950, 920)]), {950: (239, 68)},
                               created=lambda pid: None if pid == 900 else pid)
        self.assertEqual(clients, [])

    def test_unrecognised_terminal(self):
        owner = Process(800, 1, 'someterm.exe')
        procs = [owner, Process(810, 800, 'sh.exe'), Process(850, 810, 'herdr.exe')]
        clients, reason = self.find(procs, {850: (80, 24)}, owner=owner, cls='SomeTermWindow')
        self.assertEqual(reason, 'unknown_terminal')
        self.assertFalse(clients[0]['foreground'])

    def test_classic_console_window(self):
        # GetWindowThreadProcessId on a console window may report the attached
        # client itself rather than conhost.
        owner = Process(850, 700, 'herdr.exe')
        clients, reason = self.find([Process(700, 1, 'explorer.exe'), owner], {850: (120, 30)},
                                    owner=owner, cls=logic.CONSOLE_CLASS)
        self.assertIsNone(reason)
        self.assertTrue(clients[0]['foreground'])

    def test_handed_off_console_is_not_below_the_terminal(self):
        # Default-terminal handoff: herdr started from Explorer, shown in WT.
        procs = wt(1) + [Process(700, 1, 'explorer.exe'), Process(950, 700, 'herdr.exe')]
        clients, reason = self.find(procs, {950: (239, 68)})
        self.assertEqual((clients, reason), ([], None))

    def test_unreadable_argv_is_an_empty_list(self):
        clients, _ = logic.find_clients(self.owner, 'X', wt(1, herdr=[(950, 920)]), TIMES, lambda pid: None,
                                        sizes({950: (1, 1)}))
        self.assertEqual(clients[0]['argv'], [])

def ticks(detector, *steps):
    out = []
    for step in steps:
        out += detector.update(step)
    return [e[0] if e[0] != 'drag_end' else ('drag_end', e[2]) for e in out]

class DragDetectorTest(unittest.TestCase):
    def test_click(self):
        d = DragDetector()
        self.assertEqual(ticks(d, Tick(0, 10, 10, True, window=1), Tick(0.1, 12, 11, True), Tick(0.15, 12, 11, False)),
                         ['click'])

    def test_slow_or_far_press_is_not_a_click(self):
        d = DragDetector()
        self.assertEqual(ticks(d, Tick(0, 10, 10, True), Tick(0.7, 10, 10, False)), [])
        self.assertEqual(ticks(d, Tick(1, 10, 10, True), Tick(1.1, 30, 10, True, capture_hidden=False,
                                                                cursor_standard=True), Tick(1.2, 30, 10, False)), [])

    def test_text_selection_is_not_a_drag(self):
        d = DragDetector()
        steps = [Tick(0, 10, 10, True, window=1)] + [
            Tick(0.05 * i, 10 + 20 * i, 10, True, window=1, capture_hidden=False, cursor_standard=True)
            for i in range(1, 8)] + [Tick(0.5, 150, 10, False)]
        self.assertEqual(ticks(d, *steps), [])

    def test_ole_drag_released(self):
        d = DragDetector()
        events = ticks(d, Tick(0, 10, 10, True, window=1),
                       Tick(0.05, 12, 12, True, window=1, capture_hidden=True), # within the threshold
                       Tick(0.10, 40, 30, True, window=1, capture_hidden=True),
                       Tick(0.20, 400, 300, True, window=2, capture_hidden=True),
                       Tick(0.30, 400, 300, False))
        self.assertEqual(events, ['drag_start', ('drag_end', 'released')])

    def test_drag_cursor_over_another_window(self):
        d = DragDetector()
        events = ticks(d, Tick(0, 10, 10, True, window=1),
                       Tick(0.1, 60, 10, True, window=1, capture_hidden=None, cursor_standard=False),
                       Tick(0.2, 600, 10, True, window=2, capture_hidden=None, cursor_standard=False))
        self.assertEqual(events, ['drag_start'])

    def test_escape_cancels_until_release(self):
        d = DragDetector()
        events = ticks(d, Tick(0, 10, 10, True, window=1), Tick(0.1, 60, 60, True, capture_hidden=True),
                       Tick(0.2, 70, 60, True, escape=True, capture_hidden=True),
                       Tick(0.3, 90, 60, True, capture_hidden=True), Tick(0.4, 90, 60, False),
                       Tick(1.0, 5, 5, True))
        self.assertEqual(events, ['drag_start', ('drag_end', 'cancelled')])
        self.assertEqual(d.state, 'pressed')

    def test_capture_lost_while_held_cancels(self):
        d = DragDetector()
        events = ticks(d, Tick(0, 10, 10, True), Tick(0.1, 60, 60, True, capture_hidden=True),
                       Tick(0.2, 80, 60, True, capture_hidden=False))
        self.assertEqual(events, ['drag_start', ('drag_end', 'cancelled')])

    def test_escape_before_drag_blocks(self):
        d = DragDetector()
        events = ticks(d, Tick(0, 10, 10, True), Tick(0.1, 12, 10, True, escape=True),
                       Tick(0.2, 90, 90, True, capture_hidden=True), Tick(0.3, 90, 90, False))
        self.assertEqual(events, [])

class MoveThrottleTest(unittest.TestCase):
    def test_rates(self):
        m = MoveThrottle()
        self.assertTrue(m.due(0.0, (1, 1, 5)))
        self.assertFalse(m.due(0.02, (2, 1, 5))) # under 30 ms
        self.assertTrue(m.due(0.031, (2, 1, 5))) # changed
        self.assertFalse(m.due(0.2, (2, 1, 5))) # unchanged, under 250 ms
        self.assertTrue(m.due(0.29, (2, 1, 5))) # heartbeat

class FakeWin32:
    """Scripted facade: tests set `x`, `y`, `held`, and the window and process tables."""
    def __init__(self, windows, processes, consoles, dpi=True):
        self.windows, self.process_table, self.consoles, self.dpi = windows, processes, consoles, dpi
        self.x = self.y = 0
        self.held = self.escape = False
        self.capture = None
        self.cursor = True
        self.lookups = 0

    def set_dpi_awareness(self): return self.dpi
    def virtual_screen(self): return Rect(-1920, 0, 3840, 1080)
    def drag_threshold(self): return (4, 4)
    def cursor_pos(self): return (self.x, self.y)
    def primary_held(self): return self.held
    def escape_held(self): return self.escape
    def cursor_standard(self): return self.cursor
    def capture_hidden(self, tid): return self.capture
    def window(self, hwnd): return next(w for w in self.windows if w.hwnd == hwnd)
    def top_windows(self): return list(self.windows)
    def scale(self, hwnd): return 1.5
    def created(self, pid): return pid
    def argv(self, pid): return ['herdr']
    def console_size(self, pid): return self.consoles.get(pid)

    def root_at(self, x, y):
        hit = next((w for w in self.windows if w.visible and w.bounds.contains(x, y)), None)
        return hit.hwnd if hit else None

    def processes(self):
        self.lookups += 1
        return list(self.process_table)

class Clock:
    def __init__(self):
        self.t = 100.0
    def __call__(self):
        return self.t

def run_script(win, steps):
    """steps: (seconds, changes) pairs; one tick per 15 ms in between."""
    out, clock = io.StringIO(), Clock()
    s = sensor.Sensor(win, out=out, clock=clock)
    s.begin()
    for seconds, changes in steps:
        for name, value in changes.items():
            setattr(win, name, value)
        end = clock.t + seconds
        while clock.t < end:
            s.tick()
            clock.t = round(clock.t + 0.015, 6)
    return [json.loads(line) for line in out.getvalue().splitlines()], out.getvalue()

EXPLORER = top(0x500, 700, cls='CabinetWClass', bounds=(-1900, 0, 1500, 1000), tid=31)
TERMINAL = top(0x600, 900, bounds=(0, 0, 1600, 1000), tid=41)

class SensorLoopTest(unittest.TestCase):
    def drag(self, processes, consoles):
        win = FakeWin32([EXPLORER, TERMINAL], processes + [Process(700, 1, 'explorer.exe')], consoles)
        events, raw = run_script(win, [
            (0.05, {'x': -1000, 'y': 500, 'held': True}),
            (0.05, {'x': -980, 'capture': True, 'cursor': False}),
            (0.30, {'x': 800, 'y': 400}),
            (0.60, {'x': 805}),
            (0.05, {'held': False, 'capture': False}),
        ])
        ok, message = contract.check_stream(raw.splitlines())
        self.assertTrue(ok, message)
        return events, win

    def test_drag_stream(self):
        events, win = self.drag(wt(1, herdr=[(950, 920)]), {950: (239, 68)})
        types = [e['type'] for e in events]
        self.assertEqual(types[0], 'hello')
        self.assertEqual(types[1], 'drag_start')
        self.assertEqual(types[-1], 'drag_end')
        self.assertEqual(events[-1]['reason'], 'released')
        self.assertEqual(events[1]['kind'], 'unknown')
        self.assertEqual(events[1]['window']['app'], 'explorer.exe')
        moves = [e for e in events if e['type'] == 'drag_move']
        over = [m for m in moves if m['window'] and m['window']['id'] == str(0x600)]
        self.assertTrue(over)
        client = over[0]['window']['clients'][0]
        self.assertEqual((client['pid'], client['cols'], client['rows'], client['foreground']), (950, 239, 68, True))
        self.assertEqual(over[0]['window']['app'], 'WindowsTerminal.exe')
        self.assertEqual(over[0]['window']['scale'], 1.5)
        gaps = [b['t'] - a['t'] for a, b in zip(moves, moves[1:])]
        self.assertGreaterEqual(min(gaps), 0.03 - 1e-9)
        self.assertLessEqual(max(gaps), 0.25 + 0.016)
        # Process lookups: one at drag start, one when the window changed.
        self.assertEqual(win.lookups, 2)
        self.assertNotIn('status', types)

    def test_windows_terminal_with_two_tabs_is_refused(self):
        events, _ = self.drag(wt(2, herdr=[(950, 920)]), {950: (239, 68)})
        over = [e for e in events if e['type'] == 'drag_move' and e['window'] and e['window']['id'] == str(0x600)]
        self.assertTrue(all(not c['foreground'] for e in over for c in e['window']['clients']))
        statuses = [e for e in events if e['type'] == 'status']
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]['level'], 'warn')
        self.assertIn('one tab, no splits', statuses[0]['message'])

    def test_click_in_terminal(self):
        win = FakeWin32([EXPLORER, TERMINAL], wt(1, herdr=[(950, 920)]), {950: (239, 68)})
        events, raw = run_script(win, [(0.03, {'x': 300, 'y': 20}), (0.09, {'held': True}), (0.03, {'held': False})])
        self.assertTrue(contract.check_stream(raw.splitlines())[0])
        self.assertEqual([e['type'] for e in events], ['hello', 'click'])
        self.assertEqual(events[1]['window']['clients'][0]['pid'], 950)

    def test_selection_inside_the_terminal_sends_nothing(self):
        win = FakeWin32([EXPLORER, TERMINAL], wt(1, herdr=[(950, 920)]), {950: (239, 68)})
        events, _ = run_script(win, [(0.03, {'x': 300, 'y': 20, 'held': True}),
                                     (0.5, {'x': 900, 'capture': False, 'cursor': True}), (0.03, {'held': False})])
        self.assertEqual([e['type'] for e in events], ['hello'])

    def test_no_dpi_awareness_means_no_drag_capability(self):
        out = io.StringIO()
        s = sensor.Sensor(FakeWin32([], [], {}, dpi=False), out=out, clock=Clock())
        self.assertFalse(s.begin())
        lines = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertNotIn('drag', lines[0]['capabilities'])
        self.assertEqual(lines[1]['level'], 'error')

    def test_broken_stdout_stops(self):
        class Broken:
            def write(self, text): raise BrokenPipeError()
            def flush(self): pass
        with self.assertRaises(sensor.Stopped):
            sensor.Sensor(FakeWin32([], [], {}), out=Broken(), clock=Clock()).begin()

class EntryPointTest(unittest.TestCase):
    def test_check(self):
        for argv in ([sys.executable, str(ROOT/'sensors/windows/run_sensor.py'), '--check'],
                     [sys.executable, '-m', 'sensors.windows', '--check']):
            result = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(contract.check_stream(result.stdout.splitlines()), (True, 'ok'))
            self.assertEqual(json.loads(result.stdout)['capabilities'], sensor.CAPABILITIES)

    @unittest.skipIf(sys.platform == 'win32', 'checks the non-Windows behaviour')
    def test_elsewhere_reports_and_exits_on_eof(self):
        result = subprocess.run([sys.executable, str(ROOT/'sensors/windows/run_sensor.py')], input='',
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [json.loads(l) for l in result.stdout.splitlines()]
        self.assertEqual(lines[0]['capabilities'], [])
        self.assertEqual(lines[1]['type'], 'status')
        self.assertEqual(contract.check_stream(result.stdout.splitlines()), (True, 'ok'))

    def test_loader_starts_it(self):
        from spring_load.sensor import SensorProcess
        process = SensorProcess([sys.executable, str(ROOT/'sensors/windows/run_sensor.py')])
        try:
            first = process.next(timeout=10)
            self.assertEqual((first['type'], first['sensor'], first['coords']), ('hello', 'windows', 'windows-virtual'))
        finally:
            process.close()

class BuildTest(unittest.TestCase):
    def test_launcher_and_zip_application(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)/'herdr-spring-load-sensor-windows.exe'
            build.build(out, python=sys.executable, launcher=b'MZ-launcher-stub')
            data = out.read_bytes()
            self.assertTrue(data.startswith(b'MZ-launcher-stub#!"' + sys.executable.encode() + b'"\n'))
            result = subprocess.run([sys.executable, str(out), '--check'], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['sensor'], 'windows')
            self.assertFalse(out.with_name(out.name + '.tmp').exists())

    def test_finds_pip_launcher(self):
        launcher = build.find_launcher(build.launcher_name('AMD64'))
        if launcher is None:
            self.skipTest('no pip or bundled pip wheel in this Python')
        self.assertTrue(launcher.startswith(b'MZ'))
        self.assertEqual(build.launcher_name('ARM64'), 'w64-arm.exe')

    def test_missing_launcher_is_a_clear_error(self):
        original = build.find_launcher
        build.find_launcher = lambda name: None
        try:
            with tempfile.TemporaryDirectory() as temp, self.assertRaises(RuntimeError) as caught:
                build.build(Path(temp)/'s.exe', python='python.exe')
            self.assertIn('ensurepip', str(caught.exception))
        finally:
            build.find_launcher = original

if __name__ == '__main__':
    unittest.main()
