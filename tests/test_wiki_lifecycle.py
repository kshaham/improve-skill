"""The knowledge wiki across two real sessions, through the graph's own operations.

Session one records knowledge and ends (finish, then sync --dream). Session two rewords a note
as the user (the graph merges the agent's note into it) and records two notes that say
opposite things (dreaming infers a contradiction), then ends. The wiki must tell that story:
what grew, what was merged into what, and what dreaming found.
"""
import json
import subprocess
import sys
import tempfile
import unittest

from graph_support import ROOT, graph, make_repo

GRAPH = ROOT / "scripts" / "improve_graph.py"
LEDGER = ROOT / "scripts" / "improve_ledger.py"


class TwoSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(self.tmp.name, ["src/app.py", "src/cache.py"])
        self.state = self.repo / ".improve"

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, script, *args):
        result = subprocess.run([sys.executable, str(script), "--repo", str(self.repo), *args],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, f"{args}: {result.stderr}")
        return result.stdout

    def start(self, run_id):
        (self.state / "run.json").write_text(json.dumps({
            "started_at": run_id, "run_id": run_id, "deadline": "2000-01-01T00:00:00Z", "outcome": None}))

    def end(self):
        self.cli(LEDGER, "finish", "--outcome", "completed")
        self.cli(GRAPH, "sync", "--dream")

    def test_the_wiki_grows_cleans_up_and_dreams_across_sessions(self):
        self.start("2026-11-14T01:00:00Z")
        ids = []
        for text in ("Retry the upload twice before reporting a network failure",
                     "Cache the board snapshot for five seconds between polls"):
            out = self.cli(GRAPH, "note", "--kind", "technique", "--text", text, "--about", "file:src/app.py")
            ids.append(next(line.split(":", 1)[1].strip().strip('"') for line in out.splitlines() if line.startswith("id:")))
        self.end()
        wiki = self.state / "wiki"
        self.assertIn("2 live notes", (wiki / "index.md").read_text())

        self.start("2026-11-14T02:00:00Z")
        # Successful tasks that relied on the retry note prove it (three or more).
        rows = [{"id": f"t{i}", "title": f"Upload fix {i}", "area": "resilience", "status": "done",
                 "file": "src/app.py", "graph_refs": [ids[0]], "commit": f"abc{i}"} for i in range(4)]
        (self.state / "backlog.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        with graph.locked(self.repo) as value:
            graph.add_insight(value, "Cache the board snapshot for five seconds between board polls",
                              kind="technique", source="user", about=["file:src/app.py"], repo=self.repo)
            for word in ("Always", "Never"):
                graph.add_insight(value, f"{word} clear the cache before the integration tests",
                                  kind="pitfall", about=["file:src/cache.py"], repo=self.repo)
        self.end()

        changes = (wiki / "changes.md").read_text()
        latest = changes[changes.index("2026-11-14T02:00:00Z"):changes.index("2026-11-14T01:00:00Z")]
        self.assertIn("3 added, 1 removed, 1 verdict changed", latest)
        self.assertIn("Verdict none → proven: Retry the upload twice before reporting a network failure", latest)
        self.assertIn('Removed (merged into "Cache the board snapshot for five seconds between board polls")', latest)
        dreams = (wiki / "dreams.md").read_text()
        contradictions = dreams[dreams.index("## Contradictions"):dreams.index("## Knowledge gaps")]
        self.assertIn("Always clear the cache before the integration tests", contradictions)
        self.assertIn("Never clear the cache before the integration tests", contradictions)
        sessions = (wiki / "sessions.md").read_text()
        self.assertLess(sessions.index("2026-11-14T02:00:00Z"), sessions.index("2026-11-14T01:00:00Z"))
        manifest = json.loads((wiki / "manifest.json").read_text())
        self.assertEqual([row["run_id"] for row in manifest["sessions"]],
                         ["2026-11-14T01:00:00Z", "2026-11-14T02:00:00Z"])


if __name__ == "__main__":
    unittest.main()
