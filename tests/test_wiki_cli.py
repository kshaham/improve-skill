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
        self.assertNotIn("more", as_json, "the area lookup table is internal to the wiki document")
        self.assertEqual(as_json, page)
        self.assertLess(len(toon.stdout), len(json.dumps(as_json)), "TOON is the cheaper default")
        markdown = cli(self.repo, "wiki", "index", "--markdown")
        self.assertTrue(markdown.stdout.startswith("# Knowledge wiki"), markdown.stdout[:80])
        listing = cli(self.repo, "wiki", "pages").stdout
        self.assertIn("knowledge", listing)
        self.assertIn("changes", listing)

    def test_the_documented_foreground_finish_builds_the_wiki(self):
        # SKILL.md: a foreground run finishes with `sync --dream` after its final report.
        run_file(self.repo, None)
        self.assertEqual(cli(self.repo, "sync", "--dream").returncode, 0)
        self.assertFalse((self.repo / ".improve" / "wiki").exists(), "mid-run: no wiki")
        run_file(self.repo, "completed")
        result = cli(self.repo, "sync", "--dream")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.repo / ".improve" / "wiki" / "index.md").is_file())
        self.assertIn("wiki", result.stdout)

    def test_a_budget_keeps_the_strongest_notes_across_kinds(self):
        with graph.locked(self.repo) as value:
            for index in range(60):
                # Weak notes in the first kind on the page; the strong one in the last kind, so
                # only a ranking across kinds keeps it.
                graph.add_insight(value, f"Weak note number {index} about an unrelated corner of the app",
                                  kind="decision", about=["file:src/app.py"], repo=self.repo)
            strong = graph.add_insight(value, "Pinned fact: the board never stores tokens in the page",
                                       kind="fact", source="user", about=["file:src/app.py"], repo=self.repo)
            strong["pinned"] = True
        run_file(self.repo, "completed")
        self.assertEqual(cli(self.repo, "wiki", "build").returncode, 0)
        full = cli(self.repo, "wiki", "knowledge").stdout
        small = cli(self.repo, "wiki", "knowledge", "--budget", "300")
        self.assertEqual(small.returncode, 0, small.stderr)
        self.assertLess(len(small.stdout), len(full) / 3)
        self.assertLessEqual(len(small.stdout), 300 * 4 + 200, "fits the budget (about 4 characters a token)")
        self.assertIn("Pinned fact", small.stdout, "the strongest note survives the cut")
        self.assertIn("TRUNCATED", small.stdout, "says what was cut, as other budgeted answers do")
        # The totals survive the flattening, so a capped page still says how much it holds.
        roomy = json.loads(cli(self.repo, "wiki", "knowledge", "--budget", "100000", "--format", "json").stdout)
        self.assertEqual(roomy["totals"], {"decision": 60, "technique": 1, "fact": 1})
        self.assertEqual(roomy["total"], 62)

    def test_a_budget_says_when_the_wiki_itself_capped_the_page(self):
        import improve_wiki
        extra = improve_wiki.NOTES_PER_PAGE + 5
        with graph.locked(self.repo) as value:
            for index in range(extra):
                graph.add_insight(value, f"Capped fact number {index} about a corner of the app",
                                  kind="fact", about=["file:src/app.py"], repo=self.repo)
        run_file(self.repo, "completed")
        self.assertEqual(cli(self.repo, "wiki", "build").returncode, 0)
        result = cli(self.repo, "wiki", "knowledge", "--budget", "1000000")
        self.assertEqual(result.returncode, 0, result.stderr)
        first = result.stdout.splitlines()[0]
        self.assertIn(f"lists {improve_wiki.NOTES_PER_PAGE + 1} of {extra + 1} notes", first)
        self.assertIn("search", first)

    def test_every_page_answers_with_a_budget(self):
        with graph.locked(self.repo) as value:
            graph.add_insight(value, "Area pages carry their own note count, not a list",
                              kind="pitfall", about=["area:ui"], repo=self.repo)
        run_file(self.repo, "completed")
        self.assertEqual(cli(self.repo, "wiki", "build").returncode, 0)
        pages = [row["page"] for row in json.loads(cli(self.repo, "wiki", "pages", "--format", "json").stdout)]
        self.assertIn("knowledge", pages)
        self.assertIn("areas/ui", pages, "an area page is part of the check")
        for page in pages:
            result = cli(self.repo, "wiki", page, "--budget", "200")
            self.assertEqual(result.returncode, 0, f"{page}: {result.stderr}")

    def test_area_pages_carry_their_notes_for_an_agent(self):
        with graph.locked(self.repo) as value:
            graph.add_insight(value, "Area pages list their notes for agents in one call",
                              kind="pitfall", about=["area:ui"], repo=self.repo)
        run_file(self.repo, "completed")
        cli(self.repo, "wiki", "build")
        page = json.loads(cli(self.repo, "wiki", "areas/ui", "--format", "json").stdout)
        self.assertEqual(page["knowledge"][0]["text"], "Area pages list their notes for agents in one call")
        self.assertEqual(page["knowledge"][0]["kind"], "pitfall")
        stored = improve_toon.loads((self.repo / ".improve" / "wiki" / "wiki.toon").read_text())
        self.assertTrue(all(isinstance(key, str) for key in stored["pages"]["areas/ui"]["knowledge"]),
                        "the stored document keeps ids only")

    def test_an_unknown_page_is_not_read_from_disk(self):
        run_file(self.repo, "completed")
        cli(self.repo, "wiki", "build")
        for name in ("../run", "missing", "areas/../../run"):
            result = cli(self.repo, "wiki", name, "--markdown")
            self.assertEqual(result.returncode, 5, name)
            self.assertNotIn("run-1", result.stdout)


if __name__ == "__main__":
    unittest.main()
