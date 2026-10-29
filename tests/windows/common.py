"""Shared pieces of the Windows CI scripts (tests/windows/*.py). They run only
on a disposable Windows machine (the CI runner): each works in a scratch
profile under RUNNER_TEMP (or a temp folder) and never touches another herdr."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

CONFIG = ('onboarding = false\n[update]\nversion_check = false\nmanifest_check = false\n'
          '[ui]\nsidebar_width = 26\nprompt_new_tab_name = false\n')

def require_windows():
    if sys.platform != 'win32':
        sys.exit(f'{Path(sys.argv[0]).name} runs only on Windows (the CI runner)')

def scratch(name):
    base = Path(os.environ.get('RUNNER_TEMP') or tempfile.gettempdir())/f'spring-load-{name}'
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix='p.', dir=base))

def profile_env(base):
    """os.environ with herdr's and the plugin's folders moved into `base`
    (herdr: XDG_CONFIG_HOME/XDG_STATE_HOME first; the plugin: LOCALAPPDATA)."""
    env = dict(os.environ)
    for key, sub in (('XDG_CONFIG_HOME', 'c'), ('XDG_STATE_HOME', 's'), ('APPDATA', 'Roaming'), ('LOCALAPPDATA', 'Local')):
        env[key] = str(base/sub)
        (base/sub).mkdir(parents=True, exist_ok=True)
    for key in ('HERDR_SESSION', 'HERDR_SOCKET_PATH', 'HERDR_CLIENT_SOCKET_PATH', 'HERDR_CONFIG_PATH'):
        env.pop(key, None)
    return env

def write_config(env, register=True):
    """herdr's config (and the plugin's registration) in the scratch profile,
    dated 5 s back so a client started now is younger than its config."""
    config = Path(env['XDG_CONFIG_HOME'])/'herdr'/'config.toml'
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(CONFIG)
    if register:
        (config.parent/'plugins.json').write_text(json.dumps([{'plugin_id': 'herdr-spring-load', 'enabled': True,
                                                              'manifest_path': str(ROOT/'herdr-plugin.toml')}]))
    os.utime(config, (time.time() - 5, time.time() - 5))
    return config

def wait(check, timeout=10, errors=(OSError, ValueError, KeyError, StopIteration, AttributeError)):
    until = time.time() + timeout
    while time.time() < until:
        try:
            value = check()
            if value:
                return value
        except errors:
            pass
        time.sleep(0.1)
    return None

def run(argv, env=None, timeout=300, **kwargs):
    print('$ ' + subprocess.list2cmdline([str(a) for a in argv]), flush=True)
    result = subprocess.run([str(a) for a in argv], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            timeout=timeout, **kwargs)
    if result.stdout:
        print(result.stdout.rstrip(), flush=True)
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr, flush=True)
    return result

def write_report(name, data):
    folder = Path(os.environ.get('SPRING_LOAD_REPORTS') or ROOT/'tests'/'windows'/'out')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder/f'{name}.json'
    path.write_text(json.dumps(data, indent=1, default=str))
    print(f'report: {path}', flush=True)
    return path

class Check:
    """Collects named results; `fail` on the first failed assertion."""
    def __init__(self, name):
        self.name = name
        self.results = []

    def ok(self, label, condition, detail=None):
        self.results.append({'check': label, 'ok': bool(condition), 'detail': detail})
        print(f'{"PASS" if condition else "FAIL"} {label}' + (f': {detail}' if detail is not None else ''), flush=True)
        if not condition:
            write_report(self.name, {'results': self.results})
            sys.exit(1)

    def note(self, label, detail):
        self.results.append({'check': label, 'ok': None, 'detail': detail})
        print(f'NOTE {label}: {detail}', flush=True)

    def done(self, extra=None):
        write_report(self.name, {'results': self.results, **(extra or {})})
