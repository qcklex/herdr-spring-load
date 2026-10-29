"""Start a herdr client deterministically in one kind of console (CI only):

- ClassicClient: conhost.exe started explicitly with herdr.exe as its
  command, so the console is a classic console window (ConsoleWindowClass).
  A plain CREATE_NEW_CONSOLE may be handed to the default terminal instead
  (run 40904180127 got a ConPTY PseudoConsoleWindow that way).
- ConPtyClient: herdr.exe attached to a pseudo console this process creates
  (CreatePseudoConsole), the way Windows Terminal and other ConPTY terminals
  host it. Its output is drained and discarded."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import signal
import subprocess
import threading

from common import wait

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016

class COORD(ctypes.Structure):
    _fields_ = [('X', ctypes.c_short), ('Y', ctypes.c_short)]

class STARTUPINFOW(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('lpReserved', wintypes.LPWSTR), ('lpDesktop', wintypes.LPWSTR),
                ('lpTitle', wintypes.LPWSTR), ('dwX', wintypes.DWORD), ('dwY', wintypes.DWORD),
                ('dwXSize', wintypes.DWORD), ('dwYSize', wintypes.DWORD), ('dwXCountChars', wintypes.DWORD),
                ('dwYCountChars', wintypes.DWORD), ('dwFillAttribute', wintypes.DWORD), ('dwFlags', wintypes.DWORD),
                ('wShowWindow', wintypes.WORD), ('cbReserved2', wintypes.WORD), ('lpReserved2', ctypes.c_void_p),
                ('hStdInput', wintypes.HANDLE), ('hStdOutput', wintypes.HANDLE), ('hStdError', wintypes.HANDLE)]

class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [('StartupInfo', STARTUPINFOW), ('lpAttributeList', ctypes.c_void_p)]

class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [('hProcess', wintypes.HANDLE), ('hThread', wintypes.HANDLE), ('dwProcessId', wintypes.DWORD),
                ('dwThreadId', wintypes.DWORD)]

kernel32.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p,
                                wintypes.DWORD]
kernel32.CreatePseudoConsole.argtypes = [COORD, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD,
                                         ctypes.POINTER(ctypes.c_void_p)]
kernel32.CreatePseudoConsole.restype = ctypes.c_long
kernel32.ClosePseudoConsole.argtypes = [ctypes.c_void_p]
kernel32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                                       ctypes.POINTER(ctypes.c_size_t)]
kernel32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                                               ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
kernel32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
                                    wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOEXW),
                                    ctypes.POINTER(PROCESS_INFORMATION)]
kernel32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                              ctypes.c_void_p]
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

def _check(ok, what):
    if not ok:
        raise OSError(ctypes.get_last_error(), f'{what} failed')

def _terminate(pid):
    try:
        os.kill(pid, signal.SIGTERM)   # TerminateProcess on Windows
    except OSError:
        pass

class ClassicClient:
    def __init__(self, herdr, env, cwd, processes):
        conhost = Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32'/'conhost.exe'
        self.host = subprocess.Popen([str(conhost), herdr], env=env, cwd=cwd, creationflags=subprocess.DETACHED_PROCESS)
        self.pid = wait(lambda: next((p.pid for p in processes() or []
                                      if p.ppid == self.host.pid and p.exe.lower() == 'herdr.exe'), None), 15)

    def close(self):
        # herdr and its conhost only: the server herdr spawned must outlive this client.
        if self.pid:
            _terminate(self.pid)
        try:
            self.host.wait(5)   # conhost leaves once its client is gone
        except subprocess.TimeoutExpired:
            self.host.kill()
            self.host.wait(5)

class ConPtyClient:
    def __init__(self, herdr, env, cwd, cols=120, rows=30):
        self.pid = None
        self.process = self.hpc = None
        handles = [wintypes.HANDLE() for _ in range(4)]
        in_read, in_write, out_read, out_write = handles
        _check(kernel32.CreatePipe(ctypes.byref(in_read), ctypes.byref(in_write), None, 0), 'CreatePipe')
        _check(kernel32.CreatePipe(ctypes.byref(out_read), ctypes.byref(out_write), None, 0), 'CreatePipe')
        hpc = ctypes.c_void_p()
        result = kernel32.CreatePseudoConsole(COORD(cols, rows), in_read, out_write, 0, ctypes.byref(hpc))
        if result != 0:
            raise OSError(result, f'CreatePseudoConsole failed: 0x{result & 0xFFFFFFFF:08x}')
        self.hpc = hpc
        kernel32.CloseHandle(in_read)      # the pseudo console holds its own copies
        kernel32.CloseHandle(out_write)
        self.input, self.output = in_write, out_read
        size = ctypes.c_size_t()
        kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        self.attributes = ctypes.create_string_buffer(size.value)
        _check(kernel32.InitializeProcThreadAttributeList(self.attributes, 1, 0, ctypes.byref(size)),
               'InitializeProcThreadAttributeList')
        _check(kernel32.UpdateProcThreadAttribute(self.attributes, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, hpc,
                                                  ctypes.sizeof(ctypes.c_void_p), None, None), 'UpdateProcThreadAttribute')
        info = STARTUPINFOEXW()
        info.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        # Null standard handles: the child must not inherit this process's
        # redirected ones instead of the pseudo console's.
        info.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        info.lpAttributeList = ctypes.cast(self.attributes, ctypes.c_void_p)
        block = ctypes.create_unicode_buffer(''.join(f'{k}={v}\0' for k, v in sorted(env.items())) + '\0')
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([herdr]))
        process = PROCESS_INFORMATION()
        _check(kernel32.CreateProcessW(None, command, None, None, False,
                                       EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT, block, cwd,
                                       ctypes.byref(info), ctypes.byref(process)), 'CreateProcessW')
        kernel32.CloseHandle(process.hThread)
        self.process, self.pid = process.hProcess, process.dwProcessId
        threading.Thread(target=self.drain, daemon=True).start()

    def drain(self):
        buffer = ctypes.create_string_buffer(65536)
        read = wintypes.DWORD()
        while kernel32.ReadFile(self.output, buffer, len(buffer), ctypes.byref(read), None) and read.value:
            pass

    def close(self):
        if self.process:
            kernel32.TerminateProcess(self.process, 1)
            kernel32.CloseHandle(self.process)
            self.process = None
        if self.hpc:
            kernel32.ClosePseudoConsole(self.hpc)   # ends the drain: the output pipe breaks
            self.hpc = None
            kernel32.CloseHandle(self.input)
            kernel32.DeleteProcThreadAttributeList(self.attributes)
