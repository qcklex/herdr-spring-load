#!/usr/bin/env python3
"""Repeatable, transactional setup. Run by herdr plugin install ([[build]]), by the
setup pane, or by helper/install.sh. Any failure restores the previous state."""
import argparse
import copy
import fcntl
import json
try:
    import tomllib
except ImportError:
    raise SystemExit("herdr-spring-load requires Python 3.11 or newer")
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from probe import request

ROOT = Path(__file__).resolve().parent
LABEL = 'dev.qcklex.herdr-spring-load'
KEY = 'ctrl+alt+shift+f1'
ACTION = 'herdr-spring-load.probe-1'
BEGIN = '# BEGIN herdr-spring-load managed binding'
END = '# END herdr-spring-load managed binding'
FOCUS = {'left':('f8','prefix+h'), 'right':('f9','prefix+l'), 'up':('f10','prefix+k'), 'down':('f11','prefix+j')}
BINDING = BEGIN+'\n'+''.join(f'''[[keys.command]]
key = "ctrl+alt+shift+f{i}"
type = "plugin_action"
command = "herdr-spring-load.probe-{i}"
description = "herdr-spring-load client probe {i}"
''' for i in range(1,8))+END+'\n'

def strip_block(text):
    """Remove the managed block and the one blank-line separator setup put before it."""
    return re.sub(r'\n?'+re.escape(BEGIN)+r'.*?'+re.escape(END)+r'\n?', '', text, flags=re.S)

def config_path():
    override = os.environ.get('HERDR_CONFIG_PATH')
    if override:
        return Path(override).expanduser()
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home()/'.config'))) / 'herdr/config.toml'

def canonical(chord):
    # Same modifier aliases as herdr's parse_modifier_token.
    aliases = {'control':'ctrl','option':'alt','meta':'alt','command':'cmd','super':'cmd'}
    parts = [aliases.get(p.strip().lower(),p.strip().lower()) for p in chord.split('+')]
    return frozenset(parts)

def chord_values(value):
    if isinstance(value,str): return [value] if value else []
    if isinstance(value,list) and all(isinstance(v,str) for v in value): return value
    raise ValueError('shortcut must be a string or an array of strings')

def parses_as_value(fragment):
    try:
        tomllib.loads('v = '+fragment)
        return True
    except tomllib.TOMLDecodeError:
        return False

def rewrite_focus(text, parsed, remove=False, drop=(), drop_table=False, originals=None):
    """Add the private focus aliases, or remove them. On removal, fields named in
    `drop` (added by setup) are deleted when only their default is left, and with
    `drop_table` an emptied [keys] table that setup created goes too."""
    keys = parsed.get('keys',{})
    headings = list(re.finditer(r'(?m)^[ \t]*(\[\[?)([^\]\n]+)\]\]?[ \t]*(?:#[^\n]*)?$',text))
    table = next((h for h in headings if h.group(1)=='[' and h.group(2).strip().strip('"\'')=='keys'),None)
    edits=[]
    inserts=[]
    for direction,(key,default) in FOCUS.items():
        field='focus_pane_'+direction
        private='ctrl+alt+shift+'+key
        original=chord_values(keys.get(field,default))
        values=[v for v in original if canonical(v)!=canonical(private)]
        if not remove: values.append(private)
        if remove and values==original: continue
        value=json.dumps(values)
        if field in keys:
            if not table: raise ValueError('use an explicit [keys] table for focus shortcuts before setup')
            finish=next((h.start() for h in headings if h.start()>table.start()),len(text))
            segment=text[table.end():finish]
            match=re.search(r'(?m)^[ \t]*'+field+r'[ \t]*=[ \t]*',segment)
            if not match: raise ValueError('cannot safely edit focus shortcuts; use normal [keys] assignments')
            start=table.end()+match.end()
            # Read complete TOML value, including multiline arrays, before replacing.
            end=start
            while True:
                newline=text.find('\n',end)
                end=len(text) if newline<0 else newline
                try:
                    tomllib.loads('v = '+text[start:end])
                    break
                except tomllib.TOMLDecodeError:
                    if end==len(text): raise ValueError('invalid focus shortcut value')
                    end+=1
            # Keep trailing blanks and an inline comment: replace the value only.
            end=next(k for k in range(start+1,end+1)
                     if re.fullmatch(r'[ \t]*(#.*)?',text[k:end]) and parses_as_value(text[start:k]))
            if remove and field in drop and values==[default]:
                # Setup created this line: take it out, so herdr's default applies again.
                line=text.rfind('\n',0,table.end()+match.start())+1
                edits.append((line,min(len(text),end+1),''))
            else:
                original_value=(originals or {}).get(field)
                if remove and original_value is not None and chord_values(original_value)==values:
                    value=json.dumps(original_value) # the user's own form: a string stays a string
                edits.append((start,end,value))
        elif not remove:
            inserts.append(field+' = '+value+'\n')
    if inserts:
        if table:
            at=next((h.start() for h in headings if h.start()>table.start()),len(text))
            edits.append((at,at,('\n' if at and text[at-1]!='\n' else '')+''.join(inserts)))
        else:
            # A new table must follow the top-level keys, or it would capture them.
            at=headings[0].start() if headings else len(text)
            edits.append((at,at,('\n' if at and text[at-1]!='\n' else '')+'[keys]\n'+''.join(inserts)))
    for start,end,value in sorted(edits,reverse=True): text=text[:start]+value+text[end:]
    if remove and drop_table:
        headings = list(re.finditer(r'(?m)^[ \t]*(\[\[?)([^\]\n]+)\]\]?[ \t]*(?:#[^\n]*)?\n?',text))
        table = next((h for h in headings if h.group(1)=='[' and h.group(2).strip().strip('"\'')=='keys'),None)
        if table:
            finish=next((h.start() for h in headings if h.start()>table.start()),len(text))
            if not text[table.end():finish].strip():
                text=text[:table.start()]+text[finish:]
    return text

def prepare_config(text):
    without = strip_block(text)
    parsed=tomllib.loads(without)
    keys=parsed.get('keys',{})
    reserved={canonical('ctrl+alt+shift+f'+str(i)):('probe-'+str(i) if i<8 else next('focus_pane_'+d for d,v in FOCUS.items() if v[0]=='f'+str(i))) for i in range(1,12)}
    def inspect(value,path):
        if isinstance(value,dict):
            for k,v in value.items(): inspect(v,path+[k])
        elif isinstance(value,list) and not all(isinstance(v,str) for v in value):
            for i,v in enumerate(value): inspect(v,path+[str(i)])
        elif isinstance(value,(str,list)):
            for chord in chord_values(value):
                expected=reserved.get(canonical(chord))
                if expected and path!=[expected]:
                    raise ValueError('private spring-load shortcut conflicts with keys.'+'.'.join(path))
    inspect(keys,[])
    if parsed.get('ui',{}).get('mouse_capture') is False:
        raise ValueError('Herdr ui.mouse_capture must be true for spring-loaded navigation')
    base=rewrite_focus(without,parsed)
    if base and not base.endswith('\n'):
        base+='\n'
    result=base+('\n' if base else '')+BINDING
    # Refuse any edit that changes a setting other than the ones setup manages.
    expected=copy.deepcopy(parsed)
    managed=expected.setdefault('keys',{})
    for direction,(key,default) in FOCUS.items():
        field='focus_pane_'+direction
        values=[v for v in chord_values(managed.get(field,default)) if canonical(v)!=canonical('ctrl+alt+shift+'+key)]
        managed[field]=values+['ctrl+alt+shift+'+key]
    managed['command']=managed.get('command',[])+tomllib.loads(BINDING)['keys']['command']
    if tomllib.loads(result)!=expected:
        raise ValueError('cannot safely edit this config; setup would change unrelated settings')
    return result

def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

def real_path(path):
    """The file a config path really names: dotfile managers often symlink it, and
    replacing the link with a plain file would cut it off from its repository."""
    return path.resolve() if path.is_symlink() else path

def file_mode(path, default=0o600):
    return path.stat().st_mode & 0o777 if path.exists() else default

SIGNING_NAME = 'herdr-spring-load local signing'
SUPPORT = Path.home() / 'Library/Application Support/herdr-spring-load'
SEEN = 'registered-seen'
LIB_FILES = ('setup.py', 'uninstall.py', 'probe.py')

class SetupError(Exception):
    pass

class Interrupted(BaseException):
    pass

def interrupt(signum, frame):
    # Turn SIGTERM/SIGHUP (a killed install) into an exception so rollback and the
    # keychain search-list restore still run.
    raise Interrupted(f'interrupted by signal {signum}')

def tool(name):
    path = shutil.which(name)
    if not path:
        raise SetupError(f'{name} not found')
    return path

def run(argv, input=None, **kwargs):
    # No shell: paths and arguments stay data, including spaces and XML characters.
    if input is None:
        kwargs['stdin'] = subprocess.DEVNULL
    return subprocess.run(argv, input=input, capture_output=True, text=True, **kwargs)

def checked(argv, what, input=None):
    result = run(argv, input=input)
    if result.returncode != 0:
        raise SetupError(f'{what} failed: {(result.stderr or result.stdout).strip()[-500:]}')
    return result.stdout

def find_swiftc():
    # /usr/bin/swiftc is a shim that opens an install dialog when the tools are missing.
    if run([tool('xcode-select'), '-p']).returncode != 0:
        raise SetupError('Xcode Command Line Tools are missing: run xcode-select --install, then install the plugin again')
    found = run([tool('xcrun'), '--find', 'swiftc'])
    path = found.stdout.strip()
    if found.returncode != 0 or not path:
        raise SetupError('swiftc not found: run xcode-select --install, then install the plugin again')
    return path

def config_dir():
    base = os.environ.get('XDG_CONFIG_HOME')
    return Path(base)/'herdr' if base else Path.home()/'.config/herdr'

def session_sockets():
    """API sockets of the default and every named session; servers may or may not be running."""
    sockets = [config_dir()/'herdr.sock']
    sessions = config_dir()/'sessions'
    if sessions.is_dir():
        sockets += sorted(p/'herdr.sock' for p in sessions.iterdir() if p.is_dir())
    return [p for p in sockets if p.exists()]

def herdr_binary():
    return os.environ.get('HERDR_BIN_PATH') or shutil.which('herdr')

def config_diagnostics(herdr, text):
    """herdr's own diagnostics for a config text (herdr config check, offline)."""
    with tempfile.TemporaryDirectory(prefix='spring-load-check.') as temp:
        candidate = Path(temp)/'config.toml'
        candidate.write_text(text)
        env = dict(os.environ, HERDR_CONFIG_PATH=str(candidate))
        result = subprocess.run([herdr, 'config', 'check'], env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30)
    lines = [l.strip() for l in (result.stdout+result.stderr).splitlines()]
    diagnostics = {l for l in lines if l and not l.lower().startswith('config:')}
    if result.returncode not in (0, 1) or (result.returncode == 1 and not diagnostics):
        raise SetupError('herdr config check failed: '+(result.stderr or result.stdout).strip()[-300:])
    return diagnostics

def reload_one(socket_path, accepted):
    """Reload one session; returns False when no server is running there."""
    try:
        reply = request(str(socket_path), 'server.reload_config', {}, timeout=5)
    except (ConnectionRefusedError, FileNotFoundError):
        return False # stale socket: no server
    except ValueError as error:
        raise SetupError(f'herdr at {socket_path} rejected the new config: {error}')
    except OSError:
        return False
    status = reply.get('status', 'applied')
    new = [d for d in reply.get('diagnostics', []) if accepted is not None and d not in accepted]
    if status == 'failed' or new:
        raise SetupError(f'herdr at {socket_path} did not apply the new config ({status}): '+'; '.join(new or ['failed']))
    return True

def reload_servers(transaction=None, accepted=frozenset()):
    """Reload config in every running session. A session without a server picks the
    binding up when it starts. A server that rejects the config, or disables a
    binding, fails setup. On rollback every session reloaded here is reloaded
    again once the old config is back on disk."""
    reloaded = []
    for socket_path in session_sockets():
        if transaction is not None:
            transaction.after.append(lambda p=socket_path: reload_quietly(p))
        if reload_one(socket_path, accepted):
            reloaded.append(socket_path)
    return reloaded

def reload_quietly(socket_path):
    try:
        request(str(socket_path), 'server.reload_config', {}, timeout=5)
    except (OSError, ValueError):
        pass

class Transaction:
    """Every side effect registers its undo; rollback restores the previous state,
    then runs `after` steps (reloads that must see the restored files)."""
    def __init__(self):
        self.undo = []
        self.after = []
    def write(self, path, data, mode=None):
        previous = path.read_bytes() if path.exists() else None
        old_mode = file_mode(path) if previous is not None else None
        created_parent = not path.parent.exists()
        self.undo.append(lambda: atomic_write(path, previous, old_mode) if previous is not None else path.unlink(missing_ok=True))
        atomic_write(path, data, mode if mode is not None else (old_mode or 0o600))
        if created_parent:
            self.undo.append(lambda: path.parent.rmdir() if path.parent.exists() and not any(path.parent.iterdir()) else None)
    def remove(self, path):
        if not path.exists():
            return
        previous, mode = path.read_bytes(), file_mode(path)
        self.undo.append(lambda: atomic_write(path, previous, mode))
        path.unlink()
    def mkdir(self, path):
        missing = []
        for parent in [path, *path.parents]:
            if parent.exists():
                break
            missing.append(parent)
        path.mkdir(parents=True, exist_ok=True)
        # Created directories are removed only when empty: another tool may have
        # written into them meanwhile (for example ~/.config).
        for made in missing:
            self.undo.append(lambda made=made: made.rmdir() if made.exists() and not any(made.iterdir()) else None)
    def rollback(self):
        for step in reversed(self.undo):
            try:
                step()
            except (OSError, SetupError, subprocess.SubprocessError):
                pass
        for step in self.after:
            try:
                step()
            except (OSError, SetupError, subprocess.SubprocessError):
                pass
        self.undo = []
        self.after = []

def keychain_list(security):
    out = checked([security, 'list-keychains', '-d', 'user'], 'reading the keychain search list')
    return [line.strip()[1:-1] if line.strip().startswith('"') and line.strip().endswith('"') else line.strip()
            for line in out.splitlines() if line.strip()]

def same_file(a, b):
    return os.path.realpath(a) == os.path.realpath(b)

def drop_from_search_list(security, keychain):
    """Take our private keychain out of the user search list, keeping every other
    entry exactly as it is now (re-read, never a stale copy)."""
    current = keychain_list(security)
    rest = [k for k in current if not same_file(k, keychain)]
    if rest != current:
        checked([security, 'list-keychains', '-d', 'user', '-s', *rest], 'restoring the keychain search list')

def delete_keychain(security, keychain):
    if keychain.exists():
        run([security, 'delete-keychain', str(keychain)]) # also removes it from the search list
    keychain.unlink(missing_ok=True)
    drop_from_search_list(security, keychain)

def signing_identity(support, transaction):
    """A self-signed code signing identity in a private keychain, created once. Its
    designated requirement (identifier plus certificate leaf) is stable across
    rebuilds, so the Accessibility grant survives reinstalls. Never touches the
    login keychain and needs no password prompt. The keychain stays locked except
    while signing; its password file is what lets the build re-sign unattended."""
    security = tool('security')
    folder = support/'signing'
    keychain = folder/'signing.keychain-db'
    secret = folder/'keychain-password'
    # A killed earlier run may have left the keychain on the search list.
    drop_from_search_list(security, keychain)
    if not keychain.exists() or not secret.exists():
        transaction.mkdir(folder)
        os.chmod(folder, 0o700)
        password = os.urandom(24).hex()
        transaction.write(secret, password.encode(), 0o600)
        with tempfile.TemporaryDirectory(prefix='spring-load-sign.') as temp:
            cnf = Path(temp)/'cert.cnf'
            cnf.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN='+SIGNING_NAME+
                           '\n[ext]\nbasicConstraints=critical,CA:false\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=critical,codeSigning\n')
            key, cert, p12 = Path(temp)/'key.pem', Path(temp)/'cert.pem', Path(temp)/'id.p12'
            openssl = tool('openssl')
            checked([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert),
                     '-days', '7300', '-config', str(cnf)], 'creating the signing certificate')
            transport = os.urandom(8).hex()
            export = [openssl, 'pkcs12', '-export', '-inkey', str(key), '-in', str(cert), '-out', str(p12), '-passout', 'pass:'+transport]
            if run(export+['-legacy']).returncode != 0:
                checked(export, 'packaging the signing identity')
            delete_keychain(security, keychain)
            transaction.undo.append(lambda: delete_keychain(security, keychain))
            checked([security, 'create-keychain', '-p', password, str(keychain)], 'creating the signing keychain')
            drop_from_search_list(security, keychain)
            checked([security, 'set-keychain-settings', str(keychain)], 'configuring the signing keychain')
            checked([security, 'unlock-keychain', str(keychain)], 'unlocking the signing keychain', input=password+'\n')
            checked([security, 'import', str(p12), '-k', str(keychain), '-P', transport, '-T', tool('codesign')], 'importing the signing identity')
            checked([security, 'set-key-partition-list', '-S', 'apple-tool:,apple:', '-s', '-k', password, str(keychain)], 'allowing codesign to use the identity')
            run([security, 'lock-keychain', str(keychain)])
    out = checked([security, 'find-identity', '-p', 'codesigning', str(keychain)], 'finding the signing identity')
    found = re.search(r'\b([0-9A-F]{40})\b "'+re.escape(SIGNING_NAME)+'"', out)
    if not found:
        raise SetupError('signing identity missing from '+str(keychain))
    return keychain, found.group(1)

def sign(binary, keychain, identity):
    security, codesign = tool('security'), tool('codesign')
    password = (keychain.parent/'keychain-password').read_text().strip()
    # codesign resolves identities only through the user's search list (verified:
    # --keychain alone reports "no identity found"); add our keychain for the
    # duration of this call and always take it out again.
    try:
        checked([security, 'unlock-keychain', str(keychain)], 'unlocking the signing keychain', input=password+'\n')
        checked([security, 'list-keychains', '-d', 'user', '-s', *keychain_list(security), str(keychain)], 'adding the signing keychain')
        checked([codesign, '-f', '-s', identity, '--keychain', str(keychain), '-i', LABEL, str(binary)], 'signing the helper')
    finally:
        try:
            drop_from_search_list(security, keychain)
        finally:
            run([security, 'lock-keychain', str(keychain)])
    requirement = f'=identifier "{LABEL}" and certificate leaf = H"{identity.lower()}"'
    checked([codesign, '-v', '-R', requirement, str(binary)], 'verifying the helper signature')

def launchd_stop(launchctl, target, timeout=10):
    """bootout returns before launchd has torn the job down; wait until it is gone."""
    run([launchctl, 'bootout', target])
    deadline = time.monotonic() + timeout
    while run([launchctl, 'print', target]).returncode == 0:
        if time.monotonic() > deadline:
            raise SetupError('the previous helper did not stop')
        time.sleep(0.1)

def launchd_start(launchctl, plist, attempts=3):
    for attempt in range(attempts):
        result = run([launchctl, 'bootstrap', f'gui/{os.getuid()}', str(plist)])
        if result.returncode == 0:
            return
        time.sleep(0.5 * (attempt + 1)) # "5: Input/output error" while the old job finishes
    raise SetupError(f'starting the helper LaunchAgent failed: {(result.stderr or result.stdout).strip()[-300:]}')

def prune_backups(config, keep=3):
    backups = sorted(config.parent.glob(config.name+'.spring-load-*.bak'))
    for old in backups[:-keep]:
        old.unlink(missing_ok=True)

def focus_state(text, previous):
    """What setup adds that uninstall must take away again: focus fields that did
    not exist, and whether the [keys] table itself was new."""
    without = strip_block(text)
    keys = tomllib.loads(without).get('keys', {})
    added = sorted(set(previous.get('focus_fields_added', [])) |
                   {'focus_pane_'+d for d in FOCUS if 'focus_pane_'+d not in keys})
    has_table = re.search(r'(?m)^[ \t]*\[[ \t]*keys[ \t]*\][ \t]*(?:#[^\n]*)?$', without) is not None
    table_added = previous.get('keys_table_added', False) or (not has_table and bool(added))
    originals = dict(previous.get('focus_originals', {}))
    for d, (key, _) in FOCUS.items():
        field = 'focus_pane_'+d
        if field in keys and field not in added and field not in originals:
            values = chord_values(keys[field])
            if canonical('ctrl+alt+shift+'+key) not in map(canonical, values):
                originals[field] = keys[field]
    return {'focus_fields_added': added, 'keys_table_added': table_added, 'focus_originals': originals}

def unprepare_config(text, state):
    """Undo prepare_config using what setup recorded at install."""
    without = strip_block(text)
    result = rewrite_focus(without, tomllib.loads(without), remove=True,
                           drop=set(state.get('focus_fields_added', [])), drop_table=state.get('keys_table_added', False),
                           originals=state.get('focus_originals', {}))
    tomllib.loads(result)
    return result

def other_spring_helpers():
    agents = Path.home()/'Library/LaunchAgents'
    return sorted(p.name for p in agents.glob('*focus-pane*.plist')) if agents.is_dir() else []

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--link', action='store_true', help='also run herdr plugin link for this checkout (manual install from a clone)')
    parser.add_argument('--check', action='store_true', help='print the setup plan without changing anything')
    parser.add_argument('--pause', action='store_true', help='wait for Enter before exiting (setup pane)')
    parser.add_argument('--config', type=Path, default=config_path())
    parser.add_argument('--tab-dwell-ms', type=int, default=600)
    parser.add_argument('--pane-dwell-ms', type=int, default=250)
    parser.add_argument('--tab-bar', choices=['top','bottom'])
    args = parser.parse_args()
    if not all(50 <= v <= 5000 for v in (args.tab_dwell_ms, args.pane_dwell_ms)):
        parser.error('dwell times must be 50..5000 ms')
    if args.pause:
        try:
            install(args)
        except (OSError, ValueError, SetupError, Interrupted) as error:
            print(f'\nherdr-spring-load setup failed: {error}')
        try:
            input('\nPress Enter to close.')
        except EOFError:
            pass
        return
    install(args)

def install(args):
    config = real_path(args.config)
    text = config.read_text() if config.exists() else ''
    updated = prepare_config(text) # Detect conflicts before building anything.
    position = args.tab_bar or ('bottom' if re.search(r'^tab_bar_position\s*=\s*[\"\']bottom', text, re.M) else 'top')
    support = SUPPORT
    binary = support / 'herdr-spring-load'
    plist = Path.home() / 'Library/LaunchAgents' / (LABEL+'.plist')
    print(f'Helper: {binary}\nConfig: {config}\nPrivate keys: ctrl+alt+shift+F1..F11\nTab bar: {position}')
    for other in other_spring_helpers():
        print(f'Warning: {other} is installed; two drag helpers would undo each other\'s navigation.')
    if args.check:
        print('Check only: no files changed and no services contacted.')
        return
    if sys.platform != 'darwin':
        raise SetupError('setup requires macOS')
    herdr = herdr_binary()
    accepted = frozenset()
    if herdr:
        # herdr's own parser: refuse before any change if the new config would make
        # herdr disable or reject anything that the current one does not.
        accepted = config_diagnostics(herdr, text) if text else frozenset()
        new = sorted(config_diagnostics(herdr, updated) - accepted)
        if new:
            raise SetupError('herdr would not accept the new keybindings: '+'; '.join(new))
    elif args.link:
        raise SetupError('herdr not found')
    compiler = find_swiftc()
    launchctl = tool('launchctl')
    logs = Path.home()/'Library/Logs'
    env = {k:v for k,v in os.environ.items() if k in ('HERDR_CONFIG_PATH', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME')}
    env['PATH'] = '/usr/bin:/bin:/usr/sbin:/sbin'
    # Only Application Support paths are recorded: during herdr plugin install this
    # checkout is a temporary directory that is renamed afterwards.
    launch = {'Label': LABEL, 'ProgramArguments':[str(binary), '--tab-dwell-ms', str(args.tab_dwell_ms),
               '--pane-dwell-ms', str(args.pane_dwell_ms), '--tab-bar', position],
              'RunAtLoad':True, 'KeepAlive':{'SuccessfulExit':False}, 'ThrottleInterval':10,
              'ProcessType':'Interactive', 'EnvironmentVariables':env,
              'StandardErrorPath':str(logs/'herdr-spring-load.stderr.log')}
    target = f'gui/{os.getuid()}/{LABEL}'
    transaction = Transaction()
    was_loaded = False
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupt)
    transaction.mkdir(support)
    lock = open(support/'setup.lock', 'w')
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError('another herdr-spring-load setup is running')
        state_file = support/'state.json'
        previous = json.loads(state_file.read_text()) if state_file.exists() else {}
        with tempfile.TemporaryDirectory(prefix='spring-load-build.') as build:
            built = Path(build)/'herdr-spring-load'
            checked([compiler, '-O', '-module-cache-path', str(Path(build)/'cache'), str(ROOT/'helper/FocusPaneVendor.swift'),
                     str(ROOT/'helper/DragLogic.swift'), str(ROOT/'helper/main.swift'), '-o', str(built)], 'compiling the helper')
            checked([str(built), '--validate'], 'validating the helper')
            keychain, identity = signing_identity(support, transaction)
            sign(built, keychain, identity)
            if args.link:
                linked = 'herdr-spring-load' in run([herdr, 'plugin', 'list']).stdout
                checked([herdr, 'plugin', 'link', str(ROOT)], 'herdr plugin link')
                if not linked:
                    transaction.undo.append(lambda: run([herdr, 'plugin', 'unlink', 'herdr-spring-load']))
            # Stop our previous helper only, after the replacement is built and signed.
            was_loaded = run([launchctl, 'print', target]).returncode == 0
            if was_loaded:
                launchd_stop(launchctl, target)
            transaction.write(binary, built.read_bytes(), 0o755)
        # herdr deletes the plugin checkout on uninstall, so the helper keeps its own
        # copy of the removal scripts (see the registration check in main.swift).
        lib = support/'lib'
        transaction.mkdir(lib)
        for name in LIB_FILES:
            transaction.write(lib/name, (ROOT/name).read_bytes(), 0o644)
        transaction.remove(support/SEEN) # set again by the helper once herdr registers the plugin
        state = {**focus_state(text, previous), 'python': sys.executable, 'uninstall': str(lib/'uninstall.py'),
                 'config': str(config)}
        transaction.write(state_file, json.dumps(state, indent=1).encode(), 0o600)
        if updated != text:
            # The minute-long build gives other tools time to edit the config.
            current = config.read_text() if config.exists() else ''
            if current != text:
                raise SetupError(f'{config} changed during setup; run it again')
            if config.exists():
                backup = config.with_name(config.name+f'.spring-load-{time.time_ns()}.bak')
                transaction.write(backup, config.read_bytes(), 0o600)
                print(f'Config backup: {backup}')
            transaction.mkdir(config.parent)
            transaction.write(config, updated.encode(), file_mode(config))
        transaction.mkdir(logs)
        transaction.write(plist, plistlib.dumps(launch), 0o644)
        launchd_start(launchctl, plist)
        transaction.undo.append(lambda: launchd_stop(launchctl, target, timeout=5))
        reloaded = reload_servers(transaction, accepted)
    except BaseException:
        transaction.rollback()
        if was_loaded and plist.exists():
            run([launchctl, 'bootstrap', f'gui/{os.getuid()}', str(plist)])
        raise
    finally:
        lock.close()
    prune_backups(config)
    print('Config reloaded in: '+(', '.join(map(str, reloaded)) or 'no running herdr server (applies when one starts)'))
    print('\nSetup complete. macOS asks once for Accessibility for herdr-spring-load,')
    print('and once for Automation of Ghostty. In Ghostty set macos-applescript = true.')
    print('Then hold a file over a space, a tab or a pane; release to drop.')
    print('Before herdr plugin uninstall, run the plugin action "Remove herdr-spring-load helper')
    print('and keybindings". If you skip it, the helper removes itself on the next drag.')

if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, SetupError, Interrupted) as error:
        sys.exit(f'herdr-spring-load setup: {error}')
