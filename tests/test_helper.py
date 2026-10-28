import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(ROOT, "helper")


@unittest.skipUnless(shutil.which("swiftc"), "needs swiftc")
class HelperTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir)

    def build(self, sources, name):
        out = os.path.join(self.dir, name)
        result = subprocess.run(
            ["swiftc", *sources, "-o", out], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return out

    def test_drop_logic(self):
        # Drop confirmation (finding 1) and command deadlines (finding 4).
        binary = self.build(
            [os.path.join(HELPER, "DropLogic.swift"), os.path.join(ROOT, "tests", "swift", "main.swift")],
            "droplogic-tests",
        )
        result = subprocess.run([binary], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stdout)

    def test_helper_builds_and_stops_on_sigterm(self):
        binary = self.build(
            [os.path.join(HELPER, "DropLogic.swift"), os.path.join(HELPER, "main.swift")],
            "image-drop-watch",
        )
        env = dict(os.environ, HOME=self.dir)
        os.makedirs(os.path.join(self.dir, "Library", "Logs"), exist_ok=True)
        process = subprocess.Popen([binary, "--dry-run"], env=env)
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                process.wait(timeout=1)
            process.terminate()
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            if process.poll() is None:
                process.kill()


if __name__ == "__main__":
    unittest.main()
