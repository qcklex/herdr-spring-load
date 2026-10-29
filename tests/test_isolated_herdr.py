"""Opt-in integration: only a throwaway HOME/XDG and its own PTY/server.
HERDR_TEST_ISOLATED=1 python3 -m unittest discover -s tests -p test_isolated_herdr.py -v
Never reads or connects to the user's normal Herdr session.
"""
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from probe import request
from setup import prepare_config
ROOT=Path(__file__).resolve().parents[1]

@unittest.skipUnless(os.environ.get('HERDR_TEST_ISOLATED') == '1' and shutil.which('herdr'), 'opt-in isolated Herdr integration')
class IsolatedHerdrTest(unittest.TestCase):
    def test_probe_space_tab_pane_and_native_drop(self):
        with tempfile.TemporaryDirectory(prefix='sl-int.', dir='/private/tmp') as temp:
            base=Path(temp)
            config=base/'c/herdr/config.toml'
            config.parent.mkdir(parents=True)
            config.write_text(prepare_config('onboarding = false\n[update]\nversion_check=false\nmanifest_check=false\n[ui]\nsidebar_width=26\nprompt_new_tab_name=false\n'))
            env={'HOME':temp,'XDG_CONFIG_HOME':str(base/'c'),'XDG_STATE_HOME':str(base/'s'),
                 'HERDR_CONFIG_PATH':str(config),'PATH':os.environ['PATH'],'TERM':'xterm-256color',
                 'SHELL':'/bin/zsh','LANG':'en_US.UTF-8','TMPDIR':temp}
            binary=shutil.which('herdr')
            subprocess.run([binary,'plugin','link',str(ROOT)], env=env, cwd=temp, check=True, capture_output=True, timeout=10)
            pid,fd=pty.fork()
            if pid==0:
                fcntl.ioctl(0,termios.TIOCSWINSZ,struct.pack('HHHH',40,120,1200,800))
                os.chdir(temp)
                os.execve(binary,[binary],env)
            stop=threading.Event()
            output=bytearray()
            def drain():
                while not stop.is_set():
                    try:
                        if select.select([fd],[],[],.1)[0]:
                            chunk=os.read(fd,65536)
                            if not chunk: break
                            output.extend(chunk)
                    except OSError: break
            reader=threading.Thread(target=drain,daemon=True);reader.start()
            path=str(base/'c/herdr/herdr.sock')
            def wait(check, message):
                until=time.time()+8
                while time.time()<until:
                    try:
                        value=check()
                        if value:return value
                    except (OSError,ValueError,KeyError):pass
                    time.sleep(.05)
                invoked = subprocess.run([binary,'plugin','action','invoke','herdr-spring-load.probe-1'],env=env,capture_output=True,text=True)
                debug = subprocess.run([binary,'plugin','log','list','--plugin','herdr-spring-load'],env=env,capture_output=True,text=True)
                logs = [p.read_text(errors='replace')[-2000:] for p in base.rglob('*.log')]
                self.fail(message+'\nInvoke diagnostic: '+invoked.stdout+invoked.stderr+'\nPlugin logs: '+debug.stdout+debug.stderr+'\nServer logs: '+repr(logs)+'\nPTY text: '+re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '',bytes(output).decode('utf8','replace'))[-12000:])
            def screen_text():
                return re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',bytes(output).decode('utf8','replace'))
            def api(method,**params):return request(path,method,params)
            def mouse(x,y):
                os.write(fd,f'\x1b[<0;{x+1};{y+1}M\x1b[<0;{x+1};{y+1}m'.encode())
                time.sleep(.15)
            probes=base/'Library/Application Support/herdr-spring-load/probes'
            probes.mkdir(parents=True)
            def probe_client(token):
                time.sleep(.3)
                (probes/'request-1.json').write_text(json.dumps({'token':token,'time':time.time()}))
                os.write(fd,b'\x1b[1;8P')
                def result():
                    data=json.loads((probes/'reply-1.json').read_text())
                    return data if data.get('token')==token else None
                return wait(result,'private key did not invoke client-specific probe')
            try:
                wait(lambda:api('workspace.list').get('workspaces'),'isolated server did not start')
                # Custom bindings exist on the client only after its first snapshot; the sidebar header proves it rendered one.
                wait(lambda:'spaces' in screen_text(),'client did not render its first snapshot')
                initial=probe_client('initial')
                self.assertEqual(initial['socket'],path)
                original=initial['workspace']
                api('workspace.rename',workspace_id=original,label='project')
                created=api('workspace.create',cwd=temp,label='assets')
                workspace=created['workspace']['workspace_id']
                second=api('tab.create',workspace_id=workspace,label='files',cwd=temp)
                tab=second['tab']['tab_id']
                wait(lambda:'assets' in screen_text(),'client did not render the new space')
                # Default sidebar: ' spaces' header row 0, blank row 1, workspaces from row 2 in number order.
                mouse(5,3)
                after_space=probe_client('space')
                self.assertEqual(after_space['workspace'],workspace,'native workspace hover click must change this client')
                # First tab label '1': 8 cells, separator 1, then files label centered.
                mouse(26+8+1+2,0)
                after_tab=probe_client('tab')
                self.assertEqual(after_tab['layout']['layout']['tab_id'],tab,'native tab hover click must change this client')
                focused=after_tab['pane']
                split=api('pane.split',pane_id=focused,direction='right')
                destination=split['pane']['pane_id']
                os.write(fd,b'\x1b[20;8~') # private native F9 right binding
                time.sleep(.2)
                after_pane=probe_client('pane')
                self.assertEqual(after_pane['pane'],destination,'native direction binding must focus the destination client')
                image=base/'photo.png';image.write_bytes(b'fake image for path-paste test')
                # Native terminal file drops paste a shell-escaped path; do not send Enter.
                os.write(fd,b'\x1b[200~'+str(image).encode()+b'\x1b[201~')
                landed=wait(lambda: str(image) in api('pane.read',pane_id=destination,source='recent_unwrapped',lines=10)['read']['text'],'drop path did not reach destination pane')
                self.assertTrue(landed)
                print('Isolated Herdr 0.9.3: probe -> space -> tab -> pane -> dropped path PASS')
            finally:
                # The only socket touched here is under this temporary directory.
                try:api('server.stop')
                except (OSError,ValueError):pass
                stop.set()
                try:os.kill(pid,signal.SIGTERM)
                except ProcessLookupError:pass
                reader.join(1)
                os.close(fd)
                try:os.waitpid(pid,0)
                except ChildProcessError:pass
