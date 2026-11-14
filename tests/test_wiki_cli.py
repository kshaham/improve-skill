"""`improve_graph.py wiki`: read the knowledge wiki cheaply, and rebuild it only after a session."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from graph_support import ROOT, graph, make_repo

import improve_toon

SCRIPT = ROOT / "scripts" / "improve_graph.py"


def cli(repo, *args):
    return subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), *args],
                          capture_output=True, text=True, timeout=60)


def run_file(repo, outcome):
    (Path(repo) / ".improve" / "run.json").write_text(json.dumps({"run_id": "run-1", "outcome": outcome}))


class WikiCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(self.tmp.name, ["src/app.py"])
        with graph.locked(self.repo) as value:
            graph.add_insight(value, "Retry the upload twice before reporting a network failure",
                              kind="technique", about=["file:src/app.py"], repo=self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_wiki_yet_says_when_it_is_built(self):
        result = cli(self.repo, "wiki")
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn("built when an improve session finishes", result.stderr)

    def test_build_refuses_during_a_live_run(self):
        run_file(self.repo, None)
        result = cli(self.repo, "wiki", "build")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("a run is still live", result.stderr)
        self.assertFalse((self.repo / ".improve" / "wiki").exists())

    def test_pages_read_as_toon_json_and_markdown_after_a_session(self):
        run_file(self.repo, "completed")
        self.assertEqual(cli(self.repo, "wiki", "build").returncode, 0)
        toon = cli(self.repo, "wiki", "knowledge")
        self.assertEqual(toon.returncode, 0, toon.stderr)
        page = improve_toon.loads(toon.stdout)
        self.assertEqual(page["groups"][0]["notes"][0]["text"],
                         "Retry the upload twice before reporting a network failure")
        as_json = json.loads(cli(self.repo, "wiki", "knowledge", "--format", "json").stdout)
        self.assertEqual(as_json, page)
        self.assertLess(len(toon.stdout), len(json.dumps(as_json)), "TOON is the cheaper default")
        markdown = cli(self.repo, "wiki", "index", "--markdown")
        self.assertTrue(markdown.stdout.startswith("# Knowledge wiki"), markdown.stdout[:80])
        listing = cli(self.repo, "wiki", "pages").stdout
        self.assertIn("knowledge", listing)
        self.assertIn("changes", listing)

    def test_an_unknown_page_is_not_read_from_disk(self):
        run_file(self.repo, "completed")
        cli(self.repo, "wiki", "build")
        for name in ("../run", "missing", "areas/../../run"):
            result = cli(self.repo, "wiki", name, "--markdown")
            self.assertEqual(result.returncode, 5, name)
            self.assertNotIn("run-1", result.stdout)


if __name__ == "__main__":
    unittest.main()
