"""The knowledge graph across a long session: reconciliation, pruning, refinement schedule,
recovery, concurrency, a 48-hour soak, and bounds at scale. No agents are launched.

IMPROVE_GRAPH_SOAK_CYCLES (default 288: 48 hours of 10-minute cycles) scales the soak.
"""
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_support import (ROOT, LongSession, archive, check_invariants, check_matches_ledger, copy_store,  # noqa: E402
                           check_disk, environment, load, make_repo, restore_environment, write_ledger, write_run)
import improve_graph as graph  # noqa: E402
from improve_runtime import stamp  # noqa: E402

SOAK_CYCLES = int(os.environ.get("IMPROVE_GRAPH_SOAK_CYCLES", "288"))
REFINE_EVERY = 3  # Cycles between refinements: 30 minutes at 10-minute cycles.


class GraphTestCase(unittest.TestCase):
    files = ["src/a.py", "src/b.py", "src/c.py", "src/d.py"]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve graph lifecycle ")
        self.previous = environment(Path(self.temp.name) / "home")
        self.repo = make_repo(Path(self.temp.name) / "repo", self.files).resolve()
        self.state = self.repo / ".improve"
        write_run(self.state, "live", {"py.fast": 5})

    def tearDown(self):
        restore_environment(self.previous)
        self.temp.cleanup()

    def sync(self, rows, **kwargs):
        write_ledger(self.state, rows)
        return graph.sync(self.repo, **kwargs)

    def links(self, node_id, rel):
        return {edge["dst"] for edge in load(self.repo)["edges"].values() if edge["src"] == node_id and edge["rel"] == rel}


class ReconciliationTests(GraphTestCase):
    def test_ledger_edits_update_and_delete_links(self):
        task = dict(id="t", title="T", area="ui", status="blocked", paths=["src/a.py", "src/b.py"],
                    blocked_reason="Gate 4 down", verification="py.fast ok")
        self.sync([task])
        self.assertTrue(self.links("task:live:t", "blocked_by"))
        self.assertEqual(self.links("task:live:t", "verified_by"), {"gate:py.fast"})
        task.update(status="done", paths=["src/c.py"], area="security", verification="manual check")
        task.pop("blocked_reason")
        self.sync([task])
        self.assertEqual(self.links("task:live:t", "blocked_by"), set(), "unblocking removes the blocker link")
        self.assertEqual(self.links("task:live:t", "touches"), {"file:src/c.py"}, "old files are unlinked")
        self.assertEqual(self.links("task:live:t", "in_area"), {"area:security"})
        self.assertEqual(self.links("task:live:t", "verified_by"), set())
        check_matches_ledger(self, load(self.repo), "live", [task], self.repo)

    def test_citations_follow_graph_refs(self):
        with graph.locked(self.repo) as value:
            first = graph.add_insight(value, "Warm the cache before timing anything", "technique")["id"]
            second = graph.add_insight(value, "The fixture server needs port 9000 free", "pitfall")["id"]
        task = dict(id="t", title="T", status="done", graph_refs=[first, second, "insight:missing", 7])
        self.sync([task])
        cited = {edge["src"] for edge in load(self.repo)["edges"].values() if edge["rel"] == "used_in"}
        self.assertEqual(cited, {first, second})
        task["graph_refs"] = [second]
        self.sync([task])
        cited = {edge["src"] for edge in load(self.repo)["edges"].values() if edge["rel"] == "used_in"}
        self.assertEqual(cited, {second})

    def test_tasks_that_leave_the_ledger_are_removed(self):
        rows = [dict(id=str(index), title=f"T{index}", status="done", file="src/a.py") for index in range(5)]
        self.sync(rows)
        self.sync(rows[1:])
        self.assertNotIn("task:live:0", load(self.repo)["nodes"])
        check_matches_ledger(self, load(self.repo), "live", rows[1:], self.repo)

    def test_unreadable_ledgers_never_cause_mass_deletion(self):
        rows = [dict(id=str(index), title=f"T{index}", status="done") for index in range(5)]
        self.sync(rows)
        ledger = self.state / "backlog.jsonl"
        for damage in ("empty", "garbage", "missing", "symlink"):
            with self.subTest(damage=damage):
                ledger.unlink(missing_ok=True)
                if damage == "empty":
                    ledger.write_text("")
                elif damage == "garbage":
                    ledger.write_text("{not json\n[1,2]\n")
                elif damage == "symlink":
                    target = Path(self.temp.name) / "elsewhere.jsonl"
                    target.write_text(json.dumps(dict(id="x", status="done")) + "\n")
                    ledger.symlink_to(target)
                graph.sync(self.repo)
                tasks = {node["task_id"] for node in load(self.repo)["nodes"].values() if node["type"] == "task"}
                self.assertEqual(tasks, {str(index) for index in range(5)})
                ledger.unlink(missing_ok=True)

    def test_an_unchanged_ledger_is_a_no_op_write(self):
        rows = [dict(id="t", title="T", status="done", paths=["src/a.py", "src/b.py"])]
        self.sync(rows, refine_after=True)
        before = load(self.repo)
        wal = Path(str(graph.graph_path(self.repo)) + "-wal")
        wal_size = wal.stat().st_size if wal.exists() else 0
        # Advance the clock on every call so a needless timestamp rewrite cannot hide in one second.
        ticks = iter(range(10 ** 6))
        with mock.patch.object(graph, "stamp", lambda value=None: stamp(time.time() + next(ticks))):
            for _ in range(3):
                graph.sync(self.repo)
        after = load(self.repo)
        self.assertEqual((before["revision"], before.get("updated_at")), (after["revision"], after.get("updated_at")),
                         "the board only refetches when something actually changed")
        self.assertEqual(wal.stat().st_size if wal.exists() else 0, wal_size, "nothing was written at all")

    def test_ingest_tolerates_hostile_task_fields(self):
        rows = [dict(id="a", title=None, area="Not An Area!", status=7, paths="src/a.py"),
                dict(id="b", title="x" * 5000, paths=[None, 3, {"x": 1}, "src/b.py", "../../etc"], file=["bad"]),
                dict(id="c", claim="only a claim", graph_refs="not-a-list", verification={"py.fast": "ok"}),
                dict(id="d", status="blocked", blocked_reason="x" * 2000)]
        self.sync(rows, refine_after=True)
        value = load(self.repo)
        check_invariants(self, value, refined=True)
        self.assertEqual(value["nodes"]["task:live:a"]["area"], "general")
        self.assertEqual(value["nodes"]["task:live:a"]["status"], "ready")
        self.assertLessEqual(len(value["nodes"]["task:live:b"]["label"]), graph.MAX_LABEL)
        self.assertEqual(self.links("task:live:b", "touches"), {"file:src/b.py"})
        self.assertEqual(self.links("task:live:c", "verified_by"), {"gate:py.fast"})
        self.assertTrue(all(len(node["label"]) <= 300 for node in value["nodes"].values() if node["type"] == "blocker"))


class PruningTests(GraphTestCase):
    def test_pruned_live_tasks_are_never_counted_twice(self):
        rand = random.Random(3)
        rows = []
        expected = {path: 0 for path in self.files}
        with mock.patch.object(graph, "MAX_TASKS", 25):
            for cycle in range(12):
                for _ in range(6):
                    path = rand.choice(self.files)
                    rows.append(dict(id=f"t{len(rows)}", title="T", area="quality", status="done", file=path))
                    expected[path] += 1
                self.sync(rows, refine_after=cycle % 2 == 1)
            graph.sync(self.repo, refine_after=True)
            value = load(self.repo)
            check_invariants(self, value, refined=True)
            for path, count in expected.items():
                self.assertEqual(value["nodes"]["file:" + path]["stats"]["done"], count, path)
            self.assertEqual(value["nodes"]["area:quality"]["stats"]["done"], len(rows))
            pruned = next(row for row in value["runs"] if row["run_id"] == "live")["pruned"]
            self.assertEqual(len(pruned), len(rows) - 25)
            check_matches_ledger(self, value, "live", rows, self.repo, pruned=pruned)

    def test_pruning_is_oldest_finished_first_and_keeps_open_work(self):
        archive(self.repo, "old", "r-old", [dict(id=str(index), title="Old", status="done") for index in range(10)])
        rows = [dict(id=f"open{index}", title="Open", status="ready") for index in range(5)]
        with mock.patch.object(graph, "MAX_TASKS", 8):
            self.sync(rows, refine_after=True)
        tasks = [node for node in load(self.repo)["nodes"].values() if node["type"] == "task"]
        self.assertEqual(len(tasks), 8)
        self.assertEqual(sum(node["run_id"] == "live" for node in tasks), 5, "open live work outlives old history")

    def test_orphans_and_size_caps(self):
        rows = [dict(id=str(index), title="T", status="blocked", blocked_reason=f"unique reason {chr(97 + index % 26)}{index // 26}x")
                for index in range(60)]
        with mock.patch.object(graph, "MAX_BLOCKERS", 20):
            self.sync(rows, refine_after=True)
            self.assertLessEqual(sum(node["type"] == "blocker" for node in load(self.repo)["nodes"].values()), 20)
        self.sync([dict(id="0", title="T", status="done")], refine_after=True)
        value = load(self.repo)
        self.assertEqual([node for node in value["nodes"].values() if node["type"] == "blocker"], [],
                         "blockers nothing points to any more are removed")
        check_invariants(self, value, refined=True)


class RefineScheduleTests(GraphTestCase):
    def test_refine_due_rules(self):
        value = graph.empty_graph()
        self.assertFalse(graph.refine_due(value, 60), "nothing to refine")
        graph.upsert(value, "area:ui", "area", "ui")
        self.assertTrue(graph.refine_due(value, 60), "never refined")
        for interval in (None, 0, -5):
            self.assertFalse(graph.refine_due(value, interval))
        value["refine"]["last_at"] = stamp(time.time() - 30)
        self.assertFalse(graph.refine_due(value, 60))
        value["refine"]["last_at"] = stamp(time.time() - 61)
        self.assertTrue(graph.refine_due(value, 60))
        value["refine"]["last_at"] = "yesterday"
        self.assertTrue(graph.refine_due(value, 60), "an unreadable time is treated as overdue")

    def test_sync_refines_on_schedule_and_keeps_stats_current(self):
        rows = [dict(id="a", title="A", status="rejected", file="src/a.py")]
        self.assertIsNotNone(self.sync(rows, refine_interval=1800)["refine"])
        rows.append(dict(id="b", title="B", status="done", file="src/a.py"))
        self.assertIsNone(self.sync(rows, refine_interval=1800)["refine"], "not due yet")
        with graph.locked(self.repo) as value:
            value["refine"]["last_at"] = stamp(time.time() - 1801)
        self.assertIsNotNone(self.sync(rows, refine_interval=1800)["refine"])
        self.assertEqual(load(self.repo)["nodes"]["file:src/a.py"]["stats"]["attempts"], 2)

    def test_refine_is_idempotent(self):
        rows = [dict(id=str(index), title="T", status=("done", "rejected")[index % 2], paths=self.files[: index % 4 + 1])
                for index in range(20)]
        self.sync(rows, refine_after=True)
        with graph.locked(self.repo) as value:
            graph.add_insight(value, "Twin knowledge about gate speed and caching", "fact")
            graph.add_insight(value, "Twin knowledge about the gate speed and caching", "pitfall")
        graph.sync(self.repo, refine_after=True)
        strip = lambda value: {key: item for key, item in value.items() if key not in ("refine", "revision", "updated_at")}
        first = strip(load(self.repo))
        graph.sync(self.repo, refine_after=True)
        self.assertEqual(strip(load(self.repo)), first)


class RecoveryTests(GraphTestCase):
    def test_damaged_graph_recovers_from_the_last_refined_backup(self):
        rows = [dict(id="t", title="T", status="done", file="src/a.py")]
        self.sync(rows)
        with graph.locked(self.repo) as value:
            kept = graph.add_insight(value, "User decision: never touch the billing module", "decision", source="user")["id"]
            graph.refine(value, self.repo)
        self.assertTrue(graph.backup_path(graph.graph_path(self.repo)).exists())
        rows.append(dict(id="u", title="U", status="done", file="src/b.py"))
        write_ledger(self.state, rows)
        graph.graph_path(self.repo).write_text('{"nodes": [truncated')
        self.assertEqual(load(self.repo)["nodes"], {}, "readers never repair or guess")
        graph.sync(self.repo)
        value = load(self.repo)
        self.assertIn(kept, value["nodes"], "knowledge survives a damaged graph")
        self.assertIn("task:live:u", value["nodes"], "newer ledger work is re-ingested")
        self.assertEqual(value["refine"]["recovered"]["from"], "backup")
        self.assertEqual(graph.damaged_path(graph.graph_path(self.repo)).read_text(), '{"nodes": [truncated')
        check_invariants(self, value)

    def test_damaged_backup_falls_back_to_a_rebuild(self):
        archive(self.repo, "old", "r-old", [dict(id="h", title="H", status="done", file="src/c.py")])
        self.sync([dict(id="t", title="T", status="done")], refine_after=True)
        graph.graph_path(self.repo).write_text("[]")
        graph.backup_path(graph.graph_path(self.repo)).write_text("{bad")
        graph.sync(self.repo)
        nodes = load(self.repo)["nodes"]
        self.assertIn("task:live:t", nodes)
        self.assertIn("task:r-old:h", nodes, "archived runs are backfilled into a rebuilt graph")

    def test_symlinked_store_is_refused(self):
        target = Path(self.temp.name) / "outside.db"
        target.write_text("{}")
        graph.graph_path(self.repo).symlink_to(target)
        with self.assertRaises(graph.GraphError):
            graph.sync(self.repo)
        self.assertEqual(target.read_text(), "{}")
        graph.graph_path(self.repo).unlink()
        graph.backup_path(graph.graph_path(self.repo)).symlink_to(target)
        with self.assertRaises(graph.GraphError):
            graph.sync(self.repo)
        self.assertEqual(target.read_text(), "{}", "a symlinked checkpoint is never written through")


class ConcurrencyTests(GraphTestCase):
    def test_parallel_writers_lose_nothing_and_readers_never_see_a_torn_file(self):
        rows = [dict(id=str(index), title=f"T{index}", status="done", file=self.files[index % 4]) for index in range(40)]
        self.sync(rows, refine_after=True)
        stop, torn, reads = threading.Event(), [], [0]

        def reader():
            # Every read is a whole, consistent revision: never a mix of two writers' changes.
            while not stop.is_set():
                try:
                    value = load(self.repo)
                    dangling = [key for key, edge in value["edges"].items()
                                if edge["src"] not in value["nodes"] or edge["dst"] not in value["nodes"]]
                    if dangling or (value["nodes"] and not value["revision"]):
                        torn.append(f"revision {value['revision']}: {dangling[:2]}")
                    reads[0] += 1
                except Exception as exc:  # Any failed read is the finding.
                    torn.append(repr(exc))

        def daemon():
            cycle = 0
            while not stop.is_set():
                rows.append(dict(id=f"d{cycle}", title="D", status="done", file=self.files[cycle % 4]))
                write_ledger(self.state, rows)
                graph.sync(self.repo, refine_after=cycle % 4 == 3)
                cycle += 1

        threads = [threading.Thread(target=reader), threading.Thread(target=daemon)]
        for thread in threads:
            thread.start()
        script = [sys.executable, str(ROOT / "scripts/improve_graph.py"), "--repo", str(self.repo)]
        commands = [[*script, "note", "--kind", "fact", "--about", f"src/{'abcd'[writer]}.py", "--text",
                     f"Writer {writer} records fact {index} on subsystem s{writer}x{index} using token k{writer}q{index}"]
                    for writer in range(4) for index in range(8)]
        commands += [[*script, "rate", "area:general", "pin"], [*script, "refine"]]
        try:
            processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                         for command in commands]
            results = [(process.wait(timeout=120), *process.communicate()) for process in processes]
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=60)
        self.assertEqual([code for code, _, err in results if code], [], [err for code, _, err in results if code])
        ids = {json.loads(out)["id"] for code, out, _ in results[:32]}
        value = load(self.repo)
        self.assertEqual(len(ids), 32)
        self.assertTrue(ids <= set(value["nodes"]), "every concurrent note survived")
        self.assertTrue(value["nodes"]["area:general"]["pinned"])
        self.assertEqual(torn, [])
        self.assertGreater(reads[0], 0)
        check_invariants(self, value)
        check_matches_ledger(self, value, "live", rows, self.repo)


class LongSessionSoakTests(unittest.TestCase):
    """A compressed 48-hour run with history, checked after every single cycle."""

    def test_forty_eight_hour_session_stays_accurate_bounded_and_fast(self):
        # Size the cap so history is pruned midway through any soak length, live work never.
        cap = min(graph.MAX_TASKS, 900 + SOAK_CYCLES * 2)
        with tempfile.TemporaryDirectory(prefix="improve graph soak ") as directory, \
                mock.patch.object(graph, "MAX_TASKS", cap):
            previous = environment(Path(directory) / "home")
            try:
                self.soak(Path(directory))
            finally:
                restore_environment(previous)

    def soak(self, directory):
        files = [f"pkg{index // 40}/mod{index}.py" for index in range(400)]
        repo = make_repo(directory / "repo", files).resolve()
        rand = random.Random(11)
        history = {}
        for run in range(6):
            rows = [dict(id=f"h{index}", title=f"History {run}-{index}", area=rand.choice(LongSession.AREAS),
                         status=rand.choice(("done", "rejected", "blocked")), blocked_reason="Device farm offline",
                         paths=rand.sample(files[:300], rand.randint(1, 3))) for index in range(150)]
            archive(repo, f"2026-10-{run:02d}", f"h{run}", rows, gates={"py.fast": 10})
            history[f"h{run}"] = rows
        session = LongSession(repo, files)
        worst = {"sync": 0.0, "refine": 0.0, "query": 0.0}
        refines = 0
        for cycle in range(SOAK_CYCLES):
            session.step(cycle)
            started = time.perf_counter()
            result = graph.sync(repo, refine_after=cycle % REFINE_EVERY == REFINE_EVERY - 1)
            elapsed = time.perf_counter() - started
            worst["refine" if result["refine"] else "sync"] = max(worst["refine" if result["refine"] else "sync"], elapsed)
            refines += bool(result["refine"])
            value = load(repo)
            check_invariants(self, value, refined=bool(result["refine"]))
            pruned = next(row for row in value["runs"] if row["run_id"] == "live").get("pruned", [])
            check_matches_ledger(self, value, "live", session.ledger_rows(), repo, pruned=pruned)
            for vanished in session.vanished:
                self.assertNotIn(f"task:live:{vanished}", value["nodes"])
            worst["query"] = max(worst["query"], self.query_everything(value, repo, session, rand))
        graph.sync(repo, refine_after=True)
        value = load(repo)
        check_invariants(self, value, refined=True)
        self.assertEqual(refines, SOAK_CYCLES // REFINE_EVERY)
        history_pruned = sum(len(row.get("pruned", [])) for row in value["runs"] if row["run_id"] != "live")
        self.assertGreater(history_pruned, 0, "the soak must exercise pruning")
        self.assertEqual(next(row for row in value["runs"] if row["run_id"] == "live").get("pruned", []), [])
        self.assert_exact_statistics(value, repo, history, session)
        for name in session.removed_files:
            if f"file:{name}" in value["nodes"]:
                self.assertTrue(value["nodes"][f"file:{name}"]["stale"], name)
        check_disk(self, repo)
        # Generous ceilings: they catch quadratic regressions, not machine speed.
        self.assertLess(worst["sync"], 2.0, worst)
        self.assertLess(worst["refine"], 5.0, worst)
        self.assertLess(worst["query"], 0.75, worst)

    def query_everything(self, value, repo, session, rand):
        started = time.perf_counter()
        sample = rand.sample(session.files[:300], 3)
        result = graph.context(value, sample, rand.choice(LongSession.AREAS), "gate serially", repo=repo)
        statuses = {row["id"]: row.get("status") for row in result["past_tasks"]}
        for node_id, status in statuses.items():
            run_id, task_id = node_id.split(":")[1:3]
            if run_id == "live":
                self.assertEqual(status, session.rows[task_id]["status"], "context reports current outcomes")
        self.assertTrue(all(not row.get("archived") for row in result["insights"]))
        for hit in graph.search(value, "module pitfall serially"):
            self.assertIn(hit["id"], value["nodes"])
        view = graph.board_view(value, limit=220)
        self.assertLessEqual(len(view["nodes"]), 220)
        if view["nodes"]:
            focus = rand.choice(view["nodes"])["id"]
            self.assertIn(focus, {node["id"] for node in graph.board_view(value, focus=focus, depth=2)["nodes"]})
            graph.node_detail(value, focus)
            graph.shortest_path(value, focus, view["nodes"][0]["id"])
        self.assertLessEqual(len(graph.brief(value, "graph.py", "/repo")), 1800)
        graph.hotspots(value)
        return time.perf_counter() - started

    def assert_exact_statistics(self, value, repo, history, session):
        """Every finished outcome is counted exactly once, including pruned history."""
        expected_files, expected_areas = {}, {}
        all_rows = [row for rows in history.values() for row in rows] + session.ledger_rows()
        for row in all_rows:
            if row["status"] not in graph.FINISHED:
                continue
            for path in graph.task_files(row, repo):
                expected_files[path] = expected_files.get(path, 0) + 1
            expected_areas[row["area"]] = expected_areas.get(row["area"], 0) + 1
        for path, count in expected_files.items():
            node = value["nodes"].get("file:" + path)
            if node is not None:
                self.assertEqual(node["stats"]["attempts"], count, path)
        for area, count in expected_areas.items():
            self.assertEqual(value["nodes"]["area:" + area]["stats"]["attempts"], count, area)


class ScaleTests(unittest.TestCase):
    """A repository with a long history: 20 runs of 300 tasks, 3,000 files, 400 notes."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="improve graph scale ")
        cls.previous = environment(Path(cls.temp.name) / "home")
        cls.files = [f"src/m{index // 50}/f{index}.py" for index in range(3000)]
        cls.repo = make_repo(Path(cls.temp.name) / "repo", cls.files).resolve()
        rand = random.Random(5)
        cls.total_done = 0
        for run in range(20):
            rows = []
            for index in range(300):
                status = rand.choice(("done", "done", "rejected", "blocked"))
                cls.total_done += status == "done"
                rows.append(dict(id=f"t{index}", title=f"Task {index} of run {run}", area=rand.choice(LongSession.AREAS),
                                 status=status, blocked_reason=f"Gate {index} timed out",
                                 paths=rand.sample(cls.files[:900], rand.randint(1, 5)), verification="py.fast ok"))
            archive(cls.repo, f"2026-09-{run:02d}", f"r{run}", rows, gates={"py.fast": 9, "ts.fast": 31})
        write_run(cls.repo / ".improve", "live")
        write_ledger(cls.repo / ".improve", [dict(id="x", title="Live", status="ready", file=cls.files[0])])
        started = time.perf_counter()
        graph.sync(cls.repo)
        cls.backfill_seconds = time.perf_counter() - started
        started = time.perf_counter()
        graph.sync(cls.repo, refine_after=True)
        cls.first_refine_seconds = time.perf_counter() - started
        with graph.locked(cls.repo) as value:
            for index in range(450):
                graph.add_insight(value, f"Module {index} needs fixture z{index} and flag f{index % 7} set",
                                  ("pitfall", "technique", "fact")[index % 3], about=[cls.files[index]], repo=cls.repo)
        graph.sync(cls.repo, refine_after=True)
        cls.value = load(cls.repo)

    @classmethod
    def tearDownClass(cls):
        restore_environment(cls.previous)
        cls.temp.cleanup()

    def test_large_history_is_bounded_consistent_and_conserved(self):
        check_invariants(self, self.value, refined=True)
        self.assertLess(self.backfill_seconds, 10)
        self.assertLess(self.first_refine_seconds, 10, "pruning thousands of tasks stays linear")
        done = sum(node["stats"]["done"] for node in self.value["nodes"].values() if node["type"] == "area")
        self.assertEqual(done, self.total_done, "pruned outcomes are folded into area baselines exactly once")
        self.assertLessEqual(graph.summary(self.value)["insights"], graph.MAX_INSIGHTS)
        check_disk(self, self.repo)

    def test_every_query_is_fast_at_scale(self):
        value, files = self.value, self.files
        budget = {
            "context file": lambda: graph.context(value, files[:5], "ui", repo=self.repo),
            "context directory": lambda: graph.context(value, ["src/m3"], repo=self.repo),
            "context text": lambda: graph.context(value, text="fixture flag needs"),
            "search": lambda: graph.search(value, "task run timed out", limit=50),
            "search typed": lambda: graph.search(value, "module fixture", types=("insight",)),
            "node busy area": lambda: graph.node_detail(value, "area:ui"),
            "path": lambda: graph.shortest_path(value, "area:ui", "gate:py.fast"),
            "hotspots": lambda: graph.hotspots(value, 50),
            "brief": lambda: graph.brief(value, "graph.py", "/repo"),
            "board default": lambda: graph.board_view(value),
            "board max": lambda: graph.board_view(value, limit=800, include_archived=True),
            "board focus": lambda: graph.board_view(value, focus="area:ui", depth=3, limit=800),
            "board query": lambda: graph.board_view(value, query="timed out", limit=800),
        }
        for name, query in budget.items():
            with self.subTest(query=name):
                started = time.perf_counter()
                result = query()
                self.assertLess(time.perf_counter() - started, 0.5, name)
                self.assertTrue(result or name == "path")
        detail = graph.node_detail(value, "area:ui")
        self.assertEqual(len(detail["links"]), 200)
        self.assertGreater(detail["total_links"], 200)

    def test_board_payloads_stay_small(self):
        for kwargs in ({}, {"limit": 800}, {"focus": "area:ui", "depth": 3, "limit": 800}):
            with self.subTest(kwargs=kwargs):
                view = graph.board_view(self.value, **kwargs)
                self.assertLessEqual(len(view["edges"]), max(10, min(kwargs.get("limit", 250), 800)) * 4)
                self.assertLess(len(json.dumps(view)), 2 * 1024 * 1024)

    def test_steady_state_refine_and_sync_are_quick(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = make_repo(Path(directory), [])
            copy_store(self.repo, copy)
            write_run(copy / ".improve", "live")
            write_ledger(copy / ".improve", [dict(id="x", title="Live", status="ready")])
            for label, kwargs, ceiling in (("sync", {}, 2.0), ("refine", {"refine_after": True}, 5.0)):
                started = time.perf_counter()
                graph.sync(copy, **kwargs)
                self.assertLess(time.perf_counter() - started, ceiling, label)


if __name__ == "__main__":
    unittest.main()
