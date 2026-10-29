#!/usr/bin/env python3
"""Remove the helper, its LaunchAgent and the keybindings setup added; restore the
config exactly as setup recorded it. Transactional: on failure nothing is removed.
Run by the plugin's remove-helper action, helper/uninstall.sh, or the helper itself
(--self) once herdr no longer lists the plugin. --purge also deletes the signing
keychain, probes and the rest of Application Support (macOS then asks for the
Accessibility grant again on a later install)."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tomllib
from setup import (BEGIN, END, LABEL, SEEN, SUPPORT, LIB_FILES, Interrupted, SetupError, Transaction, config_diagnostics,
                   config_path, delete_keychain, drop_from_search_list, file_mode, herdr_binary, interrupt, launchd_stop,
                   real_path, reload_servers, rewrite_focus, run, strip_block, tool, unprepare_config)

def legacy_unprepare(text):
    # Installs from before state.json: strip the block and the private aliases only.
    without = strip_block(text)
    return rewrite_focus(without, tomllib.loads(without), remove=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self', action='store_true', help='run by the helper after herdr uninstalled the plugin')
    parser.add_argument('--purge', action='store_true', help='also delete the signing keychain and all support files')
    args = parser.parse_args()
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupt)
    state_file = SUPPORT/'state.json'
    state = json.loads(state_file.read_text()) if state_file.exists() else None
    config = real_path(Path(state['config']) if state and state.get('config') else config_path())
    before = config.read_text() if config.exists() else None
    # Work out the restored config before touching anything; a refusal changes nothing.
    after = None if before is None else (unprepare_config(before, state) if state else legacy_unprepare(before))
    herdr = herdr_binary()
    accepted = config_diagnostics(herdr, after) if herdr and after else None
    launchctl = tool('launchctl')
    plist = Path.home()/'Library/LaunchAgents'/(LABEL+'.plist')
    transaction = Transaction()
    lock = None
    if SUPPORT.exists():
        lock = open(SUPPORT/'setup.lock', 'w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError('a herdr-spring-load setup is running; try again when it finishes')
    try:
        if before is not None and after != before:
            transaction.write(config.with_name(config.name+'.spring-load-uninstall.bak'), before.encode(), 0o600)
            transaction.write(config, after.encode(), file_mode(config))
            reload_servers(transaction, accepted)
        for path in [SUPPORT/'herdr-spring-load', plist, SUPPORT/SEEN, state_file, *(SUPPORT/'lib'/n for n in LIB_FILES)]:
            transaction.remove(path)
    except BaseException:
        transaction.rollback()
        raise
    # Past this point the config and files are committed; the rest cannot fail the uninstall.
    lib = SUPPORT/'lib'
    if lib.is_dir():
        shutil.rmtree(lib/'__pycache__', ignore_errors=True)
        try:
            lib.rmdir()
        except OSError:
            pass
    if args.purge and SUPPORT.exists():
        keychain = SUPPORT/'signing/signing.keychain-db'
        try:
            security = tool('security')
            delete_keychain(security, keychain)
        except (OSError, SetupError):
            pass
        if lock:
            lock.close()
            lock = None
        shutil.rmtree(SUPPORT, ignore_errors=True)
    print('Helper removed; keybindings restored.'+('' if args.purge else ' Signing identity kept, so a reinstall keeps the macOS grants (use --purge to delete it).'))
    if not args.self:
        print('To unregister the plugin: herdr plugin uninstall herdr-spring-load')
    sys.stdout.flush()
    # Last: when run by the helper, this stops the helper and therefore this script.
    launchd_stop(launchctl, f'gui/{os.getuid()}/{LABEL}', timeout=5)

if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, SetupError, Interrupted) as error:
        sys.exit(f'herdr-spring-load uninstall: {error}')
