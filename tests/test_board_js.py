"""Board JavaScript checks that need only Node, not a browser: every board script parses, and
the drafts store survives cancelled Retry/Withdraw forms and bad rows. Skipped without Node."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from support import budget

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets" / "board"
HARNESS = Path(__file__).resolve().parent / "js"


def find_node():
    """Node on PATH, or the Homebrew location used on the development Macs."""
    for candidate in (os.environ.get("IMPROVE_TEST_NODE"), shutil.which("node"), "/opt/homebrew/bin/node",
                      "/usr/local/bin/node"):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


NODE = find_node()


@unittest.skipUnless(NODE, "node is not installed; board JavaScript checks skipped")
class BoardScriptTests(unittest.TestCase):
    def node(self, *args):
        return subprocess.run([NODE, *map(str, args)], capture_output=True, text=True, timeout=budget(30))

    def test_every_board_script_parses(self):
        scripts = sorted(ASSETS.glob("*.js"))
        self.assertGreater(len(scripts), 10)
        for script in scripts:
            with self.subTest(script=script.name):
                result = self.node("--check", script)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_drafts_survive_cancelled_forms_and_bad_rows(self):
        result = self.node(HARNESS / "drafts_check.mjs", ASSETS / "drafts.js")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["checks"]), 5)


if __name__ == "__main__":
    unittest.main()
