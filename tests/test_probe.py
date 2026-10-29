import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import probe

class ProbeTest(unittest.TestCase):
    def test_nonce_and_receiving_client_scope(self):
        with tempfile.TemporaryDirectory(prefix='sl.') as temp:
            base = Path(temp)
            path = str(base/'s')
            server = socket.socket(socket.AF_UNIX)
            server.bind(path)
            server.listen(4)
            server.settimeout(3)
            seen = []
            def reply():
                try:
                    for _ in range(3):
                        conn,_ = server.accept()
                        with conn:
                            data = bytearray()
                            while b'\n' not in data:
                                data.extend(conn.recv(4096))
                            req = json.loads(data)
                            seen.append(req)
                            conn.sendall(b'{"id":"spring-load","result":{"layout":{},"tabs":[],"workspaces":[]}}\n')
                finally:
                    server.close()
            thread = threading.Thread(target=reply)
            thread.start()
            (base/'request-1.json').write_text(json.dumps({'token':'nonce-123','time':time.time()}))
            env = {'HERDR_SOCKET_PATH':path, 'HERDR_PLUGIN_CONTEXT_JSON':json.dumps({'focused_pane_id':'w7:p9','workspace_id':'w7'})}
            with patch.object(probe, 'BASE', base), patch.dict(os.environ,env):
                probe.main()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            response = json.loads((base/'reply-1.json').read_text())
            self.assertEqual(response['token'],'nonce-123')
            self.assertEqual(response['socket'],path)
            self.assertEqual(seen[0]['params'],{'pane_id':'w7:p9'})
            self.assertEqual(seen[1]['params'],{'workspace_id':'w7'})
            self.assertEqual((base/'reply-1.json').stat().st_mode & 0o777,0o600)

    def test_expired_or_missing_challenge_does_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            with patch.object(probe,'BASE',base):
                probe.main()
                (base/'request-1.json').write_text(json.dumps({'token':'old','time':time.time()-10}))
                probe.main()
            self.assertFalse((base/'reply-1.json').exists())
