"""herdr API transport: the Windows named pipe (H4), exercised with a socket
pair standing in for the pipe, since this suite also runs on macOS and Linux."""
import json
from pathlib import Path, PureWindowsPath
import socket
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from spring_load import herdr_api

class PipeTransportTest(unittest.TestCase):
    def setUp(self):
        self.opened = []
        self.saved = (herdr_api.USE_PIPES, herdr_api.pipe_opener)
        self.addCleanup(self.restore)

    def restore(self):
        herdr_api.USE_PIPES, herdr_api.pipe_opener = self.saved

    def test_pipe_name_matches_interprocess_generic_namespaced(self):
        path = PureWindowsPath(r'C:\Users\a\AppData\Roaming\herdr\herdr.sock')
        self.assertEqual(herdr_api.pipe_name(path), r'\\.\pipe\C:\Users\a\AppData\Roaming\herdr\herdr.sock')

    def serve(self, reply, busy=0):
        """An opener whose 'pipe' is one end of a socket pair; the other end answers once."""
        state = {'busy': busy}
        def opener(name, mode, buffering):
            self.opened.append((name, mode, buffering))
            if state['busy']:
                state['busy'] -= 1
                error = OSError('all pipe instances are busy')
                error.winerror = herdr_api.ERROR_PIPE_BUSY
                raise error
            ours, theirs = socket.socketpair()
            def answer():
                data = b''
                while b'\n' not in data:
                    data += theirs.recv(65536)
                self.request = json.loads(data.split(b'\n')[0])
                if reply is not None:
                    theirs.sendall(json.dumps(reply).encode() + b'\n')
                    theirs.close()
            threading.Thread(target=answer, daemon=True).start()
            self.addCleanup(theirs.close)
            self.addCleanup(ours.close)
            return socket.SocketIO(ours, 'rwb')
        herdr_api.USE_PIPES, herdr_api.pipe_opener = True, opener

    def test_focus_over_the_pipe(self):
        self.serve({'id': 'herdr-spring-load', 'result': {'type': 'ok'}}, busy=2)
        result = herdr_api.focus(PureWindowsPath(r'C:\h\herdr.sock'), 'tab', 't1', timeout=2)
        self.assertEqual(result, {'type': 'ok'})
        self.assertEqual(self.request['method'], 'tab.focus')
        self.assertEqual(self.request['params'], {'tab_id': 't1'})
        self.assertEqual(self.opened[-1], (r'\\.\pipe\C:\h\herdr.sock', 'r+b', 0))
        self.assertEqual(len(self.opened), 3, 'busy pipe instances are retried')

    def test_silent_server_times_out(self):
        self.serve(None)
        with self.assertRaises(OSError):
            herdr_api.request(PureWindowsPath(r'C:\h\herdr.sock'), 'session.snapshot', timeout=0.2)

if __name__ == '__main__':
    unittest.main()
