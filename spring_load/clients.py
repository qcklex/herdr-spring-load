"""herdr client processes: which session they attach to, and the
exactly-one-local-client gate. herdr 0.9.3's public focus calls move every
attached client (server/headless/client_views.rs), so the core acts only when
the hovered client is the only local one attached to its session."""
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import sys
import time

SESSION_NAME = re.compile(r'^[A-Za-z0-9._-]{1,64}$')

def herdr_dirs(env, platform=None):
    """(config_dir, state_dir) the way herdr's config/io.rs resolves them for `env`."""
    platform = platform or sys.platform
    windows = platform in ('win32', 'cygwin')
    home = env.get('HOME') or env.get('USERPROFILE') or ''
    if env.get('XDG_CONFIG_HOME'):
        config = Path(env['XDG_CONFIG_HOME'])/'herdr'
    elif windows and env.get('APPDATA'):
        config = Path(env['APPDATA'])/'herdr'
    elif windows and env.get('USERPROFILE'):
        config = Path(env['USERPROFILE'])/'AppData/Roaming/herdr'
    else:
        config = Path(home)/'.config/herdr'
    if env.get('XDG_STATE_HOME'):
        state = Path(env['XDG_STATE_HOME'])/'herdr'
    elif windows and env.get('LOCALAPPDATA'):
        state = Path(env['LOCALAPPDATA'])/'herdr'
    elif windows and env.get('USERPROFILE'):
        state = Path(env['USERPROFILE'])/'AppData/Local/herdr'
    else:
        state = Path(home)/'.local/state/herdr'
    return config, state

def classify(argv):
    """('local', session-or-None), ('remote', None), or None (server, CLI, not herdr)."""
    if not argv or Path(argv[0].replace('\\', '/')).name.lower() not in ('herdr', 'herdr.exe'):
        return None
    session = None
    rest = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == '--session' and i + 1 < len(argv):
            session = argv[i + 1]
            i += 2
            continue
        if arg.startswith('--session='):
            session = arg.split('=', 1)[1]
        elif arg in ('--remote', '--machine') or arg.startswith(('--remote=', '--machine=')):
            return ('remote', None)
        else:
            rest.append(arg)
        i += 1
    if not rest:
        return ('local', session)
    if len(rest) == 3 and rest[:2] == ['session', 'attach']:
        return ('local', rest[2])
    return None

@dataclass(frozen=True)
class Binding:
    """Where a local client's session lives (session.rs, server/socket_paths.rs)."""
    api_socket: Path
    client_socket: Path
    config_dir: Path
    state_dir: Path
    config_path: Path

class Unbound(Exception):
    pass

def bind(client, fallback_env=None, platform=None):
    """The session a local client attaches to, from its own environment
    (docs/SENSOR.md). herdr picks the session and its directories from that
    environment, so a client whose environment the sensor could not read is
    not bound at all: guessing from the core's own environment could name
    another session's server. `fallback_env` is accepted for compatibility and
    ignored."""
    kind = classify(client.get('argv') or [])
    if kind is None or kind[0] != 'local':
        raise Unbound('not a local herdr client')
    env = client.get('env')
    if env is None:
        raise Unbound("the sensor could not read this herdr client's environment, so its session is unknown")
    config_dir, state_dir = herdr_dirs(env, platform)
    explicit = kind[1]
    def data_dir(name):
        if name is None or name == 'default':
            return config_dir
        if not SESSION_NAME.match(name) or name in ('.', '..'):
            raise Unbound(f'session name {name!r} is not understood')
        return config_dir/'sessions'/name
    if explicit is not None:
        folder = data_dir(explicit)
        api, client_socket = folder/'herdr.sock', folder/'herdr-client.sock'
    elif env.get('HERDR_SOCKET_PATH'):
        api = Path(env['HERDR_SOCKET_PATH'])
        client_socket = api.parent/f'{api.stem or "herdr"}-client.sock'
    else:
        name = env.get('HERDR_SESSION')
        folder = data_dir(name if name and SESSION_NAME.match(name) else None)
        api = folder/'herdr.sock'
        client_socket = Path(env['HERDR_CLIENT_SOCKET_PATH']) if env.get('HERDR_CLIENT_SOCKET_PATH') else folder/'herdr-client.sock'
    config_path = Path(env['HERDR_CONFIG_PATH']).expanduser() if env.get('HERDR_CONFIG_PATH') else config_dir/'config.toml'
    return Binding(api, client_socket, config_dir, state_dir, config_path)

def split_ps_args(args):
    """argv from ps's space-joined args. The executable path may contain spaces,
    so the first prefix whose base name is herdr is taken as argv[0]."""
    tokens = args.split()
    for i in range(len(tokens)):
        head = ' '.join(tokens[:i + 1])
        if Path(head).name in ('herdr', 'herdr.exe'):
            return [head] + tokens[i + 1:]
    return tokens

def list_clients_ps():
    """Every process from `ps` (macOS, Linux), as contract client objects with
    only pid, argv and tty filled in. None where ps is not available."""
    try:
        out = subprocess.run(['ps', '-axww', '-o', 'pid=,tty=,args='], capture_output=True, text=True, timeout=2,
                             stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        argv = split_ps_args(parts[2])
        if classify(argv) is None:
            continue
        tty = parts[1]
        found.append({'pid': int(parts[0]), 'argv': argv, 'tty': None if tty in ('?', '??', '-') else '/dev/' + tty.removeprefix('/dev/'),
                      'rows': None, 'cols': None, 'xpixel': 0, 'ypixel': 0, 'foreground': None, 'env': None})
    return found

def process_start(pid, platform=None):
    """Start time of a process (epoch seconds) from ps, or None."""
    if (platform or sys.platform) in ('win32', 'cygwin'):
        return None
    try:
        out = subprocess.run(['ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True, text=True, timeout=2,
                             stdin=subprocess.DEVNULL, env={'LC_ALL': 'C', 'PATH': '/bin:/usr/bin'}).stdout
        return time.mktime(time.strptime(' '.join(out.split()), '%a %b %d %H:%M:%S %Y'))
    except (OSError, subprocess.SubprocessError, ValueError, OverflowError):
        return None

def attached_count(binding, everyone, fallback_env=None, platform=None):
    """Local clients that are, or may be, attached to the same session as
    `binding` (those with a tty). A local client whose session cannot be worked
    out (environment unreadable, no --session in argv) counts as attached: the
    gate must not under-count, since herdr's focus calls move every client."""
    count = 0
    for other in everyone:
        if not other.get('tty') and platform not in ('win32', 'cygwin'):
            continue
        kind = classify(other.get('argv') or [])
        if kind is None or kind[0] != 'local':
            continue
        try:
            if bind(other, None, platform).api_socket == binding.api_socket:
                count += 1
        except Unbound:
            count += 1
    return count

def remote_clients(everyone):
    return [c for c in everyone if (classify(c.get('argv') or []) or ('',))[0] == 'remote']
