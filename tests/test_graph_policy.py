"""Knowledge-graph policy and robustness: protected knowledge, damage classification, bounded
locks, lesson import, gate attribution, stable co-change links, due checks, and CLI exit codes."""
import fcntl
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_support import budget, ROOT, environment, load, make_repo, restore_environment, write_ledger, write_run  # noqa: E402
import improve_dream as dreaming  # noqa: E402
import improve_graph as graph  # noqa: E402
import improve_graph_store as store  # noqa: E402
import improve_learning as learning  # noqa: E402
import improve_toon as toon  # noqa: E402


def cli(repo, *args, env=None):
    return subprocess.run([sys.executable, str(ROOT / "scripts/improve_graph.py"), "--repo", str(repo), *args],
                          capture_output=True, text=True, timeout=budget(60), env={**os.environ, **(env or {})})


class PolicyCase(unittest.TestCase):
    files = ["src/a.py", "src/b.py", "src/c.py", "latest/x.py"]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve graph policy ")
        self.previous = environment(Path(self.temp.name) / "home")
        self.repo = make_repo(Path(self.temp.name) / "repo", self.files).resolve()
        self.state = self.repo / ".improve"
        write_run(self.state, "live", {"test": 3, "lint": 1})

    def tearDown(self):
        restore_environment(self.previous)
        self.temp.cleanup()

    def age(self, value, runs):
        for index in range(runs):
            value["runs"].append({"run_id": f"old-{index}", "seq": index + 1})


class ProtectedKnowledgeTests(PolicyCase):
    def test_user_decisions_survive_idle_runs_and_agent_contradiction(self):
        with graph.locked(self.repo) as value:
            node = graph.add_insight(value, "Payments code is frozen until the audit ends.", "decision", source="user",
                                     about=["area:security"], repo=self.repo)
            node_id = node["id"]
            self.age(value, 20)
            node["last_seq"] = 0
            graph.rate(value, node_id, "wrong", source="agent")
            graph.rate(value, node_id, "wrong", source="agent")
            graph.refine(value, self.repo)
            self.assertIsNone(value["nodes"][node_id].get("archived"))
            self.assertEqual(value["nodes"][node_id]["wrong"], 2, "the agent's doubt is recorded, not acted on")
            self.assertIn(node_id, graph.brief(value, "G", "R"))
        with graph.locked(self.repo) as value:
            graph.rate(value, node_id, "wrong", source="user")
            self.assertTrue(value["nodes"][node_id]["archived"], "only the user archives user knowledge")

    def test_size_caps_never_remove_user_or_learning_knowledge(self):
        with graph.locked(self.repo) as value:
            user = graph.add_insight(value, "User convention: all handlers return Result types.", "convention", source="user")
            learned = graph.add_insight(value, "Lesson: run the integration suite serially.", "lesson", source="learning", reinforce=False)
            user["last_seq"] = learned["last_seq"] = 0
            with mock.patch.object(graph, "MAX_INSIGHTS", 20):
                for index in range(40):
                    note = graph.add_insight(value, f"Agent fact {index} about alpha beta gamma delta {index * 7}", "fact")
                    note["useful"] = 5
                self.age(value, 30)
                graph.refine(value, self.repo)
            insights = {key for key, node in value["nodes"].items() if node["type"] == "insight"}
            self.assertIn(user["id"], insights)
            self.assertIn(learned["id"], insights)
            self.assertLessEqual(len(insights), 22)

    def test_recently_restored_knowledge_is_protected_from_fade(self):
        with graph.locked(self.repo) as value:
            note = graph.add_insight(value, "A note nobody cites for a long while.", "fact")
            note["last_seq"] = 0
            self.age(value, 20)
            note["protected_until_tick"] = int(value.get("tick", 0)) + 3
            graph.refine(value, self.repo)
            self.assertIsNone(value["nodes"][note["id"]].get("archived"))
            note["protected_until_tick"] = -1
            graph.refine(value, self.repo)
            self.assertTrue(value["nodes"][note["id"]].get("archived"))


class DamageClassificationTests(PolicyCase):
    def test_only_corruption_counts_as_damage(self):
        self.assertFalse(store.is_damage(sqlite3.IntegrityError("UNIQUE constraint failed: node.id")))
        self.assertFalse(store.is_damage(sqlite3.ProgrammingError("Cannot operate on a closed database.")))
        self.assertFalse(store.is_damage(sqlite3.OperationalError("database is locked")))
        # A missing graph table is caught by Store.open; a missing table elsewhere is a bug, not damage.
        self.assertFalse(store.is_damage(sqlite3.OperationalError("no such table: meta")))
        self.assertTrue(store.is_damage(sqlite3.DatabaseError("file is not a database")))
        self.assertTrue(store.is_damage(sqlite3.DatabaseError("database disk image is malformed")))

    def test_a_code_error_rolls_back_without_discarding_the_store(self):
        with graph.locked(self.repo) as value:
            graph.add_insight(value, "Knowledge that must survive a later bug.", "fact")
        path = graph.graph_path(self.repo)
        with mock.patch.object(store.Store, "save", side_effect=sqlite3.IntegrityError("UNIQUE constraint failed")):
            with self.assertRaises(sqlite3.IntegrityError):
                with graph.locked(self.repo) as value:
                    graph.add_insight(value, "A second note.", "fact")
        self.assertTrue(path.exists())
        self.assertFalse(path.with_name("graph.damaged.db").exists())
        self.assertEqual(len([node for node in load(self.repo)["nodes"].values() if node["type"] == "insight"]), 1)

    def test_restore_clears_a_stale_write_ahead_log(self):
        with graph.locked(self.repo) as value:
            graph.add_insight(value, "Checkpointed knowledge.", "fact")
            graph.refine(value, self.repo)
        path = graph.graph_path(self.repo)
        Path(str(path) + "-wal").write_bytes(b"stale" * 100)
        path.unlink()
        self.assertTrue(store.restore_backup(path, graph.backup_path(path)))
        self.assertFalse(Path(str(path) + "-wal").exists())
        self.assertEqual(len([node for node in load(self.repo)["nodes"].values() if node["type"] == "insight"]), 1)

    def test_a_newer_format_is_refused_for_writing_but_readable(self):
        with graph.locked(self.repo) as value:
            graph.add_insight(value, "Written by this version.", "fact")
            value["version"] = graph.VERSION + 1
        with self.assertRaises(graph.GraphError):
            with graph.locked(self.repo) as value:
                pass
        self.assertEqual(len([node for node in load(self.repo)["nodes"].values() if node["type"] == "insight"]), 1)


class LockTests(PolicyCase):
    def test_a_held_lock_fails_fast_with_a_retryable_exit_code(self):
        with graph.locked(self.repo) as value:
            graph.add_insight(value, "Seed knowledge.", "fact")
        lock = (self.state / "graph.lock").open("a+")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with mock.patch.dict(os.environ, {"IMPROVE_GRAPH_LOCK_WAIT": "0.3"}):
                with self.assertRaises(graph.GraphBusy):
                    with graph.locked(self.repo):
                        pass
            # A note is queued, not lost or failed, while another process holds the graph.
            result = cli(self.repo, "note", "--text", "Blocked note that is applied later.", env={"IMPROVE_GRAPH_LOCK_WAIT": "0.3"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIs(toon.loads(result.stdout)["queued"], True)
            # A whole-graph write cannot be queued and still fails fast with the retryable code.
            result = cli(self.repo, "restore", "insight:none", env={"IMPROVE_GRAPH_LOCK_WAIT": "0.3"})
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn("retry later", result.stderr)
            reads = cli(self.repo, "search", "seed")
            self.assertEqual(reads.returncode, 0, "readers never wait for the writer lock")
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()


class LessonImportTests(PolicyCase):
    def test_lessons_import_once_and_return_only_when_reinforced(self):
        learning.add_lesson(self.repo, "Run the integration suite with --serial; parallel runs flake.", area="coverage")
        graph.sync(self.repo)
        graph.sync(self.repo)
        lessons = [node for node in load(self.repo)["nodes"].values() if node.get("kind") == "lesson"]
        self.assertEqual(len(lessons), 1)
        with graph.locked(self.repo) as value:
            graph.remove_nodes(value, [lessons[0]["id"]], reason="test removal", op="capped")
        graph.sync(self.repo)
        self.assertEqual([node for node in load(self.repo)["nodes"].values() if node.get("kind") == "lesson"], [],
                         "a removed lesson does not come back on the next sync")
        learning.add_lesson(self.repo, "Run the integration suite with --serial; parallel runs flake.", area="coverage")
        graph.sync(self.repo)
        lessons = [node for node in load(self.repo)["nodes"].values() if node.get("kind") == "lesson"]
        self.assertEqual(len(lessons), 1, "a reinforced lesson is imported again")
        self.assertEqual(lessons[0]["reinforced"], 2)


class IngestTests(PolicyCase):
    def test_gate_names_match_whole_tokens_or_structured_lists(self):
        write_ledger(self.state, [
            {"id": "t1", "area": "quality", "status": "done", "file": "src/a.py", "verification": "ran test: 12 passed"},
            {"id": "t2", "area": "quality", "status": "done", "file": "src/b.py", "verification": "checked the latest build"},
            {"id": "t3", "area": "quality", "status": "done", "file": "src/c.py", "verification": {"gates": ["lint"], "note": "test skipped"}},
        ])
        graph.sync(self.repo)
        edges = load(self.repo)["edges"]
        self.assertIn("task:live:t1|verified_by|gate:test", edges)
        self.assertNotIn("task:live:t2|verified_by|gate:test", edges, "`latest` is not the gate `test`")
        self.assertIn("task:live:t3|verified_by|gate:lint", edges)
        self.assertNotIn("task:live:t3|verified_by|gate:test", edges, "a structured list is exact")

    def test_supervisor_blocks_are_not_knowledge(self):
        write_ledger(self.state, [
            {"id": "t1", "area": "ui", "status": "blocked", "file": "src/a.py", "blocked_by": "supervisor",
             "blocked_reason": "3 consecutive cycles failed or recorded no new evidence"},
            {"id": "t2", "area": "ui", "status": "blocked", "file": "src/b.py", "blocked_by": "worker",
             "blocked_reason": "Worker halted: simulator unavailable"},
        ])
        graph.sync(self.repo)
        value = load(self.repo)
        blockers = [node for node in value["nodes"].values() if node["type"] == "blocker"]
        self.assertEqual(len(blockers), 1)
        self.assertIn("simulator", blockers[0]["label"])
        self.assertEqual(value["nodes"]["task:live:t1"]["status"], "blocked", "the task itself is still recorded")

    def test_co_change_links_keep_their_creation_time_across_refines(self):
        write_ledger(self.state, [
            {"id": "t1", "area": "quality", "status": "done", "paths": ["src/a.py", "src/b.py"]},
            {"id": "t2", "area": "quality", "status": "done", "paths": ["src/a.py", "src/b.py"]},
        ])
        graph.sync(self.repo, refine_after=True)
        key = "file:src/a.py|co_changed|file:src/b.py"
        first = load(self.repo)["edges"][key]
        with mock.patch.object(graph, "stamp", return_value="2030-01-01T00:00:00Z"):
            with graph.locked(self.repo) as value:
                graph.refine(value, self.repo)
        second = load(self.repo)["edges"][key]
        self.assertEqual(first["created_at"], second["created_at"])
        self.assertEqual(second["weight"], 2)


class ScheduleTests(PolicyCase):
    def test_a_fresh_graph_refines_at_once_but_dreams_only_after_its_interval(self):
        write_ledger(self.state, [{"id": "t1", "area": "ui", "status": "done", "file": "src/a.py"}])
        graph.sync(self.repo, refine_interval=1800, dream_interval=10800)
        value = load(self.repo)
        self.assertTrue(value["refine"].get("history"), "statistics are useful from the first ingest")
        self.assertEqual(value.get("tick", 0), 0, "the first sync does not dream")
        self.assertFalse(graph.refine_due(value, 1800))
        self.assertFalse(dreaming.dream_due(value, 10800))
        with graph.locked(self.repo) as value:
            value["dream"]["since"] = "2000-01-01T00:00:00Z"
        value = load(self.repo)
        self.assertTrue(dreaming.dream_due(value, 10800))
        self.assertFalse(dreaming.dream_due(value, 0), "0 disables scheduled dreaming")
        graph.sync(self.repo, dream_interval=10800)
        self.assertEqual(load(self.repo)["tick"], 1, "an overdue dream runs on the next sync")


class CliTests(PolicyCase):
    def test_context_names_neighbors_without_statistics_and_history_pages(self):
        write_ledger(self.state, [{"id": "t1", "area": "ui", "status": "ready", "file": "src/a.py"}])
        graph.sync(self.repo)
        result = cli(self.repo, "context", "--files", "src/new.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Known files here without outcome statistics yet: src/a.py", result.stdout)
        with graph.locked(self.repo) as value:
            texts = ("Widgets cache their layout between frames.", "The export dialog ignores keyboard focus.",
                     "Parallel gate runs flake on the shared fixture database.")
            ids = [graph.add_insight(value, text, "fact")["id"] for text in texts]
        with graph.locked(self.repo) as value:
            graph.remove_nodes(value, ids, reason="test", op="forgotten")
        first = toon.loads(cli(self.repo, "history", "--limit", "2").stdout)
        rest = toon.loads(cli(self.repo, "history", "--limit", "2", "--offset", "2").stdout)
        self.assertEqual(first["total"], 3)
        self.assertEqual(len(first["rows"]), 2)
        self.assertEqual(len(rest["rows"]), 1)
        self.assertNotIn(rest["rows"][0]["id"], {row["id"] for row in first["rows"]})

    def test_unlink_proposals_and_consolidate_commands(self):
        with graph.locked(self.repo) as value:
            left = graph.add_insight(value, "Widgets need the warm cache before the render gate.", "technique")
            right = graph.add_insight(value, "Widgets render only after the warm cache fills.", "technique")
            graph.link(value, left["id"], "about", "file:src/a.py")
            graph.link(value, right["id"], "about", "file:src/a.py")
            value["edges"][graph.edge_key(*sorted([left["id"], right["id"]]) [:1], "similar_to", sorted([left["id"], right["id"]])[1])] = {
                "src": sorted([left["id"], right["id"]])[0], "rel": "similar_to", "dst": sorted([left["id"], right["id"]])[1],
                "inferred": True, "confidence": 0.8, "rule": "shared-wording", "weight": 1, "created_at": graph.stamp()}
            value["dream"]["proposals"] = [{"id": "dream:abc123", "members": sorted([left["id"], right["id"]]),
                                            "strength": 0.8, "kinds": ["technique"], "created_tick": 0}]
        listed = cli(self.repo, "proposals")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("dream:abc123", listed.stdout)
        merged = cli(self.repo, "consolidate", "--proposal", "dream:abc123", "--kind", "technique",
                     "--text", "Fill the warm cache before widgets render or pass the render gate.")
        self.assertEqual(merged.returncode, 0, merged.stderr)
        summary = toon.loads(merged.stdout)
        value = load(self.repo)
        self.assertIn(summary["id"], value["nodes"])
        self.assertEqual(graph.resolve(value, left["id"]), summary["id"])
        self.assertEqual(value["dream"]["proposals"], [])
        with graph.locked(self.repo) as value:
            note = graph.add_insight(value, "Something about src/b.py and caches.", "fact")
            graph.target_id(value, "file:src/b.py", self.repo)
            key = graph.edge_key(note["id"], "applies_to", "file:src/b.py")
            value["edges"][key] = {"src": note["id"], "rel": "applies_to", "dst": "file:src/b.py", "inferred": True,
                                   "confidence": 0.7, "rule": "cited-on-file", "weight": 1, "created_at": graph.stamp()}
        rejected = cli(self.repo, "unlink", note["id"], "applies_to", "file:src/b.py", "--reason", "unrelated")
        self.assertEqual(rejected.returncode, 0, rejected.stderr)
        value = load(self.repo)
        self.assertNotIn(key, value["edges"])
        self.assertIn(key, value["dream"]["rejected"])
        self.assertEqual(cli(self.repo, "unlink", note["id"], "about", "file:src/b.py").returncode, 2, "only inferred links")


if __name__ == "__main__":
    unittest.main()
