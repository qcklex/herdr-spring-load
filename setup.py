#!/usr/bin/env python3
"""Install or update herdr-spring-load 0.3.0. Run by herdr plugin install
([[build]]), by the setup pane, or by helper/install.sh. Transactional: a
failure leaves the previous install in place.

What it does: builds this OS's sensor and checks it (`--check`), copies the
core (spring_load/, stdlib only) to the support folder, and starts the login
service that runs `python3 -m spring_load run`. It asks for no OS permission,
signs nothing and does not edit herdr's config. The one exception is migration:
an install of 0.2.0 left private keybindings in config.toml, a signing keychain
and a terminal-specific helper; those are removed again (legacy.py)."""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

from installer import (LABEL, ROOT, SEEN, VERSION, Interrupted, SetupError, Transaction, file_mode, interrupt, real_path,
                       run, service_for, support_dir)
import legacy
from spring_load import contract
from spring_load.clients import herdr_dirs
from spring_load.sensor import platform_key

LIB_FILES = ('installer.py', 'legacy.py', 'uninstall.py')
ENV_KEYS = ('HERDR_CONFIG_PATH', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME')

def build_sensor(out):
    """Build this OS's sensor into `out` and check it speaks the contract."""
    key = platform_key()
    script = ROOT/'sensors'/key/'build.py'
    if not script.exists():
        raise SetupError(f'herdr-spring-load has no sensor for {key} yet')
    spec = importlib.util.spec_from_file_location(f'sensor_build_{key}', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        module.build(out)
    except RuntimeError as error:
        raise SetupError(str(error)) from None
    result = run([str(out), '--check'], timeout=30)
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 1:
        raise SetupError(f'the built sensor failed its check: {(result.stderr or result.stdout).strip()[-300:]}')
    try:
        contract.validate_hello(json.loads(lines[0]))
    except (ValueError, contract.ContractError) as error:
        raise SetupError(f'the built sensor speaks another contract: {error}') from None

def stage_lib(folder):
    """The core and the removal scripts, as they will run from the support folder."""
    shutil.copytree(ROOT/'spring_load', folder/'spring_load', ignore=shutil.ignore_patterns('__pycache__'))
    for name in LIB_FILES:
        shutil.copy2(ROOT/name, folder/name)
    check = run([sys.executable, '-c', 'import spring_load.__main__, installer, legacy, uninstall'], cwd=str(folder),
                env={'PYTHONPATH': str(folder), 'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PYTHONDONTWRITEBYTECODE': '1'})
    if check.returncode != 0:
        raise SetupError(f'the core does not load with {sys.executable}: {check.stderr.strip()[-300:]}')

def legacy_config(previous):
    """(path, before, after) when 0.2.0's keybindings are still in the config."""
    path = previous.get('config')
    config = real_path(Path(path) if path else legacy_config_path())
    restored = legacy.restored_config(config, previous)
    return None if restored is None else (config, *restored)

def legacy_config_path():
    override = os.environ.get('HERDR_CONFIG_PATH')
    if override:
        return Path(override).expanduser()
    return herdr_dirs(os.environ)[0]/'config.toml'

def apply_legacy_config(transaction, found):
    config, before, after = found
    if config.read_text() != before:
        raise SetupError(f'{config} changed during setup; run it again')
    transaction.write(config.with_name(config.name + '.spring-load-0.2.bak'), before.encode(), 0o600)
    transaction.write(config, after.encode(), file_mode(config))
    from spring_load.herdr_api import request
    return legacy.reload_sessions(herdr_dirs(os.environ)[0], request)

def other_spring_helpers(home):
    agents = home/'Library/LaunchAgents'
    return sorted(p.name for p in agents.glob('*focus-pane*.plist')) if agents.is_dir() else []

def install(args):
    support = support_dir()
    service = service_for()
    print(f'herdr-spring-load {VERSION}\nSupport folder: {support}')
    if service is None:
        raise SetupError(f'herdr-spring-load cannot run as a login service on {sys.platform} yet')
    for other in other_spring_helpers(Path.home()):
        print(f"Warning: {other} is installed; two drag helpers would undo each other's navigation.")
    if args.check:
        print('Check only: nothing changed.')
        return
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupt)
    transaction = Transaction()
    transaction.mkdir(support)
    lock = open(support/'setup.lock', 'w')
    was_loaded = False
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError('another herdr-spring-load setup is running')
        state_file = support/'state.json'
        try:
            previous = json.loads(state_file.read_text()) if state_file.exists() else {}
        except ValueError:
            previous = {}
        found = legacy_config(previous)   # refuse before building when the config cannot be restored
        sensor_name = f'herdr-spring-load-sensor-{platform_key()}' + ('.exe' if platform_key() == 'windows' else '')
        with tempfile.TemporaryDirectory(prefix='spring-load-build.') as build:
            build = Path(build)
            (build/'sensors').mkdir()
            build_sensor(build/'sensors'/sensor_name)
            (build/'lib').mkdir()
            stage_lib(build/'lib')
            if args.link:
                herdr = shutil.which('herdr')
                if not herdr:
                    raise SetupError('herdr not found')
                linked = 'herdr-spring-load' in run([herdr, 'plugin', 'list']).stdout
                if run([herdr, 'plugin', 'link', str(ROOT)]).returncode != 0:
                    raise SetupError('herdr plugin link failed')
                if not linked:
                    transaction.undo.append(lambda: run([herdr, 'plugin', 'unlink', 'herdr-spring-load']))
            # Stop the previous helper only once the replacement is built and checked.
            was_loaded = service.loaded()
            if was_loaded:
                service.stop()
            transaction.replace_dir(build/'sensors', support/'sensors')
            transaction.replace_dir(build/'lib', support/'lib')
        for name in legacy.LEGACY_FILES[:1]:
            transaction.remove(support/name)      # 0.2.0's terminal-specific helper binary
        transaction.remove(support/SEEN)          # set again once herdr registers the plugin
        lib = support/'lib'
        state = {'version': VERSION, 'python': sys.executable, 'uninstall': str(lib/'uninstall.py'),
                 'service': type(service).__name__}
        transaction.write(state_file, json.dumps(state, indent=1).encode(), 0o600)
        reloaded = apply_legacy_config(transaction, found) if found else []
        env = {k: v for k, v in os.environ.items() if k in ENV_KEYS}
        env['PATH'] = '/usr/bin:/bin:/usr/sbin:/sbin'
        dwell = ['--tab-dwell-ms', str(args.tab_dwell_ms), '--pane-dwell-ms', str(args.pane_dwell_ms)]
        transaction.write(service.plist, service.definition(sys.executable, lib, dwell, env), 0o644)
        service.start()
        transaction.undo.append(lambda: service.stop(timeout=5))
    except BaseException:
        transaction.rollback()
        if was_loaded and service.plist.exists():
            try:
                service.start(attempts=1)
            except SetupError:
                pass
        raise
    finally:
        lock.close()
    transaction.commit()
    # After commit: 0.2.0's signing keychain is not needed any more (no TCC grant).
    try:
        if legacy.remove_signing(support):
            print("Removed 0.2.0's private signing keychain.")
    except (OSError, SetupError):
        pass
    if found:
        print(f"Removed 0.2.0's private keybindings from {found[0]}"
              + (f' (reloaded {len(reloaded)} running session(s))' if reloaded else ''))
    print('\nSetup complete. No permission is needed.')
    print('Hold a file over a Space, a tab or a pane in herdr to open it; release to drop as usual.')
    print('Spaces and tabs switch after you have clicked a different tab or Space once (that click')
    print('calibrates where herdr sits in the window). Panes work right away.')
    print('Status: run the plugin action "Spring-load helper status".')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--link', action='store_true', help='also run herdr plugin link for this checkout')
    parser.add_argument('--check', action='store_true', help='print what setup would do and change nothing')
    parser.add_argument('--pause', action='store_true', help='wait for Enter before exiting (setup pane)')
    parser.add_argument('--tab-dwell-ms', type=int, default=600)
    parser.add_argument('--pane-dwell-ms', type=int, default=250)
    args = parser.parse_args()
    if not all(50 <= v <= 5000 for v in (args.tab_dwell_ms, args.pane_dwell_ms)):
        parser.error('dwell times must be 50..5000 ms')
    if args.pause:
        try:
            install(args)
        except (OSError, ValueError, SetupError, Interrupted, subprocess.SubprocessError) as error:
            print(f'\nherdr-spring-load setup failed: {error}')
        try:
            input('\nPress Enter to close.')
        except EOFError:
            pass
        return
    install(args)

if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, SetupError, Interrupted, subprocess.SubprocessError) as error:
        sys.exit(f'herdr-spring-load setup: {error}')
