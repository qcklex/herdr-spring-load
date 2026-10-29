import importlib.util
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import setup

class SetupTest(unittest.TestCase):
    def test_preserves_config_and_is_repeatable(self):
        text = '[ui]\ntab_bar_position = "bottom"\n'
        result = setup.prepare_config(text)
        self.assertIn(text, result)
        self.assertEqual(setup.prepare_config(result), result)
        self.assertEqual(result.count('[[keys.command]]'), 7)

    def test_conflicting_binding_refused(self):
        text = '[[keys.command]]\nkey = "ctrl+alt+shift+f1"\ntype = "command"\ncommand = "other"\n'
        with self.assertRaises(ValueError):
            setup.prepare_config(text)

    def test_reordered_indented_collision_refused(self):
        with self.assertRaises(ValueError):
            setup.prepare_config('[[keys.command]]\n  key = "shift+control+option+F1"\n  type = "shell"\n  command = "unexpected"\n')

    def test_meta_alias_collision_refused(self):
        with self.assertRaises(ValueError):
            setup.prepare_config('[keys]\nnew_tab = "meta+ctrl+shift+f1"\n')

    def test_focus_aliases_stay_in_keys_before_command_tables(self):
        import tomllib
        original=('onboarding = false\n[keys]\nprefix = "ctrl+b"\n[[keys.command]]\nkey = "ctrl+x"\ntype = "shell"\ncommand = "a"\n'
                  '[[keys.command]]\nkey = "ctrl+y"\ntype = "shell"\ncommand = "b"\n[ui]\nsidebar_width = 26\n')
        parsed=tomllib.loads(setup.prepare_config(original))
        self.assertIs(parsed['onboarding'], False)
        self.assertEqual(parsed['keys']['focus_pane_left'], ['prefix+h','ctrl+alt+shift+f8'])
        self.assertEqual(parsed['keys']['command'][:2], [{'key':'ctrl+x','type':'shell','command':'a'},{'key':'ctrl+y','type':'shell','command':'b'}])
        self.assertEqual(parsed['ui'], {'sidebar_width': 26})

    def test_original_focus_shortcuts_preserved(self):
        import tomllib
        original='[keys]\nfocus_pane_left = [\n "alt+h",\n "prefix+h",\n] # keep alternatives\n'
        updated=setup.prepare_config(original)
        self.assertEqual(tomllib.loads(updated)['keys']['focus_pane_left'],['alt+h','prefix+h','ctrl+alt+shift+f8'])
        removed=setup.rewrite_focus(updated,tomllib.loads(updated),remove=True)
        self.assertEqual(tomllib.loads(removed)['keys']['focus_pane_left'],['alt+h','prefix+h'])

    def test_new_keys_table_keeps_top_level_settings(self):
        import tomllib
        original='onboarding = false\ntheme = "dark"\n[[keys.command]]\nkey = "ctrl+x"\ntype = "shell"\ncommand = "y"\n[ui]\nsidebar_width = 26\n'
        parsed=tomllib.loads(setup.prepare_config(original))
        self.assertIs(parsed['onboarding'], False)
        self.assertEqual(parsed['theme'], 'dark')
        self.assertNotIn('onboarding', parsed['keys'])
        self.assertEqual(parsed['ui'], {'sidebar_width': 26})
        self.assertEqual(parsed['keys']['command'][0]['command'], 'y')

    def test_unrelated_change_refused(self):
        original='[keys]\nfocus_pane_left = "prefix+h"\n'
        real=setup.rewrite_focus
        setup.rewrite_focus=lambda text, parsed, remove=False: real(text, parsed, remove)+'stray = 1\n'
        try:
            with self.assertRaises(ValueError):
                setup.prepare_config(original)
        finally:
            setup.rewrite_focus=real

    def test_mouse_capture_disabled_refused(self):
        with self.assertRaises(ValueError):
            setup.prepare_config('[ui]\nmouse_capture=false\n')

    def test_check_is_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, HOME=temp)
            result = subprocess.run([sys.executable, str(ROOT/'setup.py'), '--check'], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_atomic_write(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp)/'a & b.plist'
            setup.atomic_write(file, b'one')
            setup.atomic_write(file, b'two')
            self.assertEqual(file.read_bytes(), b'two')
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
