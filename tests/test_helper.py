import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

@unittest.skipUnless(shutil.which('swiftc'), 'requires macOS Swift')
class HelperTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.build = Path(cls.temp.name)
        cls.sources = [str(ROOT/'helper/FocusPaneVendor.swift'), str(ROOT/'helper/DragLogic.swift')]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def compile(self, main, name):
        binary = self.build/name
        subprocess.run(['swiftc', '-module-cache-path', str(self.build/'cache'), *self.sources, str(main), '-o', str(binary)], check=True, capture_output=True)
        return binary

    def test_navigation_regressions(self):
        binary = self.compile(ROOT/'tests/swift/main.swift', 'logic')
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertNotIn('FAIL', result.stdout)

    @unittest.skipUnless(shutil.which('herdr'), 'needs the herdr binary')
    def test_registry_written_by_real_herdr(self):
        # H1: parse plugins.json exactly as herdr writes it, in a scratch HOME.
        binary = self.compile(ROOT/'tests/swift/main.swift', 'logic')
        with tempfile.TemporaryDirectory(prefix='sl-reg.', dir='/private/tmp') as temp:
            env = {k: v for k, v in os.environ.items() if not k.startswith('HERDR')}
            env.update(HOME=temp, XDG_CONFIG_HOME=temp+'/c', XDG_STATE_HOME=temp+'/s')
            registry = Path(temp)/'c/herdr/plugins.json'
            def state():
                out = subprocess.run([str(binary), str(registry), 'registry'], capture_output=True, text=True, timeout=20).stdout
                return next(line.split()[1] for line in out.splitlines() if line.startswith('registry '))
            self.assertEqual(state(), 'missing')
            # Link a throwaway copy: uninstall below must never be able to touch the real tree.
            copy = Path(temp)/'plugin'
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns('.git', '__pycache__', 'tests'))
            subprocess.run(['herdr', 'plugin', 'link', str(copy)], env=env, check=True, capture_output=True, timeout=20)
            self.assertEqual(state(), 'registered')
            # herdr plugin uninstall without a running server edits the registry directly.
            subprocess.run(['herdr', 'plugin', 'uninstall', 'herdr-spring-load'], env=env, check=True, capture_output=True, timeout=20)
            self.assertEqual(state(), 'missing')

    def test_helper_compiles_and_validates_without_starting(self):
        binary = self.compile(ROOT/'helper/main.swift', 'helper')
        env = dict(os.environ, HOME=str(self.build))
        result = subprocess.run([str(binary), '--validate'], env=env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.build/'Library').exists(), 'validation must not start the watcher or touch setup')
        result = subprocess.run([str(binary), '--tab-dwell-ms', '5', '--validate'], env=env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)
