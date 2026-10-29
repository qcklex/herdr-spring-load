"""The only module that calls Win32. Read-only queries, no hooks, no input.

RealWin32 takes its DLLs as a mapping so tests can pass fakes. On Windows the
default loader opens user32, kernel32, dwmapi and ntdll; nothing is loaded at
import time, so this module imports on any platform."""
import ctypes
import json
import subprocess

from .structs import (CONSOLE_SCREEN_BUFFER_INFO, CURSORINFO, DWORD, FILETIME, GUITHREADINFO, HANDLE, LONG,
                     PARAMS_ENVIRONMENT, PARAMS_ENVIRONMENT_SIZE, PEB_PROCESS_PARAMETERS, POINT,
                     PROCESS_BASIC_INFORMATION, PROCESSENTRY32W, RECT, UNICODE_STRING, filetime_epoch,
                     filetime_value, wide_text)
from .logic import Process, Rect, TopWindow, parse_environment

BOOL = ctypes.c_int32
BYTE = ctypes.c_uint8
UINT = ctypes.c_uint32
LPARAM = ctypes.c_ssize_t
P = ctypes.POINTER
WINFUNCTYPE = getattr(ctypes, 'WINFUNCTYPE', ctypes.CFUNCTYPE) # same ABI on 64-bit Windows
ENUM_PROC = WINFUNCTYPE(BOOL, HANDLE, LPARAM)

SM_SWAPBUTTON, SM_CXDRAG, SM_CYDRAG = 23, 68, 69
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
VK_LBUTTON, VK_RBUTTON, VK_ESCAPE = 0x01, 0x02, 0x1B
CURSOR_SHOWING = 0x1
GWL_EXSTYLE = -20
GA_ROOT = 2
WS_EX_LAYERED = 0x00080000
LWA_ALPHA = 0x2
DWMWA_CLOAKED, DWMWA_EXTENDED_FRAME_BOUNDS = 14, 9
TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE = ctypes.c_void_p(-1).value
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION, PROCESS_VM_READ = 0x0400, 0x0010
PROCESS_BASIC_INFORMATION_CLASS, PROCESS_WOW64_INFORMATION = 0, 26
PROCESS_COMMAND_LINE_INFORMATION = 60
MAX_ENVIRONMENT = 1 << 20 # bytes; a larger block is not trusted
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING = 1, 2, 3
DETACHED_PROCESS = 0x00000008
# System cursors (IDC_*). An OLE drag shows its own cursors, none of these.
SYSTEM_CURSORS = (32512, 32513, 32514, 32515, 32516, 32640, 32641, 32642, 32643, 32644, 32645, 32646,
                  32648, 32649, 32650, 32651, 32671, 32672)

SIGNATURES = {
    'user32': {
        'SetProcessDpiAwarenessContext': (BOOL, [HANDLE]),
        'GetSystemMetrics': (ctypes.c_int32, [ctypes.c_int32]),
        'GetAsyncKeyState': (ctypes.c_int16, [ctypes.c_int32]),
        'GetCursorPos': (BOOL, [P(POINT)]),
        'GetCursorInfo': (BOOL, [P(CURSORINFO)]),
        'LoadCursorW': (HANDLE, [HANDLE, HANDLE]),
        'GetGUIThreadInfo': (BOOL, [DWORD, P(GUITHREADINFO)]),
        'IsWindowVisible': (BOOL, [HANDLE]),
        'IsIconic': (BOOL, [HANDLE]),
        'EnumWindows': (BOOL, [ENUM_PROC, LPARAM]),
        'GetWindowThreadProcessId': (DWORD, [HANDLE, P(DWORD)]),
        'GetClassNameW': (ctypes.c_int32, [HANDLE, ctypes.c_void_p, ctypes.c_int32]),
        'GetWindowLongW': (LONG, [HANDLE, ctypes.c_int32]),
        'GetLayeredWindowAttributes': (BOOL, [HANDLE, P(DWORD), P(BYTE), P(DWORD)]),
        'GetWindowRect': (BOOL, [HANDLE, P(RECT)]),
        'WindowFromPoint': (HANDLE, [POINT]),
        'GetAncestor': (HANDLE, [HANDLE, UINT]),
        'GetDpiForWindow': (UINT, [HANDLE]),
    },
    'dwmapi': {
        'DwmGetWindowAttribute': (LONG, [HANDLE, DWORD, ctypes.c_void_p, DWORD]),
    },
    'kernel32': {
        'CreateToolhelp32Snapshot': (HANDLE, [DWORD, DWORD]),
        'Process32FirstW': (BOOL, [HANDLE, P(PROCESSENTRY32W)]),
        'Process32NextW': (BOOL, [HANDLE, P(PROCESSENTRY32W)]),
        'OpenProcess': (HANDLE, [DWORD, BOOL, DWORD]),
        'GetProcessTimes': (BOOL, [HANDLE, P(FILETIME), P(FILETIME), P(FILETIME), P(FILETIME)]),
        'CloseHandle': (BOOL, [HANDLE]),
        'FreeConsole': (BOOL, []),
        'AttachConsole': (BOOL, [DWORD]),
        'CreateFileW': (HANDLE, [ctypes.c_wchar_p, DWORD, DWORD, ctypes.c_void_p, DWORD, DWORD, HANDLE]),
        'GetConsoleScreenBufferInfo': (BOOL, [HANDLE, P(CONSOLE_SCREEN_BUFFER_INFO)]),
        'ReadProcessMemory': (BOOL, [HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, P(ctypes.c_size_t)]),
        'ProcessIdToSessionId': (BOOL, [DWORD, P(DWORD)]),
        'GetCurrentProcessId': (DWORD, []),
    },
    'ntdll': {
        'NtQueryInformationProcess': (LONG, [HANDLE, UINT, ctypes.c_void_p, UINT, P(UINT)]),
    },
}

def load_dlls():
    return {name: ctypes.WinDLL(name, use_last_error=True) for name in SIGNATURES}

def bind(dlls, only=None):
    """Declare argtypes and restype once; returns {name: function}."""
    functions = {}
    for dll, table in SIGNATURES.items():
        if only and dll not in only:
            continue
        for name, (restype, argtypes) in table.items():
            fn = getattr(dlls[dll], name)
            fn.restype, fn.argtypes = restype, argtypes
            functions[name] = fn
    return functions

def split_command_line(text):
    """CommandLineToArgvW's rules: whitespace separates arguments, quotes group,
    2n backslashes before a quote give n backslashes and a quote delimiter,
    2n+1 give n backslashes and a literal quote, "" inside quotes is a quote.
    The first argument (the program) treats backslashes literally."""
    args, i, n = [], 0, len(text)
    while i < n and text[i] in ' \t':
        i += 1
    if i >= n:
        return args
    # Program name: up to the next whitespace, or inside quotes.
    if text[i] == '"':
        end = text.find('"', i + 1)
        end = n if end < 0 else end
        args.append(text[i + 1:end])
        i = end + 1
    else:
        start = i
        while i < n and text[i] not in ' \t':
            i += 1
        args.append(text[start:i])
    while True:
        while i < n and text[i] in ' \t':
            i += 1
        if i >= n:
            return args
        current, quoted = [], False
        while i < n and (quoted or text[i] not in ' \t'):
            if text[i] == '\\':
                start = i
                while i < n and text[i] == '\\':
                    i += 1
                count = i - start
                if i < n and text[i] == '"':
                    current.append('\\' * (count // 2))
                    if count % 2:
                        current.append('"')
                        i += 1
                else:
                    current.append('\\' * count)
            elif text[i] == '"':
                if quoted and i + 1 < n and text[i + 1] == '"':
                    current.append('"')
                    i += 2
                else:
                    quoted = not quoted
                    i += 1
            else:
                current.append(text[i])
                i += 1
        args.append(''.join(current))

class RealWin32:
    def __init__(self, dlls=None, probe_argv=None, run=subprocess.run):
        self.fn = bind(dlls if dlls is not None else load_dlls())
        self.probe_argv = probe_argv # argv prefix; the pid is appended
        self.run = run
        self._standard = None

    # Input state

    def set_dpi_awareness(self):
        return bool(self.fn['SetProcessDpiAwarenessContext'](DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))

    def metric(self, index):
        return self.fn['GetSystemMetrics'](index)

    def virtual_screen(self):
        return Rect(self.metric(SM_XVIRTUALSCREEN), self.metric(SM_YVIRTUALSCREEN),
                    self.metric(SM_CXVIRTUALSCREEN), self.metric(SM_CYVIRTUALSCREEN))

    def drag_threshold(self):
        return (max(1, self.metric(SM_CXDRAG)), max(1, self.metric(SM_CYDRAG)))

    def _down(self, vk):
        return bool(self.fn['GetAsyncKeyState'](vk) & 0x8000)

    def primary_held(self):
        return self._down(VK_RBUTTON if self.metric(SM_SWAPBUTTON) else VK_LBUTTON)

    def escape_held(self):
        return self._down(VK_ESCAPE)

    def cursor_pos(self):
        point = POINT()
        return (point.x, point.y) if self.fn['GetCursorPos'](ctypes.byref(point)) else None

    def cursor_standard(self):
        """True for a system cursor, False for another one, None when hidden or unknown."""
        if self._standard is None:
            load = self.fn['LoadCursorW']
            self._standard = {h for h in (load(None, c) for c in SYSTEM_CURSORS) if h}
        info = CURSORINFO(cbSize=ctypes.sizeof(CURSORINFO))
        if not self.fn['GetCursorInfo'](ctypes.byref(info)) or not info.flags & CURSOR_SHOWING:
            return None
        return info.hCursor in self._standard

    def capture_hidden(self, tid):
        """Whether thread `tid` holds the mouse capture with a hidden window."""
        info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
        if not tid or not self.fn['GetGUIThreadInfo'](tid, ctypes.byref(info)):
            return None
        if not info.hwndCapture:
            return False
        return not self.fn['IsWindowVisible'](info.hwndCapture)

    # Windows

    def _dwm(self, hwnd, attribute, value):
        return self.fn['DwmGetWindowAttribute'](hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0

    def window(self, hwnd):
        pid = DWORD()
        tid = self.fn['GetWindowThreadProcessId'](hwnd, ctypes.byref(pid))
        name = (ctypes.c_uint16 * 256)()
        self.fn['GetClassNameW'](hwnd, ctypes.cast(name, ctypes.c_void_p), 256)
        rect = RECT()
        if not self._dwm(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, rect):
            self.fn['GetWindowRect'](hwnd, ctypes.byref(rect))
        cloaked = DWORD()
        exstyle = self.fn['GetWindowLongW'](hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
        alpha = None
        if exstyle & WS_EX_LAYERED:
            value, flags = BYTE(), DWORD()
            if self.fn['GetLayeredWindowAttributes'](hwnd, None, ctypes.byref(value), ctypes.byref(flags)) \
                    and flags.value & LWA_ALPHA:
                alpha = value.value
        return TopWindow(hwnd=hwnd, pid=pid.value, tid=tid, cls=wide_text(name),
                         bounds=Rect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top),
                         visible=bool(self.fn['IsWindowVisible'](hwnd)), iconic=bool(self.fn['IsIconic'](hwnd)),
                         cloaked=self._dwm(hwnd, DWMWA_CLOAKED, cloaked) and cloaked.value != 0,
                         exstyle=exstyle, alpha=alpha)

    def root_at(self, x, y):
        """The top-level window WindowFromPoint finds at a point, or None."""
        hit = self.fn['WindowFromPoint'](POINT(int(x), int(y)))
        return (self.fn['GetAncestor'](hit, GA_ROOT) or None) if hit else None

    def top_windows(self):
        """Top-level windows in z-order, topmost first."""
        handles = []
        callback = ENUM_PROC(lambda hwnd, _: handles.append(hwnd) or 1)
        self.fn['EnumWindows'](callback, 0)
        return [self.window(h) for h in handles if h]

    def scale(self, hwnd):
        dpi = self.fn['GetDpiForWindow'](hwnd)
        return dpi / 96 if dpi else None

    # Processes

    def processes(self):
        snapshot = self.fn['CreateToolhelp32Snapshot'](TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == INVALID_HANDLE:
            return None
        try:
            entry = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
            result = []
            ok = self.fn['Process32FirstW'](snapshot, ctypes.byref(entry))
            while ok:
                result.append(Process(entry.th32ProcessID, entry.th32ParentProcessID, wide_text(entry.szExeFile)))
                ok = self.fn['Process32NextW'](snapshot, ctypes.byref(entry))
            return result
        finally:
            self.fn['CloseHandle'](snapshot)

    def _open(self, pid):
        return self.fn['OpenProcess'](PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)

    def created(self, pid):
        handle = self._open(pid)
        if not handle:
            return None
        try:
            times = [FILETIME() for _ in range(4)]
            if not self.fn['GetProcessTimes'](handle, *(ctypes.byref(t) for t in times)):
                return None
            return filetime_value(times[0])
        finally:
            self.fn['CloseHandle'](handle)

    def started(self, pid):
        """Process start in seconds since the Unix epoch, or None."""
        return filetime_epoch(self.created(pid))

    def session_of(self, pid):
        """The logon (Terminal Services) session of a process, or None."""
        session = DWORD()
        return session.value if self.fn['ProcessIdToSessionId'](pid, ctypes.byref(session)) else None

    def own_session(self):
        return self.session_of(self.fn['GetCurrentProcessId']())

    def _read(self, handle, address, size):
        """Exactly `size` bytes of another process at `address`, or None."""
        if not address or size <= 0:
            return None
        buffer = ctypes.create_string_buffer(size)
        done = ctypes.c_size_t()
        if not self.fn['ReadProcessMemory'](handle, address, buffer, size, ctypes.byref(done)) or done.value != size:
            return None
        return buffer.raw

    def _read_u64(self, handle, address):
        data = self._read(handle, address, 8)
        return None if data is None else int.from_bytes(data, 'little')

    def environment(self, pid):
        """The contract's environment keys of process `pid`, read from its PEB:
        ProcessBasicInformation gives the PEB, whose ProcessParameters hold
        the environment block and its size. Same-user, non-elevated processes
        only; anything unexpected (a 32-bit sensor or target, an access error,
        a torn or oversized block) gives None, which the core treats as
        unknown and fails safe on."""
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            return None
        handle = self.fn['OpenProcess'](PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, 0, pid)
        if not handle:
            return None
        try:
            query = self.fn['NtQueryInformationProcess']
            wow64 = ctypes.c_uint64()
            if query(handle, PROCESS_WOW64_INFORMATION, ctypes.byref(wow64), 8, None) != 0 or wow64.value:
                return None
            basic = PROCESS_BASIC_INFORMATION()
            if query(handle, PROCESS_BASIC_INFORMATION_CLASS, ctypes.byref(basic), ctypes.sizeof(basic), None) != 0:
                return None
            if not basic.PebBaseAddress:
                return None
            params = self._read_u64(handle, basic.PebBaseAddress + PEB_PROCESS_PARAMETERS)
            if not params:
                return None
            block = self._read_u64(handle, params + PARAMS_ENVIRONMENT)
            size = self._read_u64(handle, params + PARAMS_ENVIRONMENT_SIZE)
            if not block or not size or size > MAX_ENVIRONMENT or size % 2:
                return None
            data = self._read(handle, block, size)
            if data is None:
                return None
            try:
                text = data.decode('utf-16-le')
            except UnicodeDecodeError:
                return None
            return parse_environment(text)
        finally:
            self.fn['CloseHandle'](handle)

    def argv(self, pid):
        handle = self._open(pid)
        if not handle:
            return None
        try:
            size = 4096
            for _ in range(3):
                buffer = ctypes.create_string_buffer(size)
                needed = UINT()
                status = self.fn['NtQueryInformationProcess'](handle, PROCESS_COMMAND_LINE_INFORMATION, buffer,
                                                               size, ctypes.byref(needed))
                if status == 0:
                    text = UNICODE_STRING.from_buffer(buffer)
                    if not text.Buffer or not text.Length:
                        return None
                    return split_command_line(ctypes.string_at(text.Buffer, text.Length).decode('utf-16-le', 'replace'))
                if needed.value <= size:
                    return None
                size = needed.value
            return None
        finally:
            self.fn['CloseHandle'](handle)

    def console_size(self, pid):
        """(cols, rows) of the console `pid` is attached to, read by a short-lived
        child: attaching would detach this process from its own console."""
        if not self.probe_argv:
            return None
        try:
            result = self.run(self.probe_argv + [str(pid)], capture_output=True, text=True, timeout=2,
                              stdin=subprocess.DEVNULL, creationflags=DETACHED_PROCESS)
            data = json.loads(result.stdout) if result.returncode == 0 else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        if not isinstance(data, dict):
            return None
        cols, rows = data.get('cols'), data.get('rows')
        if isinstance(cols, int) and isinstance(rows, int) and cols > 0 and rows > 0:
            return cols, rows
        return None

def probe_console(pid, kernel32=None):
    """Runs in the probe child: attach to `pid`'s console and read its window
    size. Writes nothing to the console. Returns (cols, rows) or None."""
    fn = bind({'kernel32': kernel32 if kernel32 is not None else ctypes.WinDLL('kernel32', use_last_error=True)},
              only={'kernel32'})
    fn['FreeConsole']()
    if not fn['AttachConsole'](pid):
        return None
    try:
        handle = fn['CreateFileW']('CONOUT$', GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                   None, OPEN_EXISTING, 0, None)
        if not handle or handle == INVALID_HANDLE:
            return None
        try:
            info = CONSOLE_SCREEN_BUFFER_INFO()
            if not fn['GetConsoleScreenBufferInfo'](handle, ctypes.byref(info)):
                return None
            window = info.srWindow
            return window.Right - window.Left + 1, window.Bottom - window.Top + 1
        finally:
            fn['CloseHandle'](handle)
    finally:
        fn['FreeConsole']()
