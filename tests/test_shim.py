"""The .cmd shim must not swallow failure exit codes.

``endlocal`` resets ERRORLEVEL, so a shim that ends with ``endlocal`` alone
reports success for every failure. Every refusal this bridge makes would then
look like success to anything reading the exit code -- which, since ``cli.main``
collapses all BridgeErrors to 1, is the only machine-readable signal there is.
"""

import os
import subprocess
import sys
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SHIM = os.path.join(REPO, "bin", "rdb.cmd")


@unittest.skipUnless(os.name == "nt", "the shim is a Windows batch file")
class Shim(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run([SHIM, *args], capture_output=True, text=True,
                              cwd=REPO, shell=False)

    def test_shim_exists_and_preserves_errorlevel(self):
        self.assertTrue(os.path.isfile(SHIM))
        with open(SHIM, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("endlocal & exit /b", text,
                      "the shim must re-raise the exit code after endlocal")

    def test_usage_error_exits_2(self):
        # argparse fails before any config is loaded, so this needs nothing
        # installed to be a meaningful test.
        self.assertEqual(self._run("no-such-command").returncode, 2)

    def test_help_exits_0(self):
        self.assertEqual(self._run("--help").returncode, 0)


@unittest.skipUnless(os.name == "nt", "Windows only")
class ModuleEntryPoint(unittest.TestCase):
    def test_module_runs_without_the_shim(self):
        env = dict(os.environ, PYTHONPATH=os.path.join(REPO, "src"))
        result = subprocess.run([sys.executable, "-m", "rdbridge", "--help"],
                                capture_output=True, text=True, cwd=REPO, env=env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Never handles a", result.stdout)


if __name__ == "__main__":
    unittest.main()
