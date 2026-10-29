"""Is the plugin still installed in herdr? herdr has no uninstall hook, so the
core checks herdr's plugin registry (config_dir/plugins.json) when a drag
starts: once seen registered and then gone, it removes itself; disabled or
moved, it only goes inert."""
import json
from pathlib import Path

PLUGIN_ID = 'herdr-spring-load'

def registration(config_dir, plugin_id=PLUGIN_ID, exists=lambda p: Path(p).exists()):
    """'registered', 'disabled', 'manifest_missing', 'missing' or 'unreadable'."""
    path = Path(config_dir)/'plugins.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return 'missing'
    except (OSError, ValueError):
        return 'unreadable'
    if not isinstance(data, list):
        return 'unreadable'
    entry = next((e for e in data if isinstance(e, dict) and e.get('plugin_id') == plugin_id), None)
    if entry is None:
        return 'missing'
    if entry.get('enabled') is False:
        return 'disabled'
    manifest = entry.get('manifest_path')
    if not isinstance(manifest, str) or not exists(manifest):
        return 'manifest_missing'
    return 'registered'

def manifest_path(config_dir, plugin_id=PLUGIN_ID):
    """The registered manifest path, or None."""
    try:
        data = json.loads((Path(config_dir)/'plugins.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    entry = next((e for e in data if isinstance(e, dict) and e.get('plugin_id') == plugin_id), None) if isinstance(data, list) else None
    manifest = entry.get('manifest_path') if entry else None
    return manifest if isinstance(manifest, str) else None

def action(state, seen_registered):
    """'run', 'pause' or 'remove'. The install build step runs before herdr
    registers the plugin, so 'missing' removes only after a registration was seen."""
    if state == 'registered':
        return 'run'
    if state == 'missing' and seen_registered:
        return 'remove'
    return 'pause'

REASONS = {
    'disabled': 'the plugin is disabled in herdr (herdr plugin enable herdr-spring-load)',
    'manifest_missing': "herdr's plugin entry points to a manifest that no longer exists",
    'unreadable': "herdr's plugins.json could not be read",
    'missing': 'the plugin is not registered with herdr yet',
}
