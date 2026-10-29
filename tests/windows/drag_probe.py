"""Best-effort drag simulation (CI; the job does not depend on it):

    python tests/windows/drag_probe.py

Starts the Windows sensor, opens tests/windows/drag_source.ps1 (a WinForms
window whose MouseDown starts an OLE file drag) and drives the mouse with
SendInput: press in the window, move out in steps, release. It records what
the sensor printed and what SendInput returned, and always exits 0: whether
a runner's desktop delivers synthetic input to an OLE drag loop is what this
finds out. The plugin itself never synthesizes input."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from common import ROOT, Check, require_windows, scratch, wait

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0001, 0x0002, 0x0004
MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_ABSOLUTE = 0x4000, 0x8000
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('mouseData', wintypes.DWORD),
                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]

class INPUT(ctypes.Structure):
    _fields_ = [('type', wintypes.DWORD), ('mi', MOUSEINPUT)]

def main():
    require_windows()
    check = Check('drag-probe')
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.FindWindowW.restype = wintypes.HWND
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    check.note('DPI awareness for the probe', bool(user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))))
    vx, vy = user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw, vh = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    check.note('virtual screen', (vx, vy, vw, vh))
    sent = []

    def send(flags, x=None, y=None):
        event = INPUT(type=INPUT_MOUSE)
        event.mi.dwFlags = flags
        if x is not None:
            event.mi.dwFlags |= MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
            event.mi.dx = round((x - vx) * 65535 / max(1, vw - 1))
            event.mi.dy = round((y - vy) * 65535 / max(1, vh - 1))
        count = user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
        sent.append({'flags': flags, 'x': x, 'y': y, 'sent': count, 'error': ctypes.get_last_error() if not count else 0})

    base = scratch('drag')
    dropped = base/'dropped.txt'
    dropped.write_text('spring-load drag probe\n')
    out_path = ROOT/'tests'/'windows'/'out'
    out_path.mkdir(parents=True, exist_ok=True)
    events_path = out_path/'drag-probe.ndjson'
    title = f'spring-load drag source {os.getpid()}'
    source = sensor = None
    report = {}
    try:
        with open(events_path, 'wb') as events:
            sensor = subprocess.Popen([sys.executable, str(ROOT/'sensors/windows/run_sensor.py'), '--verbose'],
                                      stdin=subprocess.PIPE, stdout=events, stderr=subprocess.PIPE)
            source = subprocess.Popen(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-STA', '-File',
                                       str(ROOT/'tests/windows/drag_source.ps1'), '-File', str(dropped), '-Title', title],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            hwnd = wait(lambda: user32.FindWindowW(None, title), 20)
            check.note('drag source window', hwnd)
            if hwnd:
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                report['source_rect'] = (rect.left, rect.top, rect.right, rect.bottom)
                x, y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
                time.sleep(1.0)
                send(0, x, y)
                time.sleep(0.3)
                send(MOUSEEVENTF_LEFTDOWN)
                time.sleep(0.15)
                for step in range(1, 31):
                    send(0, x + step * 15, y + step * 8)
                    time.sleep(0.04)
                time.sleep(0.8)   # hold still: the sensor reports moves while held
                send(MOUSEEVENTF_LEFTUP)
                time.sleep(1.0)
            sensor.stdin.close()
            try:
                sensor.wait(10)
            except subprocess.TimeoutExpired:
                sensor.kill()
            report['sensor_stderr'] = sensor.stderr.read().decode('utf-8', 'replace')[-4000:]
    finally:
        if source:
            source.kill()
            out, err = source.communicate()
            report['source_stdout'], report['source_stderr'] = out[-2000:], err[-2000:]
        if sensor and sensor.poll() is None:
            sensor.kill()
    lines = [json.loads(l) for l in events_path.read_text(encoding='utf-8').splitlines() if l.strip()]
    contract = subprocess.run([sys.executable, '-m', 'spring_load.contract'], stdin=open(events_path, 'rb'), cwd=ROOT,
                              capture_output=True, text=True)
    starts = [e for e in lines if e['type'] == 'drag_start']
    report.update({'sendinput': sent, 'events': lines, 'contract': contract.stdout.strip(),
                   'event_types': [e['type'] for e in lines],
                   'drag_start': starts[0] if starts else None,
                   'drag_end': next((e for e in lines if e['type'] == 'drag_end'), None)})
    verdict = ('drag detected' if starts else
               'no drag_start' + (' (SendInput was refused)' if any(not s['sent'] for s in sent) else ''))
    check.note('verdict', verdict)
    check.note('event types', report['event_types'])
    check.note('contract', report['contract'])
    check.done(report)
    print(f'drag probe: {verdict}')

if __name__ == '__main__':
    try:
        main()
    except Exception as error:   # best effort: report, never fail the job
        print(f'drag probe could not run: {error!r}')
