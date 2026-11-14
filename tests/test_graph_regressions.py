"""Regressions from the 2026-11-12 full-codebase review, stream G: queued and replayed writes
are never lost, paths cannot leave the repository, budgets stay linear, odd input never
breaks an answer, and the maintenance passes stay correct at their bounds."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_support import (ROOT, budget, environment, load, make_repo, restore_environment, write_ledger,  # noqa: E402
                           write_run)
import improve_dream as dreaming  # noqa: E402
import improve_graph as graph  # noqa: E402
import improve_graph_cli as graph_cli  # noqa: E402
import improve_graph_journal as journal  # noqa: E402
import improve_toon as toon  # noqa: E402


class RegressionCase(unittest.TestCase):
    files = ["src/a.py", "src/b.py"]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve graph regressions ")
        self.previous = environment(Path(self.temp.name) / "home")
        self.repo = make_repo(Path(self.temp.name) / "repo", self.files).resolve()
        self.state = self.repo / ".improve"
        write_run(self.state, "live", {"test": 3})
        write_ledger(self.state, [{"id": "t1", "status": "done", "area": "quality", "title": "x", "file": "src/a.py"}])
        graph.sync(self.repo)

    def tearDown(self):
        restore_environment(self.previous)
        self.temp.cleanup()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = graph_cli.main(["--repo", str(self.repo), "--format", "json", *argv])
        return code, out.getvalue(), err.getvalue()

    def summaries(self):
        return [node.get("summary", "") for node in load(self.repo)["nodes"].values() if node["type"] == "insight"]


class PendingQueueTests(RegressionCase):
    def test_a_note_queued_during_a_draining_session_is_kept(self):
        with mock.patch.dict(os.environ, {"IMPROVE_GRAPH_LOCK_WAIT": "0.2"}):
            with graph.locked(self.repo):
                code, out, _ = self.cli("note", "--text", "first queued note about retries in a")
                self.assertTrue(json.loads(out)["queued"])
            with graph.locked(self.repo):  # Drains the first note; a second agent queues meanwhile.
                code, out, _ = self.cli("note", "--text", "second queued note about timeouts in a")
                self.assertEqual(code, 0)
                self.assertTrue(json.loads(out)["queued"])
        graph.sync(self.repo)
        notes = self.summaries()
        self.assertTrue(any(text.startswith("first queued") for text in notes))
        self.assertTrue(any(text.startswith("second queued") for text in notes), "the second note survives")

    def test_a_drain_that_never_commits_is_retried(self):
        journal.queue_pending(self.repo, "note", {"text": "queued before a failing writer", "source": "agent"})
        with self.assertRaises(RuntimeError), graph.locked(self.repo):
            raise RuntimeError("the writer's own work fails")
        self.assertTrue(list(self.state.glob("graph-pending.jsonl.*.draining")), "kept for the next writer")
        graph.sync(self.repo)
        self.assertEqual(sum(text == "queued before a failing writer" for text in self.summaries()), 1)
        self.assertEqual(list(self.state.glob("graph-pending.jsonl.*.draining")), [])

    def test_a_crash_after_commit_does_not_apply_twice(self):
        journal.queue_pending(self.repo, "note", {"text": "applied exactly once even after a crash", "source": "agent"})
        with mock.patch.object(journal, "finish_drains", lambda graph_state: None):  # Crash before cleanup.
            graph.sync(self.repo)
        graph.sync(self.repo)
        nodes = [node for node in load(self.repo)["nodes"].values()
                 if node.get("summary") == "applied exactly once even after a crash"]
        self.assertEqual([node["reinforced"] for node in nodes], [1])

    def test_concurrent_appenders_lose_nothing(self):
        def queue(index):
            journal.queue_pending(self.repo, "note", {"text": f"concurrent queued note number {index:03d}", "source": "agent"})

        threads = [threading.Thread(target=queue, args=(index,)) for index in range(30)]
        for thread in threads[:15]:
            thread.start()
        drainer = threading.Thread(target=graph.sync, args=(self.repo,))
        drainer.start()
        for thread in threads[15:]:
            thread.start()
        for thread in threads + [drainer]:
            thread.join()
        graph.sync(self.repo)
        self.assertEqual(sum(text.startswith("concurrent queued note") for text in self.summaries()), 30)


class ReadsJournalTests(RegressionCase):
    def test_reads_appended_during_a_drain_count_and_the_hint_still_sees_them(self):
        node = None
        with graph.locked(self.repo) as value:
            node = graph.add_insight(value, "A note the agent reads during the sync", "fact")["id"]
        journal.record_reads(self.repo, [node], ["src/a.py"])
        with graph.locked(self.repo) as value:
            self.assertEqual(journal.drain_reads(value, self.repo, graph.touch), 1)
            journal.record_reads(self.repo, [node], ["src/b.py"])  # Appended while the drain is open.
        graph.sync(self.repo)
        self.assertEqual(load(self.repo)["nodes"][node]["retrieved"], 2)
        self.assertEqual(journal.files_read_since(self.repo), {"src/a.py", "src/b.py"})


class RecoveryTests(RegressionCase):
    def corrupt(self):
        db = graph.graph_path(self.repo)
        for suffix in ("-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)
        db.write_bytes(b"garbage" * 1000)

    def test_replay_survives_a_failing_first_writer_and_sequences_stay_unique(self):
        graph.sync(self.repo, refine_after=True)  # Checkpoint.
        self.assertEqual(self.cli("note", "--text", "curated note after checkpoint about src a")[0], 0)
        self.corrupt()
        code, _, err = self.cli("rate", "insight:doesnotexist", "useful")
        self.assertEqual(code, 2, err)
        self.assertFalse((self.state / journal.RECOVERING).exists(), "the replay committed before the failure")
        self.assertIn("curated note after checkpoint about src a", self.summaries())
        self.assertEqual(load(self.repo)["refine"]["recovered"]["replayed"], 1)
        self.assertEqual(self.cli("note", "--text", "another fresh note written later about src a")[0], 0)
        seqs = [json.loads(line)["seq"] for line in (self.state / journal.WRITES).read_text().splitlines()]
        self.assertEqual(len(seqs), len(set(seqs)))

    def test_a_replay_that_fails_is_retried_by_the_next_writer(self):
        graph.sync(self.repo, refine_after=True)
        self.cli("note", "--text", "curated note that must come back eventually")
        self.corrupt()
        with mock.patch.object(journal, "replay", side_effect=OSError("disk hiccup")):
            with self.assertRaises(OSError):
                graph.sync(self.repo)
        self.assertTrue((self.state / journal.RECOVERING).exists())
        graph.sync(self.repo)
        self.assertIn("curated note that must come back eventually", self.summaries())
        self.assertFalse((self.state / journal.RECOVERING).exists())


class PathSafetyTests(RegressionCase):
    def test_no_spelling_leaves_the_repository(self):
        for value in ("src\\..\\..\\outside.txt", "a/../../x", "../x", "..", "/etc/passwd", "C:/x", "src/\x1b[31m.py",
                      "src/../../x", "./../x"):
            with self.subTest(value=value):
                self.assertIsNone(graph.safe_path(value, self.repo))
        self.assertEqual(graph.safe_path("src\\a.py"), "src/a.py")
        self.assertEqual(graph.safe_path(str(self.repo / "src/a.py"), self.repo), "src/a.py")

    def test_a_note_cannot_fingerprint_a_file_outside(self):
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("secret")
        code, _, err = self.cli("note", "--text", "a note about a file outside the repository",
                                "--about", "file:src\\..\\..\\outside.txt")
        self.assertEqual(code, 2, "an unusable path is an error, not a node")
        self.assertFalse(any(node_id.startswith("file:..") for node_id in load(self.repo)["nodes"]))

    def test_fingerprints_refuse_symlinks_leaving_the_repository(self):
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("secret")
        (self.repo / "src/link.py").symlink_to(outside)
        self.assertIsNone(graph.file_fingerprint(self.repo, "src/link.py"))


class BudgetAndOutputTests(RegressionCase):
    def test_budget_is_linear(self):
        rows = [{"id": f"insight:{index:012x}", "type": "insight", "summary": "word " * 20, "score": 0.5}
                for index in range(1000)]
        started = time.monotonic()
        text = graph_cli.budgeted(rows, 100, graph_cli.render)
        self.assertLess(time.monotonic() - started, budget(2.0))
        header, _, body = text.partition("\n")
        self.assertTrue(header.startswith("[!] TRUNCATED: showing "))
        self.assertLessEqual(len(body), 100 * graph_cli.CHARS_PER_TOKEN)
        self.assertGreater(len(toon.loads(body)), 0)

    def test_surrogate_input_is_stored_once_and_searchable(self):
        script = str(ROOT / "scripts/improve_graph.py")
        args = [sys.executable.encode(), script.encode(), b"--repo", str(self.repo).encode(), b"note", b"--text",
                b"retry budget is \xff five seconds in a"]
        first = subprocess.run(args, capture_output=True, timeout=budget(60))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(self.cli("search", "retry budget")[1])[0]["reinforced"], 1)
        journaled = (self.state / journal.WRITES).read_text()
        self.assertIn("retry budget is", journaled)

    def test_flattening_never_overwrites_a_real_dotted_key(self):
        row = {"id": "a", "stats": {"done": 1}, "stats.done": 9}
        self.assertEqual(toon.flatten(row), row)

    def test_old_surrogates_in_the_store_do_not_break_answers(self):
        self.assertEqual(graph_cli.tabular({"text": "bad \udcff byte"}), {"text": "bad ? byte"})


class LedgerFieldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve ledger fields ")
        self.repo = Path(self.temp.name)
        (self.repo / ".improve").mkdir()
        rows = [{"id": "t1", "status": "ready", "area": "docs", "severity": 5, "confidence": 1, "blast": 5},
                {"id": "t2", "status": "ready", "area": "quality", "paths": 5},
                {"id": "t3", "status": "ready", "area": "ui", "severity": 1, "confidence": 1, "blast": 1},
                {"id": "t4", "status": "ready", "area": "security", "severity": 5, "confidence": 0.5, "blast": 1}]
        (self.repo / ".improve/backlog.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        (self.repo / ".improve/run.json").write_text(json.dumps({
            "phase": "work", "preflight_complete": False, "gates": {"py": {"fast": "pytest -q"}},
            "gate_seconds": {"py.fast": 4}, "authorization": {"deps": "no"}, "continuation": {"mode": "daemon"},
            "deadline": "2026-11-13T00:00:00Z", "started_at": "2026-11-12T00:00:00Z",
            "focus": {"board_areas": {"prefer": ["ui"], "skip": ["docs"]}}}))

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/improve_ledger.py"), "--repo", str(self.repo),
                               "--format", "json", *args], capture_output=True, text=True, timeout=budget(60),
                              env={key: value for key, value in os.environ.items() if not key.startswith("IMPROVE_")})

    def test_id_and_created_at_cannot_be_rewritten(self):
        for field in ("id=t1", "created_at=now"):
            result = self.run_cli("set", "t2", "--field", field)
            self.assertEqual(result.returncode, 2, field)
        self.assertEqual(self.run_cli("set", "t2", "--json-field", 'id="t1"').returncode, 2)

    def test_file_filter_tolerates_odd_paths(self):
        result = self.run_cli("tasks", "--file", "src/a.py")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_run_view_includes_what_the_instructions_branch_on(self):
        view = json.loads(self.run_cli("run").stdout)
        for key in ("preflight_complete", "gates", "gate_seconds", "authorization", "continuation"):
            self.assertIn(key, view)
        self.assertEqual(json.loads(self.run_cli("run", "--full").stdout)["started_at"], "2026-11-12T00:00:00Z")

    def test_run_set_validates_and_protects_the_clock(self):
        result = self.run_cli("run-set", "cycle=4", "preflight_complete=true", "--json-field", 'active_item={"id": "t3"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        run = json.loads((self.repo / ".improve/run.json").read_text())
        self.assertEqual((run["cycle"], run["preflight_complete"], run["active_item"]), (4, True, {"id": "t3"}))
        for pair in ("deadline=2030-01-01T00:00:00Z", "outcome=completed", "started_at=x", "Bad-Key=1", "cycle=-1"):
            self.assertEqual(self.run_cli("run-set", pair).returncode, 2, pair)
        self.assertEqual(json.loads((self.repo / ".improve/run.json").read_text())["deadline"], "2026-11-13T00:00:00Z")
        # Keys the references tell the agent to write are all accepted (no forced raw writes).
        documented = self.run_cli("run-set", "last_activity_at=2026-11-12T01:00:00Z", "--json-field",
                                  'focus={"board_areas": {"prefer": ["ui"], "skip": []}}', "--json-field",
                                  'discovery={"generation": 5, "cursor": "cart"}', "mode=max")
        self.assertEqual(documented.returncode, 0, documented.stderr)

    def test_discovery_add_appends_a_checkpoint(self):
        self.assertEqual(self.run_cli("discovery-add", "--json", '{"lane": "ui", "generation": 2}').returncode, 0)
        self.assertEqual(self.run_cli("discovery-add", "--json", "[1]").returncode, 2)
        rows = json.loads(self.run_cli("discovery").stdout)["passes"]
        self.assertEqual(rows[-1]["lane"], "ui")

    def test_queue_honours_board_focus_and_critical_security(self):
        view = json.loads(self.run_cli("queue").stdout)
        self.assertEqual([row["id"] for row in view["tasks"]], ["t4", "t3", "t2"],
                         "critical security first, then prefer, docs (skip) left queued")
        self.assertEqual(view["skipped_areas"], 1)
        everything = json.loads(self.run_cli("queue", "--all-areas").stdout)
        self.assertIn("t1", [row["id"] for row in everything["tasks"]])


class IntegrityTests(RegressionCase):
    def test_restore_through_a_merge_chain_counts_once(self):
        with graph.locked(self.repo) as value:
            for node_id, useful in (("insight:aaaa", 5), ("insight:bbbb", 0), ("insight:cccc", 0)):
                graph.upsert(value, node_id, "insight", node_id, 0, kind="fact", summary=node_id, reinforced=1,
                             useful=useful, wrong=0)
        with graph.locked(self.repo) as value:
            graph.merge_into(value, "insight:bbbb", "insight:aaaa")
        with graph.locked(self.repo) as value:
            graph.merge_into(value, "insight:cccc", "insight:bbbb")
        with graph.locked(self.repo) as value:
            graph.restore_node(value, "insight:aaaa", "test")
            total = sum(int(node.get("useful", 0)) for node in value["nodes"].values() if node["type"] == "insight")
        self.assertEqual(total, 5)

    def test_one_content_word_swapped_is_not_a_duplicate(self):
        self.assertFalse(graph.duplicate("Take the read lock before touching the session cache in the worker",
                                         "Take the write lock before touching the session cache in the worker"))
        self.assertFalse(graph.duplicate("Mock the clock in billing tests so renewals are deterministic",
                                         "Mock the network in billing tests so renewals are deterministic"))
        self.assertTrue(graph.duplicate("Mock the gateway client in integration tests because the sandbox is flaky",
                                        "Mock the gateway client in integration tests since the sandbox is flaky"))

    def test_secrets_in_notes_are_redacted(self):
        self.cli("note", "--text", "The staging key is sk-abcdefghijklmnopqrstuvwxyz123456 for the payment sandbox")
        notes = " ".join(self.summaries())
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", notes)
        self.assertIn("[redacted]", notes)

    def test_trimming_drops_the_oldest_alias_not_the_alphabetically_first(self):
        value = graph.GraphState(graph.empty_graph())
        with mock.patch.object(graph, "MAX_ALIASES", 2):
            for old in ("insight:zzz", "insight:aaa", "insight:mmm"):
                graph.set_alias(value, old, "insight:keep")
            graph.trim_aliases(value)
        self.assertEqual(sorted(value["aliases"]), ["insight:aaa", "insight:mmm"])

    def test_trimmed_lessons_are_not_reimported_every_sync(self):
        import improve_learning as learning
        for index in range(6):
            learning.add_lesson(self.repo, f"Lesson number {index} about the flaky integration database setup")
        with mock.patch.object(graph, "MAX_INSIGHTS", 2):
            graph.sync(self.repo)
            first = load(self.repo)
            self.assertLessEqual(len(first["lessons"]), 4)
            revision = first["revision"]
            graph.sync(self.repo)
        self.assertEqual(load(self.repo)["revision"], revision, "nothing re-imported, nothing rewritten")

    def test_nested_gates_reach_the_brief(self):
        (self.state / "run.json").write_text(json.dumps({"gates": {"go": {"fast": "go test ./...", "batch": "go vet"}},
                                                         "gate_seconds": {"go": {"fast": 11}}}))
        graph.sync(self.repo)
        text = graph.brief(load(self.repo), "G", "R")
        self.assertIn("go.fast 11s", text)

    def test_focus_view_is_deterministic(self):
        value = load(self.repo)
        first = graph.board_view(value, focus="file:src/a.py", depth=2)["nodes"]
        program = ("import sys, json; sys.path.insert(0, %r); sys.path.insert(0, %r);"
                   "from graph_support import load; import improve_graph as g;"
                   "print(json.dumps([n['id'] for n in g.board_view(load(%r), focus='file:src/a.py', depth=2)['nodes']]))"
                   % (str(ROOT / "scripts"), str(ROOT / "tests"), str(self.repo)))
        for seed in ("1", "2"):
            result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=budget(60),
                                    env={**os.environ, "PYTHONHASHSEED": seed})
            self.assertEqual(json.loads(result.stdout), [node["id"] for node in first], result.stderr)


class MaintenanceScaleTests(unittest.TestCase):
    def test_forgetting_many_nodes_is_linear(self):
        value = graph.GraphState(graph.empty_graph())
        value["tick"] = 100
        for index in range(1000):
            graph.upsert(value, f"insight:{index:05d}", "insight", "x", kind="fact", summary=f"n{index}", reinforced=1,
                         archived="old", archived_tick=0, last_tick=0)
        for index in range(30000):
            value["edges"][f"e{index}"] = {"src": f"file:{index % 500}", "rel": "co_changed", "dst": f"file:{index % 499}"}
        started = time.monotonic()
        report = {}
        dreaming.forget(value, report)
        self.assertEqual(report["forgotten"], 1000)
        self.assertLess(time.monotonic() - started, budget(1.0))

    def test_recent_failures_outweigh_old_successes(self):
        current = 20
        old = [{"status": "done", "last_seq": 1} for _ in range(3)]
        recent = [{"status": "rejected", "last_seq": 20} for _ in range(2)]
        self.assertLess(dreaming.recency_ratio(old + recent, {}, current), 0.35)
        self.assertGreater(dreaming.recency_ratio(old, {}, 2), 0.99)

    def test_lock_helper_closes_its_descriptor_when_flock_fails(self):
        read, write = os.pipe()
        os.close(write)
        with mock.patch.object(graph.fcntl, "flock", side_effect=OSError("no locks here")):
            with self.assertRaises(graph.GraphError):
                graph.acquire_lock(read, 1)
        os.close(read)  # Still open: the caller owns it when the helper failed.


if __name__ == "__main__":
    unittest.main()
