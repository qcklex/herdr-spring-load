"""The plugin's Windows entries of herdr-plugin.toml, run as herdr would (CI):

    python tests/windows/plugin_steps.py

In a scratch profile (LOCALAPPDATA, APPDATA and XDG_* under RUNNER_TEMP): the
[[build]] step for platform windows, the built sensor through the contract
check, the status-windows action, then remove-helper-windows. Checks the Run
key value, the running helper and that removal leaves nothing behind. The Run
key itself is the runner's HKCU (not part of any profile folder); the removal
step deletes it again."""
import os
from pathlib import Path
import shutil
import subprocess
import time
import tomllib
import winreg

from common import ROOT, Check, profile_env, require_windows, run, scratch, wait, write_config
from sensor_check import check_sensor

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
VALUE = 'herdr-spring-load'

def run_value():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, VALUE)[0]
    except FileNotFoundError:
        return None

def for_windows(entries):
    return [e for e in entries if 'windows' in e.get('platforms', [])]

def resolve(command):
    """herdr runs the command's first word from PATH, as here."""
    return [shutil.which(command[0]) or command[0], *command[1:]]

def main():
    require_windows()
    check = Check('plugin-steps')
    manifest = tomllib.loads((ROOT/'herdr-plugin.toml').read_text())
    check.ok('manifest lists windows', 'windows' in manifest['platforms'], manifest['platforms'])
    builds = for_windows(manifest.get('build', []))
    actions = {a['id']: a for a in for_windows(manifest.get('actions', []))}
    check.ok('one [[build]] for windows', len(builds) == 1, builds)
    check.ok('status and remove actions for windows', {'status-windows', 'remove-helper-windows'} <= set(actions), sorted(actions))
    check.ok('no Run key value before setup', run_value() is None, run_value())

    base = scratch('plugin')
    env = profile_env(base)
    write_config(env)
    support = Path(env['LOCALAPPDATA'])/'herdr-spring-load'

    result = run(resolve(builds[0]['command']), env=env, cwd=ROOT, timeout=600)
    check.ok('[[build]] (python -B setup.py) succeeds', result.returncode == 0, result.returncode)
    check.ok('setup says no permission is needed', 'No permission is needed' in result.stdout)
    value = run_value()
    check.ok('Run key value starts the helper with pythonw', value and 'pythonw' in value.lower() and 'run_helper.py' in value, value)
    sensor = support/'sensors'/'herdr-spring-load-sensor-windows.exe'
    check.ok('built sensor installed', sensor.is_file(), str(sensor))
    check.ok('helper pid recorded', wait(lambda: (support/'helper.pid').read_text().strip().isdigit(), 15),
             str(support/'helper.pid'))
    check_sensor(check, [str(sensor)], 4, 'built-sensor', env=env)

    status = actions['status-windows']['command']
    def status_text():
        result = run(resolve(status), env=env, cwd=ROOT, timeout=60)
        return result.stdout if result.returncode == 0 and 'no status yet' not in result.stdout else None
    text = wait(status_text, 30)
    check.ok('status-windows reports a running helper', text and 'not running' not in text, text)
    check.note('status output', text)

    result = run(resolve(actions['remove-helper-windows']['command']), env=env, cwd=ROOT, timeout=300)
    check.ok('remove-helper-windows (python -B uninstall.py) succeeds', result.returncode == 0, result.returncode)
    check.ok('Run key value removed', run_value() is None, run_value())
    left = sorted(p.name for p in support.iterdir()) if support.exists() else []
    check.ok('support folder removed (setup.lock may stay)', set(left) <= {'setup.lock'}, left)
    result = run(resolve(status), env=env, cwd=ROOT, timeout=60)
    check.ok('status after removal says no status', 'no status yet' in result.stdout, result.stdout)
    check.done()
    print('windows plugin steps: build, built sensor, status, remove PASS')

if __name__ == '__main__':
    main()
