"""The ledger CLI: ranked queue, filters, bounded views, canonical writes, and formats."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import support  # noqa: E402,F401
import improve_ledger as ledger  # noqa: E402
import improve_toon as toon  # noqa: E402

LONG = "the lapse sweep swallows the repository error and reports success to the caller " * 3


class LedgerCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve ledger ")
        self.repo = Path(self.temp.name)
        self.state = self.repo / ".improve"
        self.state.mkdir()
        self.rows = [
            dict(id="q-1", area="quality", severity=3, confidence=0.9, blast=2, file="src/a.py", line=10, title="A", claim=LONG, status="ready"),
            dict(id="q-2", area="security", severity=5, confidence=0.8, blast=4, file="src/b.py", title="B", claim="b", status="ready", user_priority="low"),
            dict(id="q-3", area="ui", severity=1, confidence=0.5, blast=1, paths=["ui/x.ts", "ui/y.ts"], title="C", claim="c", status="ready",
                 user_priority="high", focus_priority=2),
            dict(id="q-4", area="ui", severity=4, confidence=0.9, blast=4, title="D", claim="d", status="ready", user_priority="high", focus_priority=1),
            dict(id="q-5", area="docs", title="E", claim="e", status="done", commit="abc1234", note="verified"),
            dict(id="q-6", area="ui", title="F", claim="f", status="blocked", blocked_reason="simulator down", note="original note"),
        ]
        self.write(self.rows)
        (self.state / "discovery.jsonl").write_text("".join(json.dumps(dict(generation=index, lane="ui", status="complete", accepted=0,
                                                                              hypothesis="h" * 150, at=f"2026-11-08T0{index}:00:00Z")) + "\n"
                                                           for index in range(1, 4)))
        (self.state / "run.json").write_text(json.dumps(dict(phase="discover", cycle=7, next_action="Inspect cart", deadline="2026-11-09T00:00:00Z",
                                                             started_at="2026-11-08T00:00:00Z", scanned_paths=["a", "b", "c"],
                                                             focus=dict(priorities=["ui"], excluded=[], source="intake.json"), gates={"go": "go test ./..."})))

    def tearDown(self):
        self.temp.cleanup()

    def write(self, rows):
        (self.state / "backlog.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))

    def run_cli(self, *args, env=None):
        import os
        return subprocess.run([sys.executable, str(ROOT / "scripts/improve_ledger.py"), "--repo", str(self.repo), *args],
                              capture_output=True, text=True, timeout=30, env={**os.environ, **(env or {})})

    def decode(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return toon.loads(result.stdout)

    def test_queue_ranks_like_the_skill_and_bounds_text(self):
        view = self.decode(self.run_cli("queue"))
        self.assertEqual([row["id"] for row in view["tasks"]], ["q-4", "q-3", "q-1", "q-2"], "high priority first, then focus, then severity product; low last")
        self.assertEqual((view["total"], view["shown"]), (4, 4))
        self.assertTrue(view["tasks"][2]["claim"].endswith("..."))
        self.assertLessEqual(len(view["tasks"][2]["claim"]), ledger.DEFAULT_WIDTH)
        self.assertNotIn("evidence", view["tasks"][0], "compact columns only")
        self.assertTrue(self.run_cli("queue").stdout.startswith("total: 4\nshown: 4\ntasks[4]{"), "a TOON table with one header")
        full = self.decode(self.run_cli("--full", "queue", "--limit", "1"))
        self.assertEqual(full["tasks"][0]["claim"], self.rows[3]["claim"])
        self.assertEqual(full["shown"], 1)

    def test_filters_and_fields(self):
        blocked = self.decode(self.run_cli("tasks", "--status", "blocked"))
        self.assertEqual([row["id"] for row in blocked["tasks"]], ["q-6"])
        self.assertEqual(blocked["tasks"][0]["blocked_reason"], "simulator down")
        by_dir = self.decode(self.run_cli("tasks", "--file", "ui"))
        self.assertEqual([row["id"] for row in by_dir["tasks"]], ["q-3"], "paths under a directory match")
        by_file = self.decode(self.run_cli("tasks", "--file", "src/b.py", "--fields", "id,severity"))
        self.assertEqual(by_file["tasks"], [{"id": "q-2", "severity": 5}])
        prefix = self.decode(self.run_cli("tasks", "--id", "q-", "--area", "ui", "--status", "ready", "--status", "blocked"))
        self.assertEqual([row["id"] for row in prefix["tasks"]], ["q-4", "q-3", "q-6"])

    def test_show_counts_discovery_run_and_json_mode(self):
        shown = self.decode(self.run_cli("show", "q-1"))
        self.assertEqual(shown["claim"], LONG, "show never truncates")
        two = self.decode(self.run_cli("show", "q-1", "q-5"))
        self.assertEqual([row["id"] for row in two], ["q-1", "q-5"])
        self.assertEqual(self.run_cli("show", "nope").returncode, 2)
        counts = self.decode(self.run_cli("counts"))
        self.assertEqual(counts["totals"]["ready"], 4)
        self.assertEqual({row["area"]: row["total"] for row in counts["areas"]}, {"docs": 1, "quality": 1, "security": 1, "ui": 3})
        passes = self.decode(self.run_cli("discovery", "--limit", "2"))
        self.assertEqual([row["generation"] for row in passes["passes"]], [2, 3])
        self.assertEqual(passes["total"], 3)
        run = self.decode(self.run_cli("run"))
        self.assertEqual((run["cycle"], run["scanned_paths_count"], run["focus"]["priorities"]), (7, 3, ["ui"]))
        self.assertNotIn("gates", run, "bulky fields stay out of the view")
        as_json = self.run_cli("--format", "json", "counts")
        self.assertEqual(json.loads(as_json.stdout)["totals"]["done"], 1)
        via_env = self.run_cli("counts", env={"IMPROVE_FORMAT": "json"})
        self.assertEqual(json.loads(via_env.stdout)["totals"]["blocked"], 1)
        piped = self.run_cli("--delimiter", "pipe", "queue", "--limit", "1")
        self.assertIn("tasks[1|]{", piped.stdout)

    def test_set_rewrites_canonically_and_validates(self):
        # A duplicate append-only row, as older writers and the supervisor once produced.
        (self.state / "backlog.jsonl").open("a").write(json.dumps(dict(id="q-1", status="in_progress")) + "\n")
        result = self.decode(self.run_cli("set", "q-1", "--status", "done", "--commit", "abc1234", "--note", "gate green",
                                          "--graph-ref", "insight:aaa", "--graph-ref", "insight:aaa", "--json-field", "line=42",
                                          "--field", "verification=go test ./... (12 passed)"))
        self.assertEqual((result["status"], result["commit"], result["line"], result["graph_refs"]), ("done", "abc1234", 42, ["insight:aaa"]))
        self.assertTrue(result["completed_at"])
        rows = [json.loads(line) for line in (self.state / "backlog.jsonl").read_text().splitlines()]
        self.assertEqual([row["id"] for row in rows], ["q-1", "q-2", "q-3", "q-4", "q-5", "q-6"], "one row per id, original order kept")
        self.assertEqual(rows[0]["claim"], LONG, "unchanged fields survive")
        self.assertEqual(rows[0]["verification"], "go test ./... (12 passed)")
        self.assertEqual(rows[5]["note"], "original note", "other rows are untouched")
        for args in (("set", "q-1", "--status", "finished"), ("set", "q-1", "--commit", "not-a-sha"), ("set", "q-1"),
                     ("set", "missing", "--status", "done"), ("set", "q-1", "--json-field", "severity=9"),
                     ("set", "q-1", "--field", "bad key=1")):
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 2, args)
            self.assertTrue("improve-ledger:" in result.stderr or "error:" in result.stderr, result.stderr)
        started = self.decode(self.run_cli("set", "q-2", "--status", "in_progress"))
        self.assertTrue(started["started_at"])
        self.assertEqual(len(list(self.state.glob(".backlog.jsonl.*"))), 0, "no temporary files are left behind")

    def test_add_appends_a_validated_row(self):
        added = self.decode(self.run_cli("add", "--json", json.dumps(dict(id="q-7", area="quality", title="G", claim="g", severity=2, confidence=0.7, blast=1))))
        self.assertEqual((added["status"], bool(added["created_at"])), ("ready", True))
        self.assertEqual(len(ledger.merged_tasks(self.state)), 7)
        for payload in ('{"id":"q-7","title":"dup"}', '{"id":"bad id!"}', '[1]', '{"id":"q-8","status":"later"}', "not json"):
            result = self.run_cli("add", "--json", payload)
            self.assertEqual(result.returncode, 2, payload)

    def test_damaged_lines_do_not_break_views_or_writes(self):
        (self.state / "backlog.jsonl").open("a").write("{broken\n")
        view = self.decode(self.run_cli("counts"))
        self.assertEqual(view["totals"]["total"], 6)
        self.decode(self.run_cli("set", "q-5", "--note", "still fine"))
        self.assertNotIn("{broken", (self.state / "backlog.jsonl").read_text(), "a canonical rewrite drops the unreadable line")

    def test_missing_state_is_a_clear_error(self):
        empty = Path(self.temp.name) / "other"
        empty.mkdir()
        result = subprocess.run([sys.executable, str(ROOT / "scripts/improve_ledger.py"), "--repo", str(empty), "queue"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, "no ledger yet is simply an empty queue")
        self.assertEqual(toon.loads(result.stdout)["total"], 0)


if __name__ == "__main__":
    unittest.main()
