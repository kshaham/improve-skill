"""A foreground session's end, as SKILL.md describes it, builds the knowledge wiki.

At clock exit 10 the agent writes the final report, records the outcome with
`improve_ledger.py finish`, and runs `improve_graph.py sync --dream`. Before this, the only way
to set the outcome was a raw edit of run.json (`run-set` refuses it), so a run that followed
the docs never built the wiki.
"""
import json
import subprocess
import sys
import tempfile
import unittest

from graph_support import ROOT, graph, make_repo

import improve_wiki

LEDGER = ROOT / "scripts" / "improve_ledger.py"
GRAPH = ROOT / "scripts" / "improve_graph.py"


def run(script, repo, *args):
    return subprocess.run([sys.executable, str(script), "--repo", str(repo), *args],
                          capture_output=True, text=True, timeout=60)


class ForegroundFinishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(self.tmp.name, ["src/app.py"])
        self.state = self.repo / ".improve"
        with graph.locked(self.repo) as value:
            graph.add_insight(value, "Retry the upload twice before reporting a network failure",
                              about=["file:src/app.py"], repo=self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def write_run(self, deadline, **extra):
        (self.state / "run.json").write_text(json.dumps({
            "started_at": "2026-11-14T00:00:00Z", "deadline": deadline, "outcome": None, **extra}))

    def test_the_documented_steps_build_the_wiki(self):
        self.write_run("2000-01-01T00:00:00Z")
        finished = run(LEDGER, self.repo, "finish", "--outcome", "completed")
        self.assertEqual(finished.returncode, 0, finished.stderr)
        state = json.loads((self.state / "run.json").read_text())
        self.assertEqual(state["outcome"], "completed")
        self.assertTrue(state["ended_at"])
        dreamed = run(GRAPH, self.repo, "sync", "--dream")
        self.assertEqual(dreamed.returncode, 0, dreamed.stderr)
        self.assertTrue((self.state / "wiki" / "index.md").is_file())
        # The board shows when the run ended (no supervisor: run.json says).
        import improve_board
        self.assertEqual(improve_board.board_snapshot(self.repo)["current"]["ended_at"], state["ended_at"])

    def test_completed_is_refused_before_the_deadline(self):
        self.write_run("2999-01-01T00:00:00Z")
        result = run(LEDGER, self.repo, "finish", "--outcome", "completed")
        self.assertEqual(result.returncode, 2)
        self.assertIn("deadline", result.stderr)
        self.assertIsNone(json.loads((self.state / "run.json").read_text())["outcome"])

    def test_a_stop_or_halt_may_end_early(self):
        self.write_run("2999-01-01T00:00:00Z")
        self.assertEqual(run(LEDGER, self.repo, "finish", "--outcome", "stopped by user").returncode, 0)
        self.write_run("2999-01-01T00:00:00Z")
        self.assertEqual(run(LEDGER, self.repo, "finish", "--outcome", "halted: gates unavailable").returncode, 0)
        self.write_run("2999-01-01T00:00:00Z")
        self.assertEqual(run(LEDGER, self.repo, "finish", "--outcome", "halted: ").returncode, 2, "a halt needs a reason")
        self.assertEqual(run(LEDGER, self.repo, "finish", "--outcome", "done-ish").returncode, 2)

    def test_the_supervisor_records_its_own_outcome(self):
        self.write_run("2000-01-01T00:00:00Z")
        (self.state / "supervisor.json").write_text(json.dumps({"run_id": "daemon", "outcome": None}))
        result = run(LEDGER, self.repo, "finish", "--outcome", "completed")
        self.assertEqual(result.returncode, 2)
        self.assertIn("supervisor", result.stderr)

    def test_a_finished_run_is_not_finished_again(self):
        self.write_run("2000-01-01T00:00:00Z", outcome="halted: earlier")
        result = run(LEDGER, self.repo, "finish", "--outcome", "completed")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads((self.state / "run.json").read_text())["outcome"], "halted: earlier")


class DocumentedStepsTests(unittest.TestCase):
    def test_every_description_of_the_session_end_records_the_outcome_before_dreaming(self):
        # An agent following any one of these must reach the wiki build; a page that says only
        # `sync --dream` leaves the outcome unset and the wiki skipped.
        for name in ("SKILL.md", "references/report-format.md", "references/knowledge-graph.md", "references/ledger.md"):
            text = (ROOT / name).read_text()
            self.assertIn("finish --outcome completed", text, name)
            self.assertLess(text.index("finish --outcome completed"), text.rindex("sync --dream"), name)
        skill = (ROOT / "SKILL.md").read_text()
        self.assertIn("python3 <skill-directory>/scripts/improve_ledger.py --repo <repo> finish --outcome completed", skill)
        self.assertIn("python3 <skill-directory>/scripts/improve_graph.py --repo <repo> sync --dream", skill)

class NoSessionTests(unittest.TestCase):
    def test_with_no_run_at_all_no_session_has_finished(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp, ["src/app.py"])
            run_id, finished = improve_wiki.run_state(repo / ".improve")
            self.assertFalse(finished)
            result = graph.sync(repo, dream_after=True)
            self.assertIn("no improve session", result["wiki"]["skipped"])
            self.assertFalse((repo / ".improve" / "wiki").exists())


if __name__ == "__main__":
    unittest.main()
