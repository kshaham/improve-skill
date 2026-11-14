"""The SQLite knowledge-graph store: partial sessions, minimal writes, search index, history,
migration, damage recovery, isolation, fairness, and disk reclamation. No agents are launched."""
import contextlib
import json
from pathlib import Path
import random
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import fts5_available  # noqa: E402
from graph_support import budget, World, check_invariants, environment, load, make_repo, restore_environment, write_ledger, write_run  # noqa: E402
import improve_graph as graph  # noqa: E402
import improve_graph_store as store_module  # noqa: E402
from improve_graph_store import Store  # noqa: E402


def row_changes(target):
    """Record each save's logical writes (rows the graph changed) and physical row changes
    (which include SQLite's own full-text index bookkeeping)."""
    original = Store.save

    def counting(self, value, hook):
        before = self.con.total_changes
        result = original(self, value, hook)
        logical = sum(self.last_write.values()) if result else 0
        target.append((logical, self.con.total_changes - before))
        return result
    return mock.patch.object(Store, "save", counting)


class StoreFixture(unittest.TestCase):
    """A world after 120 cycles and several dreams: thousands of rows, inferred links, history."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="improve graph store ")
        cls.previous = environment(Path(cls.temp.name) / "home")
        cls.world = World(Path(cls.temp.name) / "repo", seed=5)
        cls.repo = cls.world.repo
        for cycle in range(120):
            cls.world.advance()
            graph.sync(cls.repo, dream_after=cycle % 20 == 19)
        cls.full = load(cls.repo)

    @classmethod
    def tearDownClass(cls):
        restore_environment(cls.previous)
        cls.temp.cleanup()


class PartialSessionTests(StoreFixture):
    def test_fixture_has_inferred_links_and_history(self):
        self.assertGreater(sum(1 for edge in self.full["edges"].values() if edge.get("inferred")), 20)
        check_invariants(self, self.full)

    def test_context_from_a_partial_session_equals_the_full_graph(self):
        rand = random.Random(1)
        labels = [node["label"] for node in self.full["nodes"].values() if node["type"] == "file"]
        directories = sorted({label.rsplit("/", 1)[0] for label in labels})
        areas = [None, *sorted(node["label"] for node in self.full["nodes"].values() if node["type"] == "area"), "nope"]
        texts = [None, "fixture parallel", "cache store worker", "zzz"]
        for case in range(80):
            files = rand.sample(labels, rand.randint(0, 3))
            if case % 5 == 0:
                files.append(rand.choice(directories))
            if case % 7 == 0:
                files.append(rand.choice(directories) + "/brand_new.py")
            area, text = rand.choice(areas), rand.choice(texts)
            if not files and not area and not text:
                text = "fixture"
            with self.subTest(files=files, area=area, text=text), graph.reading(self.repo) as part:
                graph.load_context(part, files, area, self.repo, text)
                expected = graph.context(self.full, files, area, text, repo=self.repo)
                self.assertEqual(graph.context(part, files, area, text, repo=self.repo), expected)

    def test_search_and_node_detail_from_partial_sessions_equal_the_full_graph(self):
        rand = random.Random(2)
        words = ["cache", "fixture parallel", "world task", "device farm offline", "schema m3", "store worker share",
                 "nothing-matches-this", "part3", "py fast"]
        for query in words:
            for types in (None, ("insight",), ("task", "blocker")):
                with self.subTest(query=query, types=types), graph.reading(self.repo) as part:
                    graph.load_search(part, query)
                    self.assertEqual(graph.search(part, query, 15, types), graph.search(self.full, query, 15, types))
        for node_id in rand.sample(sorted(self.full["nodes"]), 40):
            with self.subTest(node=node_id), graph.reading(self.repo) as part:
                self.assertEqual(graph.node_detail(part, graph.load_node(part, node_id)),
                                 graph.node_detail(self.full, node_id))

    def test_knowledge_reachable_only_through_related_links_loads_in_partial_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_repo(Path(directory), ["src/a.py", "src/b.py"])
            with graph.locked(repo) as value:
                anchor = graph.add_insight(value, "Close the pool before forking workers", "pitfall", about=["src/a.py"],
                                           repo=repo)["id"]
                partner = graph.add_insight(value, "Forked workers inherit open sockets", "technique")["id"]
                left, right = sorted([anchor, partner])
                value["edges"][graph.edge_key(left, "co_cited", right)] = dict(
                    src=left, rel="co_cited", dst=right, inferred=True, confidence=0.9, rule="cited-together",
                    generation=1, evidence={"tasks": {}})
            full = load(repo)
            expected = graph.context(full, ["src/a.py"], repo=repo)
            self.assertIn(partner, [row["id"] for row in expected["insights"]])
            with graph.reading(repo) as part:
                graph.load_context(part, ["src/a.py"], None, repo)
                self.assertEqual(graph.context(part, ["src/a.py"], repo=repo), expected)

    def test_partial_sessions_load_a_fraction_of_the_graph(self):
        total = len(self.full["nodes"])
        file_id = next(key for key, node in self.full["nodes"].items() if node["type"] == "file")
        with graph.reading(self.repo) as part:
            graph.load_node(part, file_id)
            self.assertLess(len(part["nodes"]), total * 0.15, "a node query reads one neighborhood")
        with graph.reading(self.repo) as part:
            graph.load_search(part, "device farm offline")
            self.assertLess(len(part["nodes"]), total * 0.6, "a search reads only candidate rows")

    def test_search_without_fts5_scans_and_agrees(self):
        expected = graph.search(self.full, "fixture parallel", 15)
        with mock.patch.object(Store, "fts_usable", lambda self: False), graph.reading(self.repo) as part:
            self.assertIsNone(part.store.search_candidates({"fixture"}))
            graph.load_search(part, "fixture parallel")
            self.assertEqual(len(part["nodes"]), len(self.full["nodes"]), "falls back to every row")
            self.assertEqual(graph.search(part, "fixture parallel", 15), expected)


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve graph writes ")
        self.previous = environment(Path(self.temp.name) / "home")
        self.world = World(Path(self.temp.name) / "repo", seed=9)
        self.repo = self.world.repo
        for cycle in range(40):
            self.world.advance()
            graph.sync(self.repo, dream_after=cycle == 39)
        self.first_after_dream = []
        with row_changes(self.first_after_dream):
            graph.sync(self.repo)  # Stamps live nodes with the new dream tick, once per dream.

    def tearDown(self):
        restore_environment(self.previous)
        self.temp.cleanup()

    def test_writes_touch_only_the_rows_that_changed(self):
        changes = []
        insight = self.world.insights[0][0]
        with row_changes(changes):
            with graph.locked(self.repo, partial=True) as value:
                graph.add_insight(value, "A brand new pitfall about the release lane", "pitfall",
                                  about=["mod00/part1.py"], repo=self.repo)
            with graph.locked(self.repo, partial=True) as value:
                graph.fetch(value, [insight])
                graph.rate(value, insight, "useful")
            with graph.locked(self.repo) as value:
                pass
            graph.sync(self.repo)
        nodes = len(load(self.repo)["nodes"])
        self.assertGreater(nodes, 300)
        self.assertEqual(changes[0][0], 4, "one note: its node, its link to an existing file, revision, update time")
        self.assertEqual(changes[1][0], 3, "one rating: its node, revision, and update time")
        self.assertEqual(changes[2], (0, 0), "a session that changes nothing writes nothing")
        self.assertEqual(changes[3], (0, 0), "an unchanged ledger syncs without writing")
        self.assertLess(max(physical for _, physical in changes), 20, f"out of {nodes} nodes")

    def test_partial_session_cannot_overwrite_unloaded_rows(self):
        existing = next(key for key, node in load(self.repo)["nodes"].items() if node["type"] == "file")
        before = load(self.repo)
        with self.assertRaises(store_module.GraphConflict), graph.locked(self.repo, partial=True) as value:
            graph.upsert(value, existing, "file", "clobbered")
        self.assertEqual(load(self.repo), before, "the refused session wrote nothing")

    def test_exceptions_roll_back_everything(self):
        before = load(self.repo)
        with self.assertRaises(ValueError), graph.locked(self.repo) as value:
            graph.add_insight(value, "This note must never be saved anywhere", "fact")
            graph.remove_nodes(value, [next(iter(value["nodes"]))], reason="test")
            raise ValueError("boom")
        self.assertEqual(load(self.repo), before)
        self.assertEqual(graph.history(self.repo, 5)["total"], 0)

    @unittest.skipUnless(fts5_available(), "this Python's SQLite has no FTS5")
    def test_index_built_without_fts5_is_rebuilt_by_a_capable_writer(self):
        with mock.patch.object(Store, "fts_usable", lambda self: False):
            with graph.locked(self.repo, partial=True) as value:
                note = graph.add_insight(value, "Written by a Python without full-text search", "fact")
        with graph.reading(self.repo) as part:
            self.assertIsNone(part.store.search_candidates({"written"}), "a stale index is never trusted")
        with graph.locked(self.repo, partial=True) as value:
            graph.add_insight(value, "Any write from a capable Python rebuilds it", "fact")
        with graph.reading(self.repo) as part:
            self.assertIn(note["id"], part.store.search_candidates({"written"}))
            graph.load_search(part, "written python search")
            self.assertEqual(graph.search(part, "written python search"), graph.search(load(self.repo), "written python search"))

    def test_the_once_per_dream_restamp_does_not_rewrite_the_search_index(self):
        logical, physical = self.first_after_dream[0]
        self.assertGreater(logical, 100)
        self.assertLess(physical, logical * 1.2, "node rows only; unchanged search words are not reindexed")

    def test_revision_counts_changes_only(self):
        revision = load(self.repo)["revision"]
        for _ in range(3):
            graph.sync(self.repo)
        self.assertEqual(load(self.repo)["revision"], revision)
        with graph.locked(self.repo, partial=True) as value:
            graph.add_insight(value, "One change makes exactly one revision", "fact")
        self.assertEqual(load(self.repo)["revision"], revision + 1)


class LegacyAndDamageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve graph damage ")
        self.previous = environment(Path(self.temp.name) / "home")
        self.repo = make_repo(Path(self.temp.name) / "repo", ["src/a.py", "src/b.py"])
        write_run(self.repo / ".improve", "live", {"py.fast": 3})
        write_ledger(self.repo / ".improve", [dict(id="t", title="Task", status="done", paths=["src/a.py", "src/b.py"])])

    def tearDown(self):
        restore_environment(self.previous)
        self.temp.cleanup()

    def test_a_graph_json_from_before_sqlite_is_migrated_once(self):
        legacy = graph.empty_graph()
        legacy["revision"] = 41
        legacy["nodes"] = {"insight:old": {"id": "insight:old", "type": "insight", "label": "Old knowledge kept",
                                           "summary": "Old knowledge kept across the migration", "kind": "fact"},
                           "file:src/a.py": {"id": "file:src/a.py", "type": "file", "label": "src/a.py"}}
        legacy["edges"] = {"insight:old|about|file:src/a.py": {"src": "insight:old", "rel": "about", "dst": "file:src/a.py"}}
        legacy_path = self.repo / ".improve" / "graph.json"
        legacy_path.write_text(json.dumps(legacy))
        self.assertIn("insight:old", load(self.repo)["nodes"], "readers see the old file before migration")
        self.assertFalse(graph.graph_path(self.repo).exists(), "reading never creates the database")
        graph.sync(self.repo)
        value = load(self.repo)
        self.assertIn("insight:old", value["nodes"])
        self.assertIn("insight:old|about|file:src/a.py", value["edges"])
        self.assertGreaterEqual(value["revision"], 41)
        self.assertFalse(legacy_path.exists())
        self.assertTrue((self.repo / ".improve" / "graph.json.migrated").exists())
        with graph.reading(self.repo) as part:
            graph.load_search(part, "migration")
            self.assertEqual(graph.search(part, "migration")[0]["id"], "insight:old", "the index covers migrated rows")
        graph.sync(self.repo)
        self.assertEqual(len([node for node in load(self.repo)["nodes"] if node == "insight:old"]), 1)

    def damage_cases(self):
        def garbage(path):
            path.write_bytes(b"this is not a database at all")

        def truncated(path):
            data = path.read_bytes()
            path.write_bytes(data[:len(data) // 3])

        def foreign(path):
            path.unlink()
            Path(str(path) + "-wal").unlink(missing_ok=True)
            Path(str(path) + "-shm").unlink(missing_ok=True)
            other = sqlite3.connect(str(path))
            other.execute("CREATE TABLE unrelated(x)")
            other.commit()
            other.close()

        def zeroed_header(path):
            data = bytearray(path.read_bytes())
            data[:100] = bytes(100)
            path.write_bytes(bytes(data))
        return {"garbage": garbage, "truncated": truncated, "foreign schema": foreign, "zeroed header": zeroed_header}

    def test_every_kind_of_damage_recovers_from_the_checkpoint(self):
        for name, damage in self.damage_cases().items():
            with self.subTest(damage=name):
                shutil.rmtree(self.repo / ".improve" / "graph.db", ignore_errors=True)
                for leftover in (self.repo / ".improve").glob("graph*.db*"):
                    leftover.unlink()
                graph.sync(self.repo)
                with graph.locked(self.repo) as value:
                    kept = graph.add_insight(value, f"Decision that must survive {name}", "decision", source="user")["id"]
                    graph.refine(value, self.repo)
                path = graph.graph_path(self.repo)
                with graph.locked(self.repo):  # Fold the WAL in so the damage hits real data.
                    pass
                with contextlib.closing(sqlite3.connect(str(path))) as connection:
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                damage(path)
                self.assertEqual(load(self.repo)["nodes"], {}, "readers never repair")
                graph.sync(self.repo)
                value = load(self.repo)
                self.assertIn(kept, value["nodes"])
                self.assertIn("task:live:t", value["nodes"])
                self.assertEqual(value["refine"]["recovered"]["from"], "backup")
                self.assertTrue(graph.damaged_path(path).exists())
                check_invariants(self, value)

    def test_a_missing_database_is_restored_from_its_checkpoint(self):
        graph.sync(self.repo)
        with graph.locked(self.repo) as value:
            kept = graph.add_insight(value, "Knowledge that outlives a deleted database", "decision", source="user")["id"]
            graph.refine(value, self.repo)
        for leftover in (self.repo / ".improve").glob("graph.db*"):
            leftover.unlink()
        graph.sync(self.repo)
        value = load(self.repo)
        self.assertIn(kept, value["nodes"])
        self.assertEqual(value["refine"]["recovered"]["detail"], "backup (graph.db was missing)")

    def test_damage_without_a_usable_checkpoint_rebuilds_from_ledgers(self):
        graph.sync(self.repo, refine_after=True)
        path = graph.graph_path(self.repo)
        path.write_bytes(b"garbage")
        graph.backup_path(path).write_bytes(b"also garbage")
        graph.sync(self.repo)
        value = load(self.repo)
        self.assertIn("task:live:t", value["nodes"])
        self.assertEqual(value["refine"]["recovered"]["from"], "rebuild")

    def test_busy_is_not_mistaken_for_damage(self):
        graph.sync(self.repo)
        self.assertFalse(store_module.is_damage(sqlite3.OperationalError("database is locked")))
        self.assertTrue(store_module.is_damage(sqlite3.DatabaseError("file is not a database")))
        self.assertFalse(store_module.is_damage(sqlite3.OperationalError("no such table: node")))


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve graph concurrency ")
        self.previous = environment(Path(self.temp.name) / "home")
        self.repo = make_repo(Path(self.temp.name) / "repo", ["src/a.py"])
        write_run(self.repo / ".improve", "live")
        write_ledger(self.repo / ".improve", [dict(id="t", title="Task", status="done", file="src/a.py")])
        graph.sync(self.repo)

    def tearDown(self):
        restore_environment(self.previous)
        self.temp.cleanup()

    def test_readers_never_wait_for_a_writer_and_see_the_last_commit(self):
        holding, release = threading.Event(), threading.Event()
        before = load(self.repo)["revision"]

        def writer():
            with graph.locked(self.repo) as value:
                graph.add_insight(value, "Written while a reader is reading", "fact")
                holding.set()
                release.wait(10)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            self.assertTrue(holding.wait(10))
            started = time.perf_counter()
            with graph.reading(self.repo) as part:
                graph.load_search(part, "written reader")
                self.assertEqual(graph.search(part, "written reader"), [], "uncommitted work is invisible")
            self.assertEqual(load(self.repo)["revision"], before)
            self.assertLess(time.perf_counter() - started, budget(1.0), "reads do not wait for the writer")
        finally:
            release.set()
            thread.join(10)
        self.assertEqual(load(self.repo)["revision"], before + 1)

    def test_busy_writers_cannot_starve_others(self):
        # Realistic sessions: each busy writer loads and saves a graph of hundreds of tasks.
        write_ledger(self.repo / ".improve", [dict(id=f"t{index}", title="Task", status="done", file="src/a.py")
                                              for index in range(400)])
        graph.sync(self.repo)
        stop, waits = threading.Event(), []

        def hammer(worker):
            count = 0
            while not stop.is_set():
                with graph.locked(self.repo) as value:
                    for item in range(5):
                        graph.add_insight(value, f"Hammer {worker} note {count} part {item} token h{worker}x{count}y{item}",
                                          "fact")
                count += 1

        threads = [threading.Thread(target=hammer, args=(worker,)) for worker in range(3)]
        for thread in threads:
            thread.start()
        try:
            for index in range(20):
                started = time.perf_counter()
                with graph.locked(self.repo, partial=True) as value:
                    graph.add_insight(value, f"Patient writer note {index} with token p{index}", "fact")
                waits.append(time.perf_counter() - started)
        finally:
            stop.set()
            for thread in threads:
                thread.join(30)
        # Writers take turns: without the queue, SQLite's busy-retry let waits reach seconds here.
        self.assertLess(max(waits), budget(0.5), f"a write waited {max(waits):.2f}s behind busy writers")
        self.assertEqual(sum(node.get("summary", "").startswith("Patient") for node in load(self.repo)["nodes"].values()), 20)


class HistoryAndDiskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve graph history ")
        self.previous = environment(Path(self.temp.name) / "home")
        self.repo = make_repo(Path(self.temp.name) / "repo", ["src/a.py"])
        write_run(self.repo / ".improve", "live")

    def tearDown(self):
        restore_environment(self.previous)
        self.temp.cleanup()

    def test_history_is_compressed_ordered_and_bounded(self):
        with graph.locked(self.repo) as value:
            ids = [graph.add_insight(value, f"Disposable note {index} about subsystem s{index}x", "fact")["id"]
                   for index in range(80)]
        with mock.patch.object(store_module, "MAX_HISTORY", 50), graph.locked(self.repo) as value:
            for node_id in ids:
                graph.remove_node(value, node_id, reason=f"cleanup of {node_id}", op="forgotten")
        result = graph.history(self.repo, 100)
        self.assertEqual(result["total"], 50)
        self.assertEqual([row["id"] for row in result["rows"]], list(reversed(ids))[:50], "newest first, oldest dropped")
        self.assertTrue(all(row["reason"] == f"cleanup of {row['id']}" for row in result["rows"]))
        with contextlib.closing(sqlite3.connect(str(graph.graph_path(self.repo)))) as connection:
            raw = connection.execute("SELECT typeof(body) FROM history LIMIT 1").fetchone()
        self.assertEqual(raw[0], "blob")

    def test_history_filters(self):
        with graph.locked(self.repo) as value:
            keep = graph.add_insight(value, "Billing retries need an idempotency key", "technique")["id"]
            drop = graph.add_insight(value, "Search index rebuilds take ten minutes", "fact")["id"]
            graph.upsert(value, "gate:slow", "gate", "slow")
        with graph.locked(self.repo) as value:
            graph.remove_nodes(value, [keep, drop, "gate:slow"], reason="test", op="forgotten")
        self.assertEqual([row["id"] for row in graph.history(self.repo, query="billing")["rows"]], [keep])
        self.assertEqual([row["id"] for row in graph.history(self.repo, types=["gate"])["rows"]], ["gate:slow"])
        self.assertEqual([row["id"] for row in graph.history(self.repo, node_id=drop)["rows"]], [drop])
        with graph.locked(self.repo) as value:
            graph.restore_node(value, keep)
        self.assertNotIn(keep, [row["id"] for row in graph.history(self.repo)["rows"]])
        self.assertIn(keep, [row["id"] for row in graph.history(self.repo, include_restored=True)["rows"]])

    def test_freed_pages_return_to_the_disk_after_a_checkpoint(self):
        with graph.locked(self.repo) as value:
            for index in range(3000):
                graph.upsert(value, f"file:bulk/{index}.py", "file", f"bulk/{index}.py", summary="x" * 300)
        with graph.locked(self.repo) as value:
            graph.remove_nodes(value, [key for key in value["nodes"] if key.startswith("file:bulk/")], reason="bulk")
            graph.refine(value, self.repo)
        connection = sqlite3.connect(str(graph.graph_path(self.repo)))
        self.assertEqual(connection.execute("PRAGMA auto_vacuum").fetchone()[0], 2)
        self.assertEqual(connection.execute("PRAGMA freelist_count").fetchone()[0], 0)
        connection.close()

    def test_a_database_without_incremental_vacuum_is_converted(self):
        graph.sync(self.repo)
        path = graph.graph_path(self.repo)
        connection = sqlite3.connect(str(path), isolation_level=None)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA auto_vacuum=NONE")
        connection.execute("VACUUM")
        self.assertEqual(connection.execute("PRAGMA auto_vacuum").fetchone()[0], 0)
        connection.close()
        with graph.locked(self.repo) as value:
            graph.refine(value, self.repo)
        with contextlib.closing(sqlite3.connect(str(path))) as connection:
            self.assertEqual(connection.execute("PRAGMA auto_vacuum").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
