"""Install, update, migration and uninstall in a scratch HOME with a stub
toolchain on PATH. launchctl, security, herdr, xcode-select and xcrun are stubs
that log their arguments and keep their state in the test's temp folder, so
no real LaunchAgent, keychain or herdr session is touched. xcrun swiftc
"builds" a shell-script sensor that answers --check with a valid hello."""
import fcntl
import json
import os
from pathlib import Path
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import installer
import legacy
from spring_load.lock import held, try_lock
from spring_load.paths import support_dir

HELLO = '{"type":"hello","t":0,"contract":1,"sensor":"macos","version":"0.3.0","coords":"macos-points","capabilities":["drag","click","window","clients"]}'

STUBS = {
    'launchctl': '''#!/bin/sh
echo "launchctl $*" >> "$FAKE_LOG"
loaded="$FAKE_DIR/loaded"
case "$1" in
  print) [ -f "$loaded" ] && exit 0; exit 113 ;;
  bootout) rm -f "$loaded"; exit 0 ;;
  bootstrap)
    [ -n "${FAKE_BOOTSTRAP_FAIL:-}" ] && { echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; }
    [ -f "$loaded" ] && { echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; }
    cp "$3" "$FAKE_DIR/loaded"; exit 0 ;;
esac
exit 0
''',
    'security': '''#!/bin/sh
echo "security $*" >> "$FAKE_LOG"
case "$1" in
  delete-keychain) rm -f "$2" ;;
  list-keychains) [ "${4:-}" = -s ] || echo '    "/login.keychain-db"' ;;
esac
exit 0
''',
    'herdr': '''#!/bin/sh
echo "herdr $*" >> "$FAKE_LOG"
exit 0
''',
    'xcode-select': '''#!/bin/sh
exit "${FAKE_NO_TOOLS:-0}"
''',
    'xcrun': '''#!/bin/sh
echo "xcrun $*" >> "$FAKE_LOG"
if [ "$1" = --find ]; then echo /fake/swiftc; exit 0; fi
shift
while [ "$#" -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done
if [ -n "${FAKE_BAD_SENSOR:-}" ]; then printf '#!/bin/sh\\necho nonsense\\n' > "$out"; else
printf '#!/bin/sh\\n[ "$1" = --check ] && echo %s && exit 0\\nexit 0\\n' "'$FAKE_HELLO'" > "$out"; fi
chmod +x "$out"
''',
}

class FakeServer:
    """Answers herdr API requests on a unix socket and records the methods."""
    def __init__(self, path):
        self.methods = []
        path.parent.mkdir(parents=True, exist_ok=True)
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(path))
        self.sock.listen(4)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = b''
                while b'\n' not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                request = json.loads(data.split(b'\n')[0])
                self.methods.append(request['method'])
                conn.sendall((json.dumps({'id': request['id'], 'result': {'type': 'config_reload', 'status': 'applied',
                                                                         'diagnostics': []}}) + '\n').encode())

    def close(self):
        self.sock.close()

@unittest.skipUnless(sys.platform == 'darwin', 'macOS install flow')
class InstallTest(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='sl-i.', dir='/private/tmp'))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.home = self.temp/'h'
        self.home.mkdir()
        self.bin = self.temp/'bin'
        self.bin.mkdir()
        self.fake = self.temp/'fake'
        self.fake.mkdir()
        for name, body in STUBS.items():
            (self.bin/name).write_text(body)
            (self.bin/name).chmod(0o755)
        self.log = self.temp/'calls.log'
        self.config = self.temp/'c/herdr/config.toml'
        self.config.parent.mkdir(parents=True)
        self.original = 'onboarding = false\n[keys]\nprefix = "ctrl+b"\n'
        self.config.write_text(self.original)
        self.env = {'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.temp/'c'), 'XDG_STATE_HOME': str(self.temp/'s'),
                    'PATH': f'{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin:{Path(sys.executable).parent}',
                    'TMPDIR': str(self.temp), 'FAKE_LOG': str(self.log), 'FAKE_DIR': str(self.fake),
                    'FAKE_HELLO': HELLO, 'LANG': 'en_US.UTF-8'}
        for name in STUBS:   # every system tool setup touches must be a stub
            self.assertEqual(shutil.which(name, path=self.env['PATH']), str(self.bin/name))
        self.support = self.home/'Library/Application Support/herdr-spring-load'
        self.plist = self.home/'Library/LaunchAgents/dev.qcklex.herdr-spring-load.plist'
        self.sensor = self.support/'sensors/herdr-spring-load-sensor-macos'

    def install(self, root=ROOT, args=(), **env):
        return subprocess.run(['sh', str(root/'scripts/build.sh'), *args], cwd=root, env={**self.env, **env},
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)

    def uninstall(self, *args, script=None, **env):
        return subprocess.run([sys.executable, str(script or ROOT/'uninstall.py'), *args], cwd=self.temp,
                              env={**self.env, **env}, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)

    def calls(self):
        return self.log.read_text() if self.log.exists() else ''

    def loaded(self):
        return (self.fake/'loaded').exists()

    def assert_untouched(self):
        self.assertEqual(self.config.read_text(), self.original)
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.sensor.exists())
        self.assertFalse((self.support/'lib').exists())
        self.assertFalse((self.support/'state.json').exists())
        self.assertFalse(self.loaded())

    def test_fresh_install(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('No permission is needed', result.stdout)
        self.assertEqual(self.config.read_text(), self.original, 'setup does not edit herdr config')
        self.assertTrue(os.access(self.sensor, os.X_OK))
        lib = self.support/'lib'
        for name in ('spring_load/engine.py', 'spring_load/render.py', 'installer.py', 'legacy.py', 'uninstall.py'):
            self.assertTrue((lib/name).exists(), name)
        self.assertEqual(list(lib.rglob('__pycache__')), [])
        plist = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(plist['ProgramArguments'][1:], ['-m', 'spring_load', 'run', '--tab-dwell-ms', '600', '--pane-dwell-ms', '250'])
        self.assertEqual(plist['EnvironmentVariables']['PYTHONPATH'], str(lib))
        self.assertEqual(plist['LimitLoadToSessionType'], 'Aqua')
        self.assertTrue(self.loaded())
        state = json.loads((self.support/'state.json').read_text())
        self.assertEqual(state['version'], '0.3.0')
        self.assertEqual(state['uninstall'], str(lib/'uninstall.py'))
        calls = self.calls()
        for absent in ('security', 'codesign', 'tccutil', 'osascript', 'herdr plugin'):
            self.assertNotIn(absent, calls)
        # The installed core loads from the support folder alone.
        check = subprocess.run([state['python'], '-c', 'import spring_load.__main__, uninstall'], cwd=self.temp,
                               env={'PYTHONPATH': str(lib), 'PATH': '/usr/bin:/bin'}, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_reinstall_replaces_and_resets_the_registration_marker(self):
        self.assertEqual(self.install().returncode, 0)
        (self.support/'registered-seen').write_text('{}')
        (self.support/'lib/stale.py').write_text('')
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.support/'registered-seen').exists())
        self.assertFalse((self.support/'lib/stale.py').exists())
        self.assertEqual([p.name for p in self.support.iterdir() if '.old-' in p.name or '.new-' in p.name], [])
        self.assertIn('launchctl bootout', self.calls())
        self.assertTrue(self.loaded())

    def test_bad_sensor_changes_nothing(self):
        result = self.install(FAKE_BAD_SENSOR='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('the built sensor', result.stderr)
        self.assert_untouched()

    def test_failed_service_start_rolls_back(self):
        result = self.install(FAKE_BOOTSTRAP_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assert_untouched()

    def test_failed_update_restores_previous_install(self):
        self.assertEqual(self.install().returncode, 0)
        marker = self.support/'lib/spring_load/__init__.py'
        marker.write_text(marker.read_text() + '# previous\n')
        plist = self.plist.read_bytes()
        (self.fake/'loaded').unlink()   # bootstrap fails while the old job "lingers"
        (self.fake/'loaded').write_text('lingering')
        result = self.install(FAKE_BOOTSTRAP_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('# previous', marker.read_text(), 'previous core restored')
        self.assertEqual(self.plist.read_bytes(), plist)

    def test_missing_command_line_tools_changes_nothing(self):
        result = self.install(FAKE_NO_TOOLS='2')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('xcode-select --install', result.stderr)
        self.assert_untouched()
        self.assertNotIn('xcrun swiftc', self.calls())

    def test_old_python_changes_nothing(self):
        old = self.temp/'oldpy'
        old.mkdir()
        for name in ('python3', 'python3.11'):
            (old/name).write_text('#!/bin/sh\nexit 1\n')
            (old/name).chmod(0o755)
        result = subprocess.run(['/bin/sh', str(ROOT/'scripts/build.sh')], cwd=ROOT, stdin=subprocess.DEVNULL,
                                env={**self.env, 'PATH': f'{old}:/usr/bin:/bin', 'SPRING_LOAD_PYTHON_FALLBACKS': f'{old}/python3'},
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Python 3.11 or newer', result.stderr)
        self.assert_untouched()

    def test_concurrent_setup_refused(self):
        self.support.mkdir(parents=True)
        with open(self.support/'setup.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('another herdr-spring-load setup is running', result.stderr)
        self.assertFalse(self.plist.exists())

    def test_check_changes_nothing(self):
        result = self.install(args=('--check',))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Check only', result.stdout)
        self.assert_untouched()

    def test_setup_pane_waits_before_closing(self):
        result = subprocess.run(['sh', str(ROOT/'scripts/build.sh'), '--pause'], cwd=ROOT,
                                env={**self.env, 'FAKE_NO_TOOLS': '2'}, input='\n', capture_output=True, text=True, timeout=60)
        self.assertIn('setup failed: Xcode Command Line Tools are missing', result.stdout)
        self.assertIn('Press Enter to close.', result.stdout)

    def test_status_action_reads_the_status_file(self):
        result = subprocess.run(['sh', str(ROOT/'scripts/build.sh'), '--status'], cwd=ROOT, env=self.env,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('no status yet', result.stdout)
        self.support.mkdir(parents=True)
        (self.support/'status.json').write_text(json.dumps({'state': 'idle', 'sensor': 'macos 0.3.0', 'updated': 0,
                                                            'reason': 'herdr 0.10.0 is not a version this replica was checked against'}))
        result = subprocess.run(['sh', str(ROOT/'scripts/build.sh'), '--status'], cwd=ROOT, env=self.env,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        self.assertIn('herdr-spring-load: not running', result.stdout)   # nothing holds helper.lock
        self.assertIn('a different herdr tab once, or a different Space twice', result.stdout)   # review N12
        self.assertIn('0.10.0', result.stdout)
        with open(self.support/'helper.lock', 'a') as held:   # what a running helper holds
            self.assertTrue(try_lock(held))
            result = subprocess.run(['sh', str(ROOT/'scripts/build.sh'), '--status'], cwd=ROOT, env=self.env,
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        self.assertIn('herdr-spring-load: idle', result.stdout)

    def test_uninstall_removes_everything(self):
        self.assertEqual(self.install().returncode, 0)
        result = self.uninstall()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.support.exists())
        self.assert_untouched()

    def test_self_removal_works_after_herdr_deleted_the_checkout(self):
        checkout = self.temp/'managed/herdr-spring-load'
        shutil.copytree(ROOT, checkout, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        self.assertEqual(self.install(root=checkout).returncode, 0)
        state = json.loads((self.support/'state.json').read_text())
        shutil.rmtree(checkout)
        self.assertTrue(state['uninstall'].startswith(str(self.support)))
        result = subprocess.run([state['python'], state['uninstall'], '--self'], cwd=self.temp, stdin=subprocess.DEVNULL,
                                env={**self.env, 'PATH': f'{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin',
                                     'PYTHONPATH': str(self.support/'lib')},
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('herdr plugin uninstall', result.stdout)
        self.assert_untouched()

    def test_migrates_a_0_2_0_install(self):
        # 0.2.0 left its managed block, focus aliases, a signing keychain and a helper binary.
        with_block = (self.original + '[keys]\n' if False else self.original.replace('prefix = "ctrl+b"\n',
                      'prefix = "ctrl+b"\nfocus_pane_left = ["prefix+h", "ctrl+alt+shift+f8"]\n'
                      'focus_pane_right = ["prefix+l", "ctrl+alt+shift+f9"]\nfocus_pane_up = ["prefix+k", "ctrl+alt+shift+f10"]\n'
                      'focus_pane_down = ["prefix+j", "ctrl+alt+shift+f11"]\n') + '\n' + legacy.BINDING)
        self.config.write_text(with_block)
        self.support.mkdir(parents=True)
        (self.support/'herdr-spring-load').write_text('old helper')
        (self.support/'signing').mkdir()
        (self.support/'signing/signing.keychain-db').write_text('')
        (self.support/'signing/keychain-password').write_text('x')
        (self.support/'state.json').write_text(json.dumps({
            'focus_fields_added': ['focus_pane_down', 'focus_pane_left', 'focus_pane_right', 'focus_pane_up'],
            'keys_table_added': False, 'focus_originals': {}, 'config': str(self.config)}))
        server = FakeServer(self.config.parent/'herdr.sock')
        self.addCleanup(server.close)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config.read_text(), self.original)
        self.assertEqual(tomllib.loads(self.config.read_text()), tomllib.loads(self.original))
        self.assertEqual(server.methods, ['server.reload_config'])
        self.assertFalse((self.support/'herdr-spring-load').exists())
        self.assertFalse((self.support/'signing').exists())
        self.assertIn('security delete-keychain', self.calls())
        self.assertIn("Removed 0.2.0's private keybindings", result.stdout)

    def test_herdr_plugin_install_contract(self):
        # herdr runs [[build]] in a temporary checkout with stdin null and no plugin
        # env, requires a byte-identical manifest, then renames the checkout.
        checkout = self.temp/'tmp-install/checkout'
        shutil.copytree(ROOT, checkout, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        manifest = (checkout/'herdr-plugin.toml').read_bytes()
        builds = [b for b in tomllib.loads(manifest.decode())['build'] if 'macos' in b.get('platforms', ['macos'])]
        self.assertEqual([b['command'] for b in builds], [['sh', 'scripts/build.sh']])
        env = {k: v for k, v in self.env.items() if not k.startswith('HERDR')}
        for build in builds:
            result = subprocess.run(build['command'], cwd=checkout, env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((checkout/'herdr-plugin.toml').read_bytes(), manifest)
        self.assertEqual(list(checkout.rglob('__pycache__')), [], 'the checkout stays clean')
        final = self.temp/'managed/herdr-spring-load'
        final.parent.mkdir()
        checkout.rename(final)
        for path in [self.plist, self.config, *self.support.rglob('*')]:
            if path.is_file():
                self.assertNotIn(str(checkout).encode(), path.read_bytes(), path)
        self.assertTrue(os.access(self.sensor, os.X_OK))

class ZeroPermissionTest(unittest.TestCase):
    def test_manifest_has_no_probe_actions_and_shipped_code_no_permissioned_api(self):
        manifest = tomllib.loads((ROOT/'herdr-plugin.toml').read_text())
        self.assertEqual(manifest['version'], '0.3.0')
        self.assertEqual(sorted(a['id'] for a in manifest['actions']),
                         ['remove-helper', 'remove-helper-windows', 'status', 'status-windows'])
        shipped = [*ROOT.glob('*.py'), *ROOT.glob('spring_load/*.py'), *ROOT.glob('sensors/*/*'), *ROOT.glob('scripts/*'),
                   *ROOT.glob('helper/*.sh')]
        for path in shipped:
            if ' 2' in path.name or not path.is_file():
                continue
            text = path.read_text(errors='replace')
            if path.name == 'legacy.py':
                text = text.replace("'security'", '')   # deletes 0.2.0's keychain, never asks for anything
            for word in ('osascript', 'AXUIElement', 'AXIsProcessTrusted', 'tccutil', 'CGEventPost', 'codesign',
                         'NSAppleScript', 'Ghostty', 'ghostty'):
                self.assertFalse(word in text, f'{word} in {path.relative_to(ROOT)}')

class ManifestPlatformTest(unittest.TestCase):
    """herdr 0.9.3 runs every [[build]], action and pane whose `platforms`
    include the host, and rejects duplicate ids even across platforms."""
    def setUp(self):
        self.manifest = tomllib.loads((ROOT/'herdr-plugin.toml').read_text())

    def entries(self, section, platform):
        return [e for e in self.manifest[section] if platform in e.get('platforms', self.manifest['platforms'])]

    def test_each_platform_gets_one_build_status_removal_and_setup(self):
        self.assertEqual(self.manifest['platforms'], ['macos', 'linux', 'windows'])
        for platform in ('macos', 'linux', 'windows'):
            with self.subTest(platform):
                self.assertEqual(len(self.entries('build', platform)), 1)
                actions = self.entries('actions', platform)
                self.assertEqual(sorted(a['id'].removesuffix('-windows') for a in actions), ['remove-helper', 'status'])
                self.assertEqual(len(self.entries('panes', platform)), 1)
        for section in ('actions', 'panes'):
            ids = [e['id'] for e in self.manifest[section]]
            self.assertEqual(len(ids), len(set(ids)), f'duplicate {section} ids')

    def test_commands_per_platform(self):
        for platform in ('macos', 'linux'):
            for section in ('build', 'actions', 'panes'):
                for entry in self.entries(section, platform):
                    self.assertEqual(entry['command'][0], 'sh', entry)
                    self.assertTrue((ROOT/entry['command'][1]).is_file(), entry)
        # Linux needs python3 only: build.sh picks the sensor by OS, and the Linux one is a zipapp.
        self.assertNotIn('swift', (ROOT/'sensors/linux/build.py').read_text())
        for section in ('build', 'actions', 'panes'):
            for entry in self.entries(section, 'windows'):
                self.assertEqual(entry['command'][:2], ['python', '-B'], entry)   # no sh on Windows
                script = entry['command'][2]
                self.assertTrue(script == '-m' or (ROOT/script).is_file(), entry)
                if section != 'build':
                    self.assertIn('experimental', entry['title'], entry)

    def test_windows_status_runs_from_the_plugin_root(self):
        temp = Path(tempfile.mkdtemp(prefix='sl-w.', dir='/private/tmp'))
        self.addCleanup(shutil.rmtree, temp, True)
        status = next(a for a in self.manifest['actions'] if a['id'] == 'status-windows')
        result = subprocess.run([sys.executable, *status['command'][1:]], cwd=ROOT, stdin=subprocess.DEVNULL,
                                env={'HOME': str(temp), 'PATH': '/usr/bin:/bin'}, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('no status yet', result.stdout)

SYSTEMCTL = """#!/bin/sh
echo "systemctl $*" >> "$FAKE_LOG"
[ "$1" = --user ] || exit 2
shift
units="$XDG_CONFIG_HOME/systemd/user"
case "$1" in
  show-environment) [ -n "${FAKE_NO_USER_BUS:-}" ] && exit 1; echo "HOME=$HOME"; exit 0 ;;
  is-active) if [ -f "$FAKE_DIR/active" ]; then echo active; exit 0; fi; echo inactive; exit 3 ;;
  enable) [ -f "$units/$2" ] || exit 1; mkdir -p "$units/default.target.wants"
          ln -sf "$units/$2" "$units/default.target.wants/$2"; exit 0 ;;
  restart) [ -n "${FAKE_START_FAIL:-}" ] && { echo "Job failed" >&2; exit 1; }; cp "$units/$2" "$FAKE_DIR/active"; exit 0 ;;
  stop) rm -f "$FAKE_DIR/active"; exit 0 ;;
esac
exit 0
"""

class LinuxInstallTest(unittest.TestCase):
    """setup.py and uninstall.py as they run on Linux (sys.platform 'linux'),
    with the real Linux sensor build (a python3 zipapp) and a stub systemctl
    that keeps its state in the test's temp folder."""
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='sl-l.', dir='/private/tmp'))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.home = self.temp/'h'
        self.home.mkdir()
        self.bin = self.temp/'bin'
        self.bin.mkdir()
        self.fake = self.temp/'fake'
        self.fake.mkdir()
        for name, body in {'systemctl': SYSTEMCTL, 'herdr': STUBS['herdr']}.items():
            (self.bin/name).write_text(body)
            (self.bin/name).chmod(0o755)
        self.log = self.temp/'calls.log'
        self.env = {'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.temp/'c'), 'XDG_DATA_HOME': str(self.temp/'d'),
                    'XDG_STATE_HOME': str(self.temp/'s'), 'DISPLAY': ':7', 'TMPDIR': str(self.temp),
                    'PATH': f'{self.bin}:{Path(sys.executable).parent}:/usr/bin:/bin', 'FAKE_LOG': str(self.log),
                    'FAKE_DIR': str(self.fake), 'LANG': 'C.UTF-8'}
        self.support = self.temp/'d/herdr-spring-load'
        self.unit = self.temp/'c/systemd/user/dev.qcklex.herdr-spring-load.service'
        self.link = self.temp/'c/systemd/user/default.target.wants/dev.qcklex.herdr-spring-load.service'

    def run_as_linux(self, script, *args, **env):
        code = ('import runpy, sys; sys.platform = "linux"; sys.argv = [sys.argv[1], *sys.argv[2:]]; '
                'sys.path.insert(0, str(__import__("pathlib").Path(sys.argv[0]).resolve().parent)); '
                'runpy.run_path(sys.argv[0], run_name="__main__")')
        return subprocess.run([sys.executable, '-c', code, str(script), *args], cwd=self.temp, env={**self.env, **env},
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)

    def calls(self):
        return self.log.read_text() if self.log.exists() else ''

    def test_install_status_and_uninstall(self):
        result = self.run_as_linux(ROOT/'setup.py')
        self.assertEqual(result.returncode, 0, result.stderr)
        sensor = self.support/'sensors/herdr-spring-load-sensor-linux'
        self.assertTrue(os.access(sensor, os.X_OK))
        self.assertTrue(sensor.read_bytes().startswith(b'#!/usr/bin/env python3'))
        unit = self.unit.read_text()
        lib = self.support/'lib'
        self.assertIn(f'ExecStart="{sys.executable}" "-m" "spring_load" "run" "--tab-dwell-ms" "600"', unit)
        self.assertIn(f'Environment="PYTHONPATH={lib}"', unit)
        self.assertIn(f'Environment="PATH={Path(sys.executable).parent}:', unit)
        self.assertIn('WantedBy=default.target graphical-session.target', unit)
        self.assertNotIn('DISPLAY', unit, 'the display comes from the user manager, per login')
        self.assertTrue(self.link.is_symlink())
        self.assertTrue((self.fake/'active').exists())
        calls = self.calls()
        for expected in ('systemctl --user daemon-reload', 'systemctl --user enable dev.qcklex.herdr-spring-load.service',
                         'systemctl --user restart dev.qcklex.herdr-spring-load.service'):
            self.assertIn(expected, calls)
        self.assertNotIn('swift', calls)
        self.assertTrue((lib/'run_helper.py').exists())
        # The installed sensor speaks the contract.
        check = subprocess.run([str(sensor), '--check'], env={'PATH': self.env['PATH']}, capture_output=True, text=True,
                               timeout=30)
        self.assertEqual(json.loads(check.stdout)['type'], 'hello')

        result = self.run_as_linux(ROOT/'uninstall.py')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.link.exists() or self.link.is_symlink(), 'enable links go with the unit')
        self.assertFalse((self.fake/'active').exists())
        self.assertFalse(self.support.exists())

    def test_failed_start_rolls_back(self):
        result = self.run_as_linux(ROOT/'setup.py', FAKE_START_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('starting the helper service failed', result.stderr)
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.link.exists() or self.link.is_symlink())
        self.assertFalse((self.support/'lib').exists())
        self.assertFalse((self.support/'sensors').exists())

    def test_no_user_systemd_refuses_cleanly(self):
        result = self.run_as_linux(ROOT/'setup.py', FAKE_NO_USER_BUS='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('no systemd user session', result.stderr)
        self.assertFalse(self.unit.exists())
        self.assertFalse((self.support/'lib').exists())

    def test_display_is_passed_on_when_the_user_manager_has_none(self):
        self.assertEqual(self.run_as_linux(ROOT/'setup.py').returncode, 0)
        self.assertIn('systemctl --user import-environment DISPLAY', self.calls())

class FakeWinreg:
    HKEY_CURRENT_USER = 'HKCU'
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def CreateKey(self, root, path):
        class Key:
            def __enter__(self):
                return (root, path)
            def __exit__(self, *exc):
                return False
        return Key()

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[(key, name)] = value

    def DeleteValue(self, key, name):
        if (key, name) not in self.values:
            raise FileNotFoundError(name)
        del self.values[(key, name)]

class WindowsServiceTest(unittest.TestCase):
    """The experimental Windows Run key service, with a fake winreg."""
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='sl-r.', dir='/private/tmp'))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.winreg = FakeWinreg()
        sys.modules['winreg'] = self.winreg
        self.addCleanup(sys.modules.pop, 'winreg', None)
        self.service = installer.RunKey(self.temp/'support')

    def test_definition_uses_pythonw_and_the_lib_launcher(self):
        python = self.temp/'Python311/python.exe'
        python.parent.mkdir()
        python.write_text('')
        (python.parent/'pythonw.exe').write_text('')
        lib = self.temp/'support/lib'
        data = json.loads(self.service.definition(str(python), lib, ['--tab-dwell-ms', '600'], {'XDG_CONFIG_HOME': 'C:/c'}))
        self.assertEqual(data['argv'], [str(python.parent/'pythonw.exe'), str(lib/'run_helper.py'), 'run', '--tab-dwell-ms', '600'])
        self.assertEqual(data['env'], {'XDG_CONFIG_HOME': 'C:/c'})

    def test_run_key_follows_the_definition_file(self):
        self.service.plist.parent.mkdir(parents=True)
        self.service.plist.write_text(json.dumps({'argv': ['C:/Py/pythonw.exe', 'C:/x y/lib/run_helper.py', 'run'], 'env': {}}))
        self.service.reconcile()
        self.assertEqual(self.winreg.values[(('HKCU', installer.RunKey.KEY), 'herdr-spring-load')],
                         'C:/Py/pythonw.exe "C:/x y/lib/run_helper.py" run')
        self.service.plist.unlink()
        self.service.reconcile()
        self.assertEqual(self.winreg.values, {})
        self.service.reconcile()   # removing an absent value is fine

    def test_loaded_is_the_helper_lock(self):
        support = self.temp/'support'
        support.mkdir()
        self.assertFalse(self.service.loaded())
        holder = subprocess.Popen([sys.executable, '-c', 'import sys, time; sys.path.insert(0, sys.argv[1]); '
                                   'from spring_load.lock import try_lock; h = open(sys.argv[2], "a"); '
                                   'assert try_lock(h); print("held", flush=True); time.sleep(30)',
                                   str(ROOT), str(support/'helper.lock')], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        self.assertTrue(self.service.loaded())
        holder.kill()
        holder.wait()
        holder.stdout.close()
        self.assertFalse(self.service.loaded())

class RunHelperTest(unittest.TestCase):
    def test_runs_the_core_from_lib_with_the_recorded_env(self):
        temp = Path(tempfile.mkdtemp(prefix='sl-h.', dir='/private/tmp'))
        self.addCleanup(shutil.rmtree, temp, True)
        lib = temp/'support/lib'
        shutil.copytree(ROOT/'spring_load', lib/'spring_load', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(ROOT/'run_helper.py', lib/'run_helper.py')
        recorded = temp/'recorded'
        (temp/'support/service.json').write_text(json.dumps({'argv': [], 'env': {'HOME': str(recorded)}}))
        # The status file exists only under the recorded HOME's support folder.
        status = support_dir({'HOME': str(recorded), 'XDG_DATA_HOME': str(recorded/'data')})/'status.json'
        status.parent.mkdir(parents=True)
        status.write_text(json.dumps({'state': 'idle', 'sensor': 'test 1', 'updated': 0}))
        result = subprocess.run([sys.executable, str(lib/'run_helper.py'), 'status'], cwd=temp, stdin=subprocess.DEVNULL,
                                env={'HOME': str(temp), 'XDG_DATA_HOME': str(recorded/'data'), 'PATH': '/usr/bin:/bin'},
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('sensor test 1', result.stdout, 'service.json env applied before the core started')
        self.assertEqual(list(lib.rglob('__pycache__')), [])

class InstanceLockTest(unittest.TestCase):
    def test_second_helper_exits_and_status_sees_the_first(self):
        temp = Path(tempfile.mkdtemp(prefix='sl-k.', dir='/private/tmp'))
        self.addCleanup(shutil.rmtree, temp, True)
        lock = temp/'helper.lock'
        self.assertFalse(held(lock))
        with open(lock, 'a') as first:
            self.assertTrue(try_lock(first))
            code = ('import sys; sys.path.insert(0, sys.argv[1]); from spring_load.lock import try_lock; '
                    'print(try_lock(open(sys.argv[2], "a")))')
            other = subprocess.run([sys.executable, '-c', code, str(ROOT), str(lock)], capture_output=True, text=True)
            self.assertEqual(other.stdout.strip(), 'False')
            self.assertTrue(held(lock))
        self.assertFalse(held(lock))

if __name__ == '__main__':
    unittest.main()
