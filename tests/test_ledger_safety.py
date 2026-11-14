"""Ledger writes that do not lose data: citations accumulate, concurrent writers queue on a
lock, damaged and id-less lines survive, and starting a task without a graph lookup earns an
advisory hint (review 2026-11-10, sections 2.5, 2.6, 3.3, and Graphify's always-on nudge)."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from support import budget  # noqa: E402
import improve_graph_journal as journal  # noqa: E402
import improve_ledger as ledger  # noqa: E402
import improve_resilience as resilience  # noqa: E402
import improve_toon as toon  # noqa: E402


class LedgerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve ledger safety ")
        self.repo = Path(self.temp.name)
        self.state = self.repo / ".improve"
        self.state.mkdir()
        rows = [{"id": "q-1", "status": "ready", "area": "quality", "title": "A", "file": "src/a.py"},
                {"id": "q-2", "status": "ready", "area": "quality", "title": "B", "paths": ["src/b.py"]}]
        self.path = self.state / "backlog.jsonl"
        self.path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/improve_ledger.py"), "--repo", str(self.repo), *args],
                                capture_output=True, text=True, timeout=budget(60))
        self.assertEqual(result.returncode, 0, result.stderr)
        return toon.loads(result.stdout)

    def tasks(self):
        return resilience.merged_tasks(self.state)

    def test_should_add_graph_refs_instead_of_replacing_them(self):
        self.cli("set", "q-1", "--graph-ref", "insight:aaa")
        self.cli("set", "q-1", "--graph-ref", "insight:bbb", "--graph-ref", "insight:aaa")
        self.assertEqual(self.tasks()["q-1"]["graph_refs"], ["insight:aaa", "insight:bbb"])
        self.cli("set", "q-1", "--graph-ref", "insight:ccc", "--clear-graph-refs")
        self.assertEqual(self.tasks()["q-1"]["graph_refs"], ["insight:ccc"])

    def test_should_keep_damaged_and_idless_lines_in_place(self):
        with self.path.open("a") as stream:
            stream.write('{"id": "q-3", "status": "re\n')
            stream.write('{"note": "a row with no id"}\n')
            stream.write('{"id": "q-4", "status": "ready"}\n')
        self.cli("set", "q-2", "--status", "in_progress")
        lines = self.path.read_text().splitlines()
        self.assertIn('{"id": "q-3", "status": "re', lines)
        self.assertIn('{"note": "a row with no id"}', lines)
        self.assertEqual(lines.index('{"id": "q-3", "status": "re'), 2, "kept where it was")
        self.assertEqual(self.tasks()["q-2"]["status"], "in_progress")
        self.assertEqual(self.tasks()["q-4"]["status"], "ready")

    def test_should_not_lose_updates_from_concurrent_writers(self):
        errors = []

        def writer(task_id, index):
            try:
                ledger.update_task(self.state, task_id, {"note": f"note {index}"}, append_refs=[f"insight:{index:03d}"])
            except Exception as exc:  # noqa: BLE001 - collected for the assertion below.
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=("q-1", index)) for index in range(12)]
        threads += [threading.Thread(target=resilience.block_active_item, args=(self.state, "simulator down"))]
        (self.state / "run.json").write_text(json.dumps({"active_item": {"id": "q-2"}}))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        tasks = self.tasks()
        self.assertEqual(len(tasks["q-1"]["graph_refs"]), 12, "every concurrent citation is kept")
        self.assertEqual(tasks["q-2"]["status"], "blocked", "the supervisor's write is not overwritten")
        self.assertEqual(len(self.path.read_text().splitlines()), 2, "one canonical row per id")

    def test_supervisor_waits_for_the_ledger_lock(self):
        done = threading.Event()
        (self.state / "run.json").write_text(json.dumps({"active_item": {"id": "q-1"}}))
        with resilience.ledger_lock(self.state):
            thread = threading.Thread(target=lambda: (resilience.block_active_item(self.state, "gate hung"), done.set()))
            thread.start()
            time.sleep(0.2)
            self.assertFalse(done.is_set(), "block_active_item waits for the agent's write to finish")
        thread.join(5)
        self.assertTrue(done.is_set())

    def test_should_never_cut_ids_or_paths_in_list_views(self):
        long_path = "src/" + "x" * 150 + ".py"
        ledger.update_task(self.state, "q-1", {"file": long_path, "claim": "y" * 300})
        rows = self.cli("queue", "--width", "30")["tasks"]
        row = next(row for row in rows if row["id"] == "q-1")
        self.assertEqual(row["file"], long_path)
        self.assertLessEqual(len(row["claim"]), 30)

    def test_should_hint_when_a_task_starts_without_a_graph_lookup(self):
        result = self.cli("set", "q-1", "--status", "in_progress")
        self.assertIn("context --files src/a.py", result["hint"])
        self.assertNotIn("hint", self.tasks()["q-1"], "the hint is advice, not ledger data")
        journal.record_reads(self.repo, [], ["src/b.py"])
        self.assertNotIn("hint", self.cli("set", "q-2", "--status", "in_progress"))

    def test_add_keeps_damaged_lines_and_refuses_duplicates(self):
        with self.path.open("a") as stream:
            stream.write("{broken\n")
        self.cli("add", "--json", json.dumps({"id": "q-9", "title": "new"}))
        self.assertIn("{broken", self.path.read_text())
        self.assertIn("q-9", self.tasks())
        duplicate = subprocess.run([sys.executable, str(ROOT / "scripts/improve_ledger.py"), "--repo", str(self.repo),
                                    "add", "--json", json.dumps({"id": "q-1"})], capture_output=True, text=True, timeout=budget(60))
        self.assertEqual(duplicate.returncode, 2)


if __name__ == "__main__":
    unittest.main()
