"""Cross-run learning store and recovery helpers, without launching agents."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from support import budget  # noqa: E402  (importing support also isolates git)

import improve_learning as learning  # noqa: E402
import improve_resilience as resilience  # noqa: E402


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve learning ")
        self.repo = Path(self.temp.name) / "repo"
        (self.repo / ".improve").mkdir(parents=True)
        self.home = os.environ.get("IMPROVE_LEARNING_HOME")
        os.environ["IMPROVE_LEARNING_HOME"] = str(Path(self.temp.name) / "home")

    def tearDown(self):
        if self.home is None:
            os.environ.pop("IMPROVE_LEARNING_HOME", None)
        else:
            os.environ["IMPROVE_LEARNING_HOME"] = self.home
        self.temp.cleanup()

    def test_record_run_is_idempotent_and_aggregates_areas_and_blockers(self):
        rows = [dict(id="a", area="ui", status="done"), dict(id="b", area="ui", status="rejected"),
                dict(id="c", area="security", status="ready"), dict(id="c", status="blocked", blocked_reason="Gate 42 timed out")]
        control = dict(run_id="r1", outcome="completed")
        learning.record_run(self.repo, control, rows)
        learning.record_run(self.repo, control, rows)
        value = learning.load(learning.store_path(self.repo))
        self.assertEqual(len(value["runs"]), 1)
        self.assertEqual(value["areas"]["ui"]["done"], 1)
        self.assertEqual(value["areas"]["security"]["blocked"], 1)
        learning.record_run(self.repo, dict(run_id="r2"), [dict(id="d", status="blocked", blocked_reason="Gate 7 timed out")])
        blockers = learning.load(learning.store_path(self.repo))["blockers"]
        self.assertEqual(blockers[0]["count"], 2, "numbers must not split recurring blockers")

    def test_guidance_needs_enough_samples_and_stays_bounded(self):
        self.assertEqual(learning.guidance(self.repo), "")
        learning.record_run(self.repo, dict(run_id="r1"), [dict(id=str(i), area="docs", status="done") for i in range(2)])
        self.assertNotIn("docs (", learning.guidance(self.repo))
        learning.record_run(self.repo, dict(run_id="r2"), [dict(id="x", area="docs", status="rejected")])
        self.assertIn("docs (67% of 3)", learning.guidance(self.repo))
        for index in range(40):
            learning.add_lesson(self.repo, f"Lesson number {index} about a specific build quirk to remember")
        self.assertLessEqual(len(learning.guidance(self.repo)), 1800)

    def test_lessons_validate_reinforce_and_keep_scopes_separate(self):
        with self.assertRaises(learning.StateError):
            learning.add_lesson(self.repo, "short")
        with self.assertRaises(learning.StateError):
            learning.add_lesson(self.repo, "x" * 401)
        learning.add_lesson(self.repo, "Use the serial test runner here.")
        lesson = learning.add_lesson(self.repo, "use the  SERIAL test runner here!")
        self.assertEqual(lesson["reinforced"], 2)
        learning.add_lesson(self.repo, "Atomic writes prevent torn ledgers.", scope="global")
        self.assertEqual(len(learning.load(learning.store_path(self.repo))["lessons"]), 1)
        self.assertIn("Atomic writes", learning.guidance(self.repo))

    def test_damaged_store_is_preserved_and_never_blocks_work(self):
        path = learning.store_path(self.repo)
        path.write_text("{not json")
        self.assertEqual(learning.guidance(self.repo), "")
        self.assertTrue(path.with_suffix(".damaged.json").exists())
        learning.add_lesson(self.repo, "Recovered stores keep working.")
        self.assertEqual(len(learning.load(path)["lessons"]), 1)

    def test_malformed_rows_are_skipped_instead_of_dropping_all_guidance(self):
        learning.store_path(self.repo).write_text(json.dumps({
            "lessons": [{"key": "k", "reinforced": 1}, "junk", {"text": 7},
                        {"key": "use the serial runner for integration tests",
                         "text": "Use the serial runner for integration tests.", "reinforced": "often"}],
            "blockers": [{"key": "b", "count": "x"}, {"key": "c", "count": 3, "example": "Simulator offline"}, None],
            "areas": {"ui": {"done": "3", "rejected": 1}, "docs": "broken"},
            "runs": [None]}))
        text = learning.guidance(self.repo)
        self.assertIn("Use the serial runner", text)
        self.assertIn("(3x) Simulator offline", text)
        learning.add_lesson(self.repo, "Use the serial runner for integration tests.")
        learning.record_run(self.repo, dict(run_id="r1"), [dict(id="a", area="ui", status="done")])
        value = learning.load(learning.store_path(self.repo))
        self.assertEqual(value["lessons"][0]["reinforced"], 2, "a malformed count restarts at its default")
        self.assertEqual(value["areas"]["ui"]["done"], 4)
        self.assertNotIn("docs", value["areas"])
        self.assertTrue(learning.board_summary(self.repo)["lessons"])

    def test_concurrent_lesson_writes_keep_every_lesson(self):
        for scope in learning.SCOPES:
            with self.subTest(scope=scope):
                errors = []

                def writer(worker):
                    try:
                        for index in range(8):
                            learning.add_lesson(self.repo, f"Lesson {worker}-{index} about a distinct build quirk.", scope=scope)
                    except Exception as exc:  # noqa: BLE001 - reported below
                        errors.append(exc)
                threads = [threading.Thread(target=writer, args=(worker,)) for worker in range(6)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(budget(30))
                self.assertEqual(errors, [])
                lessons = learning.load(learning.store_path(self.repo, scope))["lessons"]
                self.assertEqual(len(lessons), 48, "no read-modify-write lost another writer's lesson")


class RecoveryHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve recovery ")
        self.state = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_lane_rotation_prefers_focus_skips_blocked_and_rotates(self):
        (self.state / "discovery.jsonl").write_text("".join(json.dumps({"lane": lane}) + "\n" for lane in ("ui", "security")))
        focus = {"board_areas": {"prefer": ["security", "ui"], "skip": ["features"]}}
        self.assertEqual(resilience.suggest_lane(self.state, focus), "ui")  # Explored before security.
        self.assertNotEqual(resilience.suggest_lane(self.state, None), "ui")
        self.assertIsNone(resilience.suggest_lane(self.state, {"board_areas": {"skip": list(resilience.DISCOVERY_LANES)}}))

    def test_block_active_item_records_reason_and_clears_active(self):
        (self.state / "run.json").write_text(json.dumps({"active_item": {"id": "t", "title": "T", "area": "ui"}, "cycle": 2}))
        self.assertEqual(resilience.block_active_item(self.state, "Simulator unavailable"), "t")
        task = resilience.merged_tasks(self.state)["t"]
        self.assertEqual((task["status"], task["blocked_reason"], task["block_count"]), ("blocked", "Simulator unavailable", 1))
        run = json.loads((self.state / "run.json").read_text())
        self.assertNotIn("active_item", run)
        self.assertEqual(run["supervisor_blocks"][0]["task"], "t")
        self.assertIn("Simulator unavailable", resilience.recovery_brief(self.state))
        self.assertIn("ready queue is empty", resilience.recovery_brief(self.state))

    def test_repair_ledgers_moves_only_bad_lines(self):
        original = '{"id":"a"}\n{bad\n[1]\n{"id":"b"}\n'
        (self.state / "backlog.jsonl").write_text(original)
        problems = resilience.repair_ledgers(self.state)
        self.assertEqual(len(problems), 1)
        self.assertEqual([row["id"] for row in resilience.read_rows(self.state / "backlog.jsonl")], ["a", "b"])
        self.assertEqual((self.state / "backlog.jsonl.damaged").read_text(), "{bad\n[1]\n")
        self.assertEqual((self.state / "backlog.jsonl.bak").read_text(), original, "the rewritten bytes are kept")
        self.assertEqual(resilience.repair_ledgers(self.state), [])

    def test_repair_ledgers_waits_for_the_ledger_lock(self):
        (self.state / "backlog.jsonl").write_bytes(b'{"id":"a"}\n\xff\xfe{"id":"b"}\n')
        finished = threading.Event()
        with resilience.ledger_lock(self.state):
            thread = threading.Thread(target=lambda: (resilience.repair_ledgers(self.state), finished.set()))
            thread.start()
            self.assertFalse(finished.wait(0.3), "repair must not rewrite a ledger while a writer holds the lock")
            self.assertIn(b"\xff", (self.state / "backlog.jsonl").read_bytes())
        thread.join(budget(10))
        self.assertTrue(finished.is_set())
        self.assertNotIn(b"\xff", (self.state / "backlog.jsonl").read_bytes())
        self.assertIn(b"\xff", (self.state / "backlog.jsonl.bak").read_bytes(), "undecodable bytes survive in the backup")

    def test_repair_is_deferred_when_the_lock_stays_busy(self):
        (self.state / "backlog.jsonl").write_text('{bad\n')
        original = resilience.REPAIR_LOCK_WAIT
        resilience.REPAIR_LOCK_WAIT = 0.2
        try:
            with resilience.ledger_lock(self.state):
                problems = resilience.repair_ledgers(self.state)
        finally:
            resilience.REPAIR_LOCK_WAIT = original
        self.assertEqual(problems, ["ledger.lock stayed busy; ledger repair deferred to the next cycle"])
        self.assertEqual((self.state / "backlog.jsonl").read_text(), "{bad\n")

    def test_malformed_block_count_does_not_stop_blocking(self):
        (self.state / "backlog.jsonl").write_text(json.dumps({"id": "t", "status": "in_progress", "block_count": "twice"}) + "\n")
        (self.state / "run.json").write_text(json.dumps({"active_item": {"id": "t"}}))
        self.assertEqual(resilience.block_active_item(self.state, "Gate hung"), "t")
        task = resilience.merged_tasks(self.state)["t"]
        self.assertEqual((task["status"], task["block_count"]), ("blocked", 1))

    def test_unauthorized_target_outcome_is_cleared(self):
        for skill, args, expected in (("improve", "", None), ("improve-max", "", None),
                                      ("improve-max", "--target 5x --stop-at-target", "target reached")):
            with self.subTest(skill=skill, args=args):
                run = {"outcome": "Target Reached"}
                resilience.repair_run(run, skill, args)
                self.assertEqual(run["outcome"], expected)
        legacy = {"outcome": "target reached"}
        self.assertEqual(resilience.repair_run(legacy), [], "without the skill, the outcome is only normalized")


if __name__ == "__main__":
    unittest.main()
