"""Where the plugin keeps its own files on each OS."""
import os
from pathlib import Path
import sys

def support_dir(env=None, platform=None):
    env = os.environ if env is None else env
    platform = platform or sys.platform
    home = Path(env.get('HOME') or env.get('USERPROFILE') or Path.home())
    if platform == 'darwin':
        return home/'Library/Application Support/herdr-spring-load'
    if platform in ('win32', 'cygwin'):
        base = env.get('LOCALAPPDATA')
        return (Path(base) if base else home/'AppData/Local')/'herdr-spring-load'
    base = env.get('XDG_DATA_HOME')
    return (Path(base) if base else home/'.local/share')/'herdr-spring-load'

def log_file(env=None, platform=None):
    env = os.environ if env is None else env
    platform = platform or sys.platform
    if platform == 'darwin':
        home = Path(env.get('HOME') or Path.home())
        return home/'Library/Logs/herdr-spring-load.log'
    return support_dir(env, platform)/'herdr-spring-load.log'
