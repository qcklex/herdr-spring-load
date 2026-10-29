#!/usr/bin/env python3
"""Remove herdr-spring-load: stop the login service and delete its definition,
the sensor, the core copy and the support files. When a 0.2.0 install left its
private keybindings in herdr's config, they are taken out as 0.2.0 recorded
them. Run by the plugin's remove-helper action, helper/uninstall.sh, or by the
core itself (--self) once herdr no longer lists the plugin. Transactional up to
the point where files are deleted: a config that cannot be restored changes
nothing."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

from installer import Interrupted, SetupError, Transaction, file_mode, interrupt, real_path, service_for, support_dir
import legacy
from spring_load.clients import herdr_dirs

KEEP = ('setup.lock',)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self', action='store_true', help='run by the helper after herdr uninstalled the plugin')
    parser.add_argument('--purge', action='store_true', help='accepted for 0.2.0 compatibility; everything is removed anyway')
    parser.parse_args()
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupt)
    support = support_dir()
    state_file = support/'state.json'
    try:
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
    except ValueError:
        state = {}
    config_path = real_path(Path(state['config']) if state.get('config') else
                            (Path(os.environ['HERDR_CONFIG_PATH']).expanduser() if os.environ.get('HERDR_CONFIG_PATH')
                             else herdr_dirs(os.environ)[0]/'config.toml'))
    restored = legacy.restored_config(config_path, state)   # worked out first: a refusal changes nothing
    service = service_for()
    lock = None
    if support.exists():
        lock = open(support/'setup.lock', 'w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError('a herdr-spring-load setup is running; try again when it finishes')
    transaction = Transaction()
    try:
        if restored:
            before, after = restored
            transaction.write(config_path.with_name(config_path.name + '.spring-load-uninstall.bak'), before.encode(), 0o600)
            transaction.write(config_path, after.encode(), file_mode(config_path))
        if service is not None:
            transaction.remove(service.plist)
    except BaseException:
        transaction.rollback()
        raise
    transaction.commit()
    if restored:
        from spring_load.herdr_api import request
        legacy.reload_sessions(herdr_dirs(os.environ)[0], request)
    try:
        legacy.remove_signing(support)
    except (OSError, SetupError):
        pass
    if support.exists():
        for entry in support.iterdir():
            if entry.name in KEEP:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
    if lock:
        lock.close()
        (support/'setup.lock').unlink(missing_ok=True)
        try:
            support.rmdir()
        except OSError:
            pass
    print('herdr-spring-load helper removed.' + (' Private 0.2.0 keybindings restored.' if restored else ''))
    if not parse_self():
        print('To unregister the plugin: herdr plugin uninstall herdr-spring-load')
    sys.stdout.flush()
    # Last: when the helper runs this, stopping the service ends this script too.
    if service is not None and service.loaded():
        service.stop(timeout=5)

def parse_self():
    return '--self' in sys.argv[1:]

if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, SetupError, Interrupted, subprocess.SubprocessError) as error:
        sys.exit(f'herdr-spring-load uninstall: {error}')
