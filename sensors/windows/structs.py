"""Win32 structures for the Windows sensor.

Fields use fixed-width ctypes types, never ctypes.wintypes: on LP64 systems
(macOS, Linux) wintypes.LONG is 8 bytes and WCHAR is 4, which would give the
wrong layout where the tests run. With fixed widths the sizes match 64-bit
Windows on every platform."""
import ctypes

LONG = ctypes.c_int32
DWORD = ctypes.c_uint32
SHORT = ctypes.c_int16
WORD = ctypes.c_uint16
HANDLE = ctypes.c_void_p
ULONG_PTR = ctypes.c_size_t
MAX_PATH = 260

class POINT(ctypes.Structure):
    _fields_ = [('x', LONG), ('y', LONG)]

class RECT(ctypes.Structure):
    _fields_ = [('left', LONG), ('top', LONG), ('right', LONG), ('bottom', LONG)]

class CURSORINFO(ctypes.Structure):
    _fields_ = [('cbSize', DWORD), ('flags', DWORD), ('hCursor', HANDLE), ('ptScreenPos', POINT)]

class GUITHREADINFO(ctypes.Structure):
    _fields_ = [('cbSize', DWORD), ('flags', DWORD), ('hwndActive', HANDLE), ('hwndFocus', HANDLE),
                ('hwndCapture', HANDLE), ('hwndMenuOwner', HANDLE), ('hwndMoveSize', HANDLE),
                ('hwndCaret', HANDLE), ('rcCaret', RECT)]

class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [('dwSize', DWORD), ('cntUsage', DWORD), ('th32ProcessID', DWORD),
                ('th32DefaultHeapID', ULONG_PTR), ('th32ModuleID', DWORD), ('cntThreads', DWORD),
                ('th32ParentProcessID', DWORD), ('pcPriClassBase', LONG), ('dwFlags', DWORD),
                ('szExeFile', WORD * MAX_PATH)]

class FILETIME(ctypes.Structure):
    _fields_ = [('dwLowDateTime', DWORD), ('dwHighDateTime', DWORD)]

class COORD(ctypes.Structure):
    _fields_ = [('X', SHORT), ('Y', SHORT)]

class SMALL_RECT(ctypes.Structure):
    _fields_ = [('Left', SHORT), ('Top', SHORT), ('Right', SHORT), ('Bottom', SHORT)]

class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [('dwSize', COORD), ('dwCursorPosition', COORD), ('wAttributes', WORD),
                ('srWindow', SMALL_RECT), ('dwMaximumWindowSize', COORD)]

class UNICODE_STRING(ctypes.Structure):
    _fields_ = [('Length', WORD), ('MaximumLength', WORD), ('Buffer', HANDLE)]

def wide_text(array):
    """Decode a NUL-terminated WORD array (UTF-16LE code units)."""
    units = []
    for unit in array:
        if unit == 0:
            break
        units.append(unit)
    return b''.join(u.to_bytes(2, 'little') for u in units).decode('utf-16-le', 'replace')

def filetime_value(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime
