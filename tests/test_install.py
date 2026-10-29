"""Install and uninstall flows in a scratch HOME/XDG with a fake toolchain on PATH.
launchctl, security, codesign, herdr, xcode-select, xcrun and swiftc are stubs that
log their arguments and keep their state under the test's temp directory, so no
real LaunchAgent, keychain, search list or herdr session is touched.
"""
import fcntl
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import setup

STUBS = {
    # A loaded job is a file; bootout can lag (FAKE_BOOTOUT_LAG prints) and the
    # first FAKE_BOOTSTRAP_EIO bootstraps fail with EIO, as launchd does.
    'launchctl': '''#!/bin/sh
echo "launchctl $*" >> "$FAKE_LOG"
loaded="$FAKE_DIR/loaded"; lag="$FAKE_DIR/lag"; eio="$FAKE_DIR/eio"
case "$1" in
  print)
    if [ -f "$lag" ]; then n=$(cat "$lag"); rm -f "$lag"; [ "$n" -gt 1 ] && echo $((n-1)) > "$lag"; exit 0; fi
    [ -f "$loaded" ] && exit 0; exit 113 ;;
  bootout)
    [ -f "$loaded" ] && [ -n "${FAKE_BOOTOUT_LAG:-}" ] && echo "$FAKE_BOOTOUT_LAG" > "$lag"
    rm -f "$loaded"; exit 0 ;;
  bootstrap)
    if [ -n "${FAKE_BOOTSTRAP_EIO:-}" ]; then
      c=$(cat "$eio" 2>/dev/null || echo 0); c=$((c+1)); echo $c > "$eio"
      if [ "$c" -le "$FAKE_BOOTSTRAP_EIO" ]; then echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; fi
    fi
    [ -f "$loaded" ] && { echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; }
    : > "$loaded"; exit 0 ;;
esac
exit 0
''',
    'security': '''#!/bin/sh
echo "security $*" >> "$FAKE_LOG"
case "$1" in
  create-keychain) for last; do :; done; : > "$last" ;;
  delete-keychain) rm -f "$2"; grep -v -F -x "$2" "$FAKE_SEARCH" > "$FAKE_SEARCH.new"; mv "$FAKE_SEARCH.new" "$FAKE_SEARCH" ;;
  unlock-keychain) read -r pw && [ -n "$pw" ] && echo "security unlock-keychain received password on stdin" >> "$FAKE_LOG" ;;
  import) exit "${FAKE_IMPORT_RC:-0}" ;;
  list-keychains)
    if [ "${4:-}" = -s ]; then shift 4; printf '%s\\n' "$@" > "$FAKE_SEARCH"; else
      while read -r k; do echo "    \\"$k\\""; done < "$FAKE_SEARCH"; fi ;;
  find-identity) echo '  1) ABCDEF0123456789ABCDEF0123456789ABCDEF01 "herdr-spring-load local signing" (CSSMERR_TP_NOT_TRUSTED)' ;;
esac
exit 0
''',
    'codesign': '''#!/bin/sh
echo "codesign $*" >> "$FAKE_LOG"
if [ "$1" = -f ]; then
  [ -n "${FAKE_CODESIGN_SLEEP:-}" ] && sleep "$FAKE_CODESIGN_SLEEP"
  exit "${FAKE_CODESIGN_RC:-0}"
fi
exit 0
''',
    # herdr config check: FAKE_REJECT_BINDINGS makes herdr disable the probe
    # bindings; a "# FAKE_EXISTING" line is a diagnostic the user already has.
    'herdr': '''#!/bin/sh
echo "herdr $*" >> "$FAKE_LOG"
if [ "$1 $2" = "config check" ]; then
  out=""
  grep -q "FAKE_EXISTING" "$HERDR_CONFIG_PATH" && out="unknown config key fake_existing; ignoring key"
  [ -n "${FAKE_REJECT_BINDINGS:-}" ] && grep -q "herdr-spring-load.probe-1" "$HERDR_CONFIG_PATH" && out="$out
ctrl+alt+shift+f1: kept keys.reserved, disabled keys.command"
  if [ -n "$out" ]; then echo "config: issues found"; echo "$out" | sed '/^$/d'; exit 1; fi
  echo "config: ok"; exit 0
fi
exit 0
''',
    'xcode-select': '''#!/bin/sh
exit "${FAKE_NO_TOOLS:-0}"
''',
    'xcrun': '''#!/bin/sh
echo "$FAKE_BIN/swiftc"
''',
    'swiftc': '''#!/bin/sh
echo "swiftc $*" >> "$FAKE_LOG"
[ -n "${FAKE_EDIT_CONFIG:-}" ] && echo "# edited during build" >> "$FAKE_EDIT_CONFIG"
while [ "$#" -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done
printf '#!/bin/sh\\nexit 0\\n' > "$out"; chmod +x "$out"
''',
}


class FakeServer:
    """Answers herdr API requests on a unix socket and records the methods."""
    def __init__(self, path, error=None, result=None):
        self.methods = []
        self.error = error
        self.result = result if result is not None else {'type': 'config_reload', 'status': 'applied', 'diagnostics': []}
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
                reply = {'id': request['id'], 'error': self.error} if self.error else {'id': request['id'], 'result': self.result}
                conn.sendall((json.dumps(reply)+'\n').encode())

    def close(self):
        self.sock.close()


def real_search_list():
    # Read-only: the user's actual keychain search list.
    return subprocess.run(['/usr/bin/security', 'list-keychains', '-d', 'user'], capture_output=True, text=True).stdout


@unittest.skipUnless(sys.platform == 'darwin', 'macOS install flow')
class InstallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.real_search = real_search_list()

    def tearDown(self):
        self.assertEqual(real_search_list(), self.real_search, 'real keychain search list changed')

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
            path = self.bin/name
            path.write_text(body)
            path.chmod(0o755)
        self.log = self.temp/'calls.log'
        self.search = self.temp/'search.txt'
        self.search.write_text('/login.keychain-db\n')
        self.config = self.temp/'c/herdr/config.toml'
        self.config.parent.mkdir(parents=True)
        self.original = 'onboarding = false\n[keys]\nprefix = "ctrl+b"\n[[keys.command]]\nkey = "ctrl+x"\ntype = "shell"\ncommand = "a"\n'
        self.config.write_text(self.original)
        self.env = {'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.temp/'c'), 'XDG_STATE_HOME': str(self.temp/'s'),
                    'PATH': f'{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin:{Path(sys.executable).parent}',
                    'TMPDIR': str(self.temp), 'FAKE_LOG': str(self.log), 'FAKE_SEARCH': str(self.search),
                    'FAKE_BIN': str(self.bin), 'FAKE_DIR': str(self.fake), 'LANG': 'en_US.UTF-8'}
        # Every system tool setup touches must resolve to a stub, never the real one.
        for name in STUBS:
            self.assertEqual(shutil.which(name, path=self.env['PATH']), str(self.bin/name))
        self.support = self.home/'Library/Application Support/herdr-spring-load'
        self.plist = self.home/'Library/LaunchAgents/dev.qcklex.herdr-spring-load.plist'
        self.keychain = self.support/'signing/signing.keychain-db'

    def install(self, root=ROOT, args=(), **env):
        return subprocess.run(['sh', str(root/'scripts/build.sh'), *args], cwd=root, env={**self.env, **env},
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)

    def uninstall(self, *args, script=None, **env):
        script = script or ROOT/'uninstall.py'
        return subprocess.run([sys.executable, str(script), *args], cwd=self.temp, env={**self.env, **env},
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)

    def calls(self):
        return self.log.read_text() if self.log.exists() else ''

    def loaded(self):
        return (self.fake/'loaded').exists()

    def assert_untouched(self):
        self.assertEqual(self.config.read_text(), self.original)
        self.assertFalse(self.plist.exists())
        self.assertFalse((self.support/'herdr-spring-load').exists())
        self.assertFalse((self.support/'signing').exists())
        self.assertFalse((self.support/'state.json').exists())
        self.assertEqual(list(self.config.parent.glob('*.bak')), [])
        self.assertEqual(self.search.read_text(), '/login.keychain-db\n')
        self.assertFalse(self.loaded())

    def test_installs_without_any_running_server(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('no running herdr server', result.stdout)
        parsed = tomllib.loads(self.config.read_text())
        self.assertIs(parsed['onboarding'], False)
        self.assertEqual(len(parsed['keys']['command']), 8)
        self.assertTrue(os.access(self.support/'herdr-spring-load', os.X_OK))
        launch = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(launch['ProgramArguments'][0], str(self.support/'herdr-spring-load'))
        calls = self.calls()
        self.assertIn('codesign -f -s ABCDEF0123456789ABCDEF0123456789ABCDEF01', calls)
        self.assertIn('-i dev.qcklex.herdr-spring-load', calls)
        self.assertIn('certificate leaf = H"abcdef0123456789abcdef0123456789abcdef01"', calls)
        self.assertTrue(self.loaded())
        # The signing keychain is on the search list only while codesign runs.
        self.assertEqual(self.search.read_text(), '/login.keychain-db\n')
        # Repeatable: a second run reuses the identity and leaves the config alone.
        before = self.config.read_text()
        self.log.unlink()
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.config.read_text(), before)
        self.assertNotIn('create-keychain', self.calls())
        self.assertIn('launchctl bootout', self.calls())
        self.assertTrue(self.loaded())

    def test_reloads_named_sessions_and_skips_stale_sockets(self):
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(str(self.temp/'c/herdr/herdr.sock'))
        stale.close() # default session: socket file left behind, no server
        server = FakeServer(self.temp/'c/herdr/sessions/work/herdr.sock')
        self.addCleanup(server.close)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(server.methods, ['server.reload_config'])
        self.assertIn('sessions/work/herdr.sock', result.stdout)

    def test_rejected_config_rolls_everything_back(self):
        server = FakeServer(self.temp/'c/herdr/herdr.sock', error={'code': 'invalid_config', 'message': 'bad'})
        self.addCleanup(server.close)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('rejected the new config', result.stderr)
        self.assert_untouched()
        self.assertIn('launchctl bootout', self.calls())

    def test_rollback_reloads_sessions_that_already_took_the_config(self):
        # M1: the default session applies the new config, the named one rejects it.
        first = FakeServer(self.temp/'c/herdr/herdr.sock')
        second = FakeServer(self.temp/'c/herdr/sessions/work/herdr.sock', error={'code': 'invalid_config', 'message': 'bad'})
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assert_untouched()
        self.assertEqual(first.methods, ['server.reload_config', 'server.reload_config'],
                         'the session that took the new config is reloaded from the restored file')

    def test_disabled_binding_in_reload_report_rolls_back(self):
        # M5: herdr applies the reload but reports that it disabled a binding.
        server = FakeServer(self.temp/'c/herdr/herdr.sock', result={
            'type': 'config_reload', 'status': 'partial',
            'diagnostics': ['ctrl+alt+shift+f1: kept keys.reserved, disabled keys.command']})
        self.addCleanup(server.close)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('did not apply the new config (partial)', result.stderr)
        self.assert_untouched()

    def test_herdr_config_check_refuses_before_any_change(self):
        # M5, offline: herdr's own parser rejects the candidate; nothing is built.
        result = self.install(FAKE_REJECT_BINDINGS='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('herdr would not accept the new keybindings', result.stderr)
        self.assertNotIn('swiftc', self.calls())
        self.assert_untouched()
        self.assertFalse(self.support.exists())

    def test_existing_config_diagnostics_do_not_block_install(self):
        self.original += '# FAKE_EXISTING\n'
        self.config.write_text(self.original)
        server = FakeServer(self.temp/'c/herdr/herdr.sock', result={
            'type': 'config_reload', 'status': 'partial', 'diagnostics': ['unknown config key fake_existing; ignoring key']})
        self.addCleanup(server.close)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_update_waits_for_bootout_and_retries_bootstrap(self):
        # M2: launchd keeps the old job for a moment, then refuses one bootstrap with EIO.
        self.assertEqual(self.install().returncode, 0)
        (self.fake/'eio').unlink(missing_ok=True)
        result = self.install(FAKE_BOOTOUT_LAG='3', FAKE_BOOTSTRAP_EIO='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.loaded())
        self.assertGreaterEqual(self.calls().count('launchctl print'), 4)

    def test_failed_launch_agent_rolls_everything_back(self):
        result = self.install(FAKE_BOOTSTRAP_EIO='9')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('starting the helper LaunchAgent failed', result.stderr)
        self.assert_untouched()

    def test_failed_update_restores_previous_install(self):
        self.assertEqual(self.install().returncode, 0)
        binary = (self.support/'herdr-spring-load').read_bytes()
        config, plist = self.config.read_text(), self.plist.read_bytes()
        state = (self.support/'state.json').read_bytes()
        self.log.unlink()
        # Every bootstrap attempt of the update fails; the restore bootstrap succeeds.
        result = self.install(args=['--tab-dwell-ms', '900'], FAKE_BOOTSTRAP_EIO='3')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.support/'herdr-spring-load').read_bytes(), binary)
        self.assertEqual(self.config.read_text(), config)
        self.assertEqual(self.plist.read_bytes(), plist)
        self.assertEqual((self.support/'state.json').read_bytes(), state)
        self.assertTrue(self.keychain.exists())
        self.assertTrue(self.loaded(), 'the previous helper runs again')

    def test_search_list_restored_when_signing_fails(self):
        result = self.install(FAKE_CODESIGN_RC='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('signing the helper failed', result.stderr)
        # The signing keychain was added for codesign, then taken out again.
        added = self.calls().split('codesign -f')[0].rsplit('list-keychains -d user -s', 1)[1]
        self.assertIn('signing.keychain-db', added)
        self.assert_untouched()

    def test_search_list_restored_when_install_is_killed(self):
        # M3: SIGTERM while codesign runs still restores the search list and rolls back.
        process = subprocess.Popen(['sh', str(ROOT/'scripts/build.sh')], cwd=ROOT, env={**self.env, 'FAKE_CODESIGN_SLEEP': '5'},
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 30
        while 'codesign -f' not in self.calls():
            self.assertLess(time.time(), deadline)
            time.sleep(0.05)
        self.assertIn('signing.keychain-db', self.search.read_text())
        process.send_signal(signal.SIGTERM)
        _, err = process.communicate(timeout=30)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('interrupted by signal', err)
        self.assert_untouched()

    def test_stale_search_list_entry_is_removed(self):
        # M3: a run killed by SIGKILL left the keychain on the list; the next run clears it.
        self.assertEqual(self.install().returncode, 0)
        self.search.write_text(f'/login.keychain-db\n{self.keychain}\n/other.keychain-db\n')
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.search.read_text(), '/login.keychain-db\n/other.keychain-db\n')

    def test_failed_keychain_creation_deletes_the_keychain(self):
        # M3: rollback uses delete-keychain, which also drops any search-list entry.
        result = self.install(FAKE_IMPORT_RC='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('importing the signing identity failed', result.stderr)
        self.assertIn('security delete-keychain', self.calls())
        self.assert_untouched()

    def test_signing_keychain_locked_and_password_off_argv(self):
        # M7 mitigations: unlock reads the password on stdin; the keychain is locked after signing.
        self.assertEqual(self.install().returncode, 0)
        calls = self.calls()
        self.assertNotIn('unlock-keychain -p', calls)
        self.assertIn('unlock-keychain received password on stdin', calls)
        self.assertGreater(calls.rindex('security lock-keychain'), calls.rindex('codesign -f'))
        self.assertEqual(self.keychain.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.keychain.parent/'keychain-password').stat().st_mode & 0o777, 0o600)

    def test_symlinked_config_keeps_link_and_mode(self):
        # M4: dotfile managers link config.toml into a repository.
        dotfiles = self.temp/'dotfiles'
        dotfiles.mkdir()
        target = dotfiles/'herdr.toml'
        target.write_text(self.original)
        target.chmod(0o644)
        self.config.unlink()
        self.config.symlink_to(target)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.config.is_symlink())
        self.assertIn('herdr-spring-load.probe-1', target.read_text())
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.uninstall().returncode, 0)
        self.assertTrue(self.config.is_symlink())
        self.assertEqual(target.read_text(), self.original)
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)

    def test_uninstall_restores_config_exactly(self):
        # M6: install then uninstall returns the original text, for several shapes.
        for original in ['onboarding = false\n[ui]\nsidebar_width = 26\n', self.original,
                         '[keys]\nfocus_pane_left = "alt+h" \n', '']:
            with self.subTest(original=original):
                self.config.write_text(original)
                self.assertEqual(self.install().returncode, 0)
                self.assertEqual(self.install().returncode, 0) # a re-run must not lose what was recorded
                result = self.uninstall()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.config.read_text(), original)
                for path in [self.plist, self.support/'herdr-spring-load', self.support/'state.json', self.support/'lib']:
                    self.assertFalse(path.exists(), path)
                self.assertFalse(self.loaded())
                self.assertTrue(self.keychain.exists(), 'identity kept so the grant survives a reinstall')

    def test_failed_uninstall_removes_nothing(self):
        # M6: the restored config is rejected by a running session, so nothing is removed.
        self.assertEqual(self.install().returncode, 0)
        installed = self.config.read_text()
        server = FakeServer(self.temp/'c/herdr/herdr.sock', error={'code': 'invalid_config', 'message': 'bad'})
        self.addCleanup(server.close)
        result = self.uninstall()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_text(), installed)
        self.assertTrue(self.plist.exists())
        self.assertTrue((self.support/'herdr-spring-load').exists())
        self.assertTrue((self.support/'state.json').exists())
        self.assertTrue(self.loaded())

    def test_purge_deletes_identity_and_support_files(self):
        self.assertEqual(self.install().returncode, 0)
        result = self.uninstall('--purge')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.support.exists())
        self.assertIn(f'security delete-keychain {self.keychain}', self.calls())
        self.assertEqual(self.config.read_text(), self.original)

    def test_self_removal_works_after_herdr_deleted_the_checkout(self):
        # H1: herdr plugin uninstall deletes the checkout; the helper runs the copy
        # of uninstall.py that setup keeps in Application Support.
        checkout = self.temp/'managed/herdr-spring-load'
        shutil.copytree(ROOT, checkout, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        self.assertEqual(self.install(root=checkout).returncode, 0)
        state = json.loads((self.support/'state.json').read_text())
        shutil.rmtree(checkout)
        self.assertTrue(state['uninstall'].startswith(str(self.support)))
        result = subprocess.run([state['python'], state['uninstall'], '--self'], cwd=self.temp, stdin=subprocess.DEVNULL,
                                env={**self.env, 'PATH': f'{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin'},
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config.read_text(), self.original)
        self.assertFalse(self.plist.exists())
        self.assertFalse((self.support/'herdr-spring-load').exists())
        self.assertFalse(self.loaded())

    def test_install_resets_registered_seen_marker(self):
        # H1: the build runs before herdr registers the plugin, so a marker left by an
        # earlier install must not make the new helper remove itself.
        self.support.mkdir(parents=True)
        (self.support/'registered-seen').touch()
        self.assertEqual(self.install().returncode, 0)
        self.assertFalse((self.support/'registered-seen').exists())

    def test_concurrent_config_edit_aborts(self):
        # L2: another tool edits the config while the helper compiles.
        result = self.install(FAKE_EDIT_CONFIG=str(self.config))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('changed during setup', result.stderr)
        self.assertEqual(self.config.read_text(), self.original+'# edited during build\n')
        self.assertFalse(self.plist.exists())

    def test_backups_are_pruned(self):
        # L3
        for i in range(5):
            (self.config.parent/f'config.toml.spring-load-{i:019d}.bak').write_text(str(i))
        setup.prune_backups(self.config)
        self.assertEqual(sorted(p.read_text() for p in self.config.parent.glob('*.bak')), ['2', '3', '4'])

    def test_setup_pane_waits_before_closing(self):
        # L5: the pane shows the result until Enter.
        result = subprocess.run(['sh', str(ROOT/'scripts/build.sh'), '--pause'], cwd=ROOT, env={**self.env, 'FAKE_NO_TOOLS': '2'},
                                input='\n', capture_output=True, text=True, timeout=60)
        self.assertIn('setup failed: Xcode Command Line Tools are missing', result.stdout)
        self.assertIn('Press Enter to close.', result.stdout)

    def test_rollback_keeps_directories_others_wrote_into(self):
        # L6
        transaction = setup.Transaction()
        made = self.temp/'new/inner'
        transaction.mkdir(made)
        (self.temp/'new/other-tool.txt').write_text('keep')
        transaction.rollback()
        self.assertFalse(made.exists())
        self.assertEqual((self.temp/'new/other-tool.txt').read_text(), 'keep')

    def test_concurrent_setup_refused(self):
        # L7
        self.support.mkdir(parents=True)
        with open(self.support/'setup.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('another herdr-spring-load setup is running', result.stderr)
        self.assertFalse(self.plist.exists())

    def test_warns_about_focus_pane_helper(self):
        # L8
        agents = self.home/'Library/LaunchAgents'
        agents.mkdir(parents=True)
        (agents/'dev.example.herdr-focus-pane.plist').write_text('')
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('herdr-focus-pane.plist is installed', result.stdout)

    def test_missing_command_line_tools_changes_nothing(self):
        result = self.install(FAKE_NO_TOOLS='2')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('xcode-select --install', result.stderr)
        self.assert_untouched()
        self.assertNotIn('swiftc', self.calls())

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

    def test_herdr_plugin_install_contract(self):
        # herdr runs [[build]] in a temporary checkout with stdin null and no plugin
        # env, requires a byte-identical manifest, then renames the checkout.
        checkout = self.temp/'tmp-install/checkout'
        shutil.copytree(ROOT, checkout, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        manifest = (checkout/'herdr-plugin.toml').read_bytes()
        builds = [b for b in tomllib.loads(manifest.decode())['build'] if 'macos' in b.get('platforms', ['macos'])]
        self.assertEqual([b['command'] for b in builds], [['sh', 'scripts/build.sh']])
        env = {k: v for k, v in self.env.items() if not k.startswith('HERDR')}
        self.assertEqual(list((self.temp/'c').rglob('*.sock')), [], 'no herdr server may be running')
        for build in builds:
            result = subprocess.run(build['command'], cwd=checkout, env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((checkout/'herdr-plugin.toml').read_bytes(), manifest)
        final = self.temp/'managed/herdr-spring-load'
        final.parent.mkdir()
        checkout.rename(final)
        for path in [self.plist, self.config, *self.support.rglob('*')]:
            if path.is_file():
                self.assertNotIn(str(checkout).encode(), path.read_bytes(), path)
        self.assertTrue(os.access(self.support/'herdr-spring-load', os.X_OK))
        self.assertEqual(self.search.read_text(), '/login.keychain-db\n')


@unittest.skipUnless(shutil.which('herdr'), 'needs the herdr binary')
class RealHerdrConfigCheckTest(unittest.TestCase):
    def test_reports_disabled_bindings(self):
        # M5: the real herdr names a binding it disables; config check never writes.
        with tempfile.TemporaryDirectory(prefix='sl-cc.', dir='/private/tmp') as temp:
            old = dict(os.environ)
            os.environ.update(HOME=temp, XDG_CONFIG_HOME=temp+'/c', XDG_STATE_HOME=temp+'/s')
            try:
                herdr = shutil.which('herdr')
                self.assertEqual(setup.config_diagnostics(herdr, setup.prepare_config('onboarding = false\n')), set())
                clash = '[keys]\nfocus_pane_left = "ctrl+alt+shift+f8"\nnew_tab = "ctrl+alt+shift+f8"\n'
                self.assertTrue(any('disabled keys.focus_pane_left' in d for d in setup.config_diagnostics(herdr, clash)))
            finally:
                os.environ.clear()
                os.environ.update(old)
            self.assertEqual(list(Path(temp).rglob('*')), [])
