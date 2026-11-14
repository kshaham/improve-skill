"""Reliability: curated knowledge survives recovery, busy writes are queued, co-change and
renamed files keep their history (review 2026-11-10, section 2)."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_support import (budget, GIT_IDENTITY, ROOT, environment, load, make_repo, restore_environment, write_ledger,  # noqa: E402
                           write_run)
import improve_dream as dreaming  # noqa: E402
import improve_graph as graph  # noqa: E402
import improve_graph_journal as journal  # noqa: E402
import improve_graph_store as store  # noqa: E402
import improve_toon as toon  # noqa: E402


def cli(repo, *args, env=None):
    return subprocess.run([sys.executable, str(ROOT / "scripts/improve_graph.py"), "--repo", str(repo), *args],
                          capture_output=True, text=True, timeout=budget(60), env={**os.environ, **(env or {})})


class ReliabilityCase(unittest.TestCase):
    files = ["src/a.py", "src/b.py", "src/c.py"]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve graph reliability ")
        self.previous = environment(Path(self.temp.name) / "home")
        self.repo = make_repo(Path(self.temp.name) / "repo", self.files).resolve()
        self.state = self.repo / ".improve"
        write_run(self.state, "live", {"test": 3})

    def tearDown(self):
        restore_environment(self.previous)
        self.temp.cleanup()

    def corrupt(self):
        path = self.state / "graph.db"
        for suffix in ("-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
        path.write_bytes(b"this is not a sqlite database" * 200)

    def write_note(self, text, kind="fact"):
        with graph.locked(self.repo, partial=True) as value:
            return journal.apply_write(value, "note", {"text": text, "kind": kind, "source": "agent"}, self.repo)["id"]


class WriteJournalTests(ReliabilityCase):
    def test_should_replay_curated_writes_made_after_the_checkpoint(self):
        before = self.write_note("Knowledge recorded before the checkpoint was taken")
        with graph.locked(self.repo) as value:
            dreaming.dream(value, self.repo)
        self.assertTrue((self.state / "graph.backup.db").exists())
        self.assertEqual(journal.read_lines(self.state / journal.WRITES), [], "a checkpoint truncates the journal")
        after = self.write_note("Knowledge recorded after the checkpoint, only in the journal")
        with graph.locked(self.repo, partial=True) as value:
            journal.apply_write(value, "rate", {"id": after, "verdict": "pin", "source": "user"}, self.repo)
        self.corrupt()
        with graph.locked(self.repo) as value:
            recovered = value["refine"]["recovered"]
        self.assertEqual(recovered["from"], "backup")
        self.assertEqual(recovered["replayed"], 2)
        nodes = load(self.repo)["nodes"]
        self.assertIn(before, nodes)
        self.assertIn(after, nodes)
        self.assertTrue(nodes[after]["pinned"], "the user's pin came back too")
        self.assertTrue((self.state / "graph.damaged.db").exists())

    def test_should_replay_the_whole_journal_when_no_checkpoint_exists(self):
        note = self.write_note("Knowledge written before any refinement or dream ran")
        self.corrupt()
        graph.sync(self.repo)
        self.assertIn(note, load(self.repo)["nodes"])

    def test_should_journal_board_and_agent_writes_but_not_ledger_ingest(self):
        write_ledger(self.state, [{"id": "t1", "status": "done", "area": "quality", "title": "x", "paths": ["src/a.py"]}])
        graph.sync(self.repo)
        self.write_note("An agent note that belongs in the journal")
        ops = [row["op"] for row in journal.read_lines(self.state / journal.WRITES)]
        self.assertEqual(ops, ["note"])


class BoardJournalTests(ReliabilityCase):
    def test_should_journal_board_curation_and_replay_it(self):
        import improve_board as board
        with graph.locked(self.repo) as value:
            dreaming.dream(value, self.repo)
        saved = board.graph_post(self.repo, {"action": "note", "request_id": "request-0001", "text": "Board decision: "
                                             "keep the payments module frozen", "kind": "decision"})
        node = saved["node"]["id"]
        board.graph_post(self.repo, {"action": "rate", "request_id": "request-0002", "id": node, "verdict": "pin"})
        ops = [(row["op"], row["args"].get("source")) for row in journal.read_lines(self.state / journal.WRITES)]
        self.assertEqual(ops, [("note", "user"), ("rate", "user")])
        self.corrupt()
        graph.sync(self.repo)
        restored = load(self.repo)["nodes"][node]
        self.assertEqual((restored["source"], restored["pinned"]), ("user", True))


class PendingWriteTests(ReliabilityCase):
    def test_should_queue_a_note_while_the_graph_is_held_and_apply_it_once(self):
        self.write_note("Seed knowledge so the store exists")
        lock = (self.state / "graph.lock").open("a+")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            result = cli(self.repo, "note", "--text", "Queued while the daemon held the graph", "--kind", "pitfall",
                         env={"IMPROVE_GRAPH_LOCK_WAIT": "0.2"})
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(toon.loads(result.stdout)["queued"])
        graph.sync(self.repo)
        graph.sync(self.repo)
        queued = [node for node in load(self.repo)["nodes"].values() if node.get("summary", "").startswith("Queued while")]
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["reinforced"], 1, "drained exactly once")
        self.assertEqual(queued[0]["kind"], "pitfall")
        self.assertFalse((self.state / journal.PENDING).exists(), "drained files are removed after commit")
        self.assertEqual(list(self.state.glob("graph-pending.jsonl.*.draining")), [])

    def test_should_skip_a_queued_write_that_no_longer_applies(self):
        journal.queue_pending(self.repo, "rate", {"id": "insight:gone", "verdict": "useful", "source": "agent"})
        journal.queue_pending(self.repo, "note", {"text": "Still applies after the bad entry", "source": "agent"})
        graph.sync(self.repo)
        self.assertTrue(any(node.get("summary") == "Still applies after the bad entry"
                            for node in load(self.repo)["nodes"].values()))


class CoChangeRetentionTests(ReliabilityCase):
    def test_should_keep_co_change_weight_after_the_tasks_are_pruned(self):
        rows = [{"id": f"t{index}", "status": "done", "area": "quality", "title": f"t{index}",
                 "paths": ["src/a.py", "src/b.py"]} for index in range(3)]
        write_ledger(self.state, rows)
        graph.sync(self.repo, refine_after=True)
        key = graph.edge_key("file:src/a.py", "co_changed", "file:src/b.py")
        self.assertEqual(load(self.repo)["edges"][key]["weight"], 3)
        bound = graph.MAX_TASKS
        try:
            graph.MAX_TASKS = 1
            with graph.locked(self.repo) as value:
                report = graph.refine(value, self.repo)
            self.assertEqual(report["pruned_tasks"], 2)
            self.assertEqual(load(self.repo)["edges"][key]["weight"], 3, "pruning keeps the pair as counts")
            graph.MAX_TASKS = bound
            with graph.locked(self.repo) as value:
                graph.restore_node(value, "task:live:t0", "test")
                graph.refine(value, self.repo)
            self.assertEqual(load(self.repo)["edges"][key]["weight"], 3, "a restored task is not counted twice")
        finally:
            graph.MAX_TASKS = bound


class RenameTests(ReliabilityCase):
    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *GIT_IDENTITY, *args], check=True, capture_output=True)

    def test_should_move_history_and_knowledge_to_the_renamed_file(self):
        rows = [{"id": "t1", "status": "done", "area": "quality", "title": "one", "paths": ["src/a.py"]},
                {"id": "t2", "status": "rejected", "area": "quality", "title": "two", "paths": ["src/a.py"]}]
        write_ledger(self.state, rows)
        with graph.locked(self.repo) as value:
            note = graph.add_insight(value, "The parser in this module must stay single pass", "convention",
                                     about=["file:src/a.py"], repo=self.repo)["id"]
        graph.sync(self.repo, refine_after=True)
        self.git("mv", "src/a.py", "src/renamed.py")
        self.git("commit", "-qm", "rename")
        with graph.locked(self.repo) as value:
            report = graph.refine(value, self.repo)
        self.assertEqual(report["renamed"], 1)
        value = load(self.repo)
        self.assertNotIn("file:src/a.py", value["nodes"])
        moved = value["nodes"]["file:src/renamed.py"]
        self.assertEqual((moved["stats"]["done"], moved["stats"]["rejected"]), (1, 1))
        self.assertIn(graph.edge_key(note, "about", "file:src/renamed.py"), value["edges"])
        self.assertEqual(graph.resolve(value, "file:src/a.py"), "file:src/renamed.py")
        self.assertIn("src/renamed.py", value["nodes"][note]["fingerprints"], "fingerprints follow the move")
        # The ledger still names the old path: ingest lands on the new node.
        graph.sync(self.repo)
        value = load(self.repo)
        self.assertNotIn("file:src/a.py", value["nodes"])
        result = cli(self.repo, "context", "--files", "src/renamed.py")
        self.assertIn("1/2 past attempts verified", result.stdout)
        self.assertIn(note, result.stdout)
        old = cli(self.repo, "context", "--files", "src/a.py")
        self.assertIn(note, old.stdout, "the old path still finds the history")

    def test_should_not_rescan_history_when_head_has_not_moved(self):
        with graph.locked(self.repo) as value:
            graph.upsert(value, "file:src/a.py", "file", "src/a.py")
            graph.refine(value, self.repo)
            head = value["refine"]["head"]
            self.assertEqual(graph.git_renames(self.repo, head), ([], head))


class StoreHygieneTests(ReliabilityCase):
    def test_should_keep_evidence_of_earlier_damage_incidents(self):
        path = self.state / "graph.db"
        for attempt in range(5):
            path.write_bytes(f"damage {attempt}".encode())
            store.set_aside(path)
        self.assertEqual((self.state / "graph.damaged.db").read_bytes(), b"damage 4")
        older = sorted(self.state.glob("graph.damaged.*.db"))
        self.assertEqual(len(older), store.DAMAGED_KEEP)

    def test_should_warn_when_reading_a_newer_format(self):
        self.write_note("Seed knowledge for the version check")
        import sqlite3
        con = sqlite3.connect(self.state / "graph.db")
        con.execute("UPDATE meta SET value='99' WHERE key='version'")
        con.commit()
        con.close()
        result = cli(self.repo, "search", "seed")
        self.assertEqual(result.returncode, 0)
        self.assertIn("newer skill", result.stderr)
        write = cli(self.repo, "rate", "insight:x", "useful")
        self.assertEqual(write.returncode, 2)
        self.assertIn("newer skill", write.stderr)

    def test_should_report_a_partial_conflict_as_an_error_not_a_crash(self):
        self.assertTrue(issubclass(store.GraphConflict, ValueError))

    def test_should_record_a_diff_and_the_rules_version_at_each_dream(self):
        self.write_note("A note so the dream has something to see")
        with graph.locked(self.repo) as value:
            report = dreaming.dream(value, self.repo)
            self.assertEqual(value["dream"]["rules_version"], dreaming.RULES_VERSION)
        self.assertIn("summary", report["diff"])
        result = cli(self.repo, "diff", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
