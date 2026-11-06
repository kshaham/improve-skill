"""Cross-run learning store and recovery helpers, without launching agents."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

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
        (self.state / "backlog.jsonl").write_text('{"id":"a"}\n{bad\n[1]\n{"id":"b"}\n')
        problems = resilience.repair_ledgers(self.state)
        self.assertEqual(len(problems), 1)
        self.assertEqual([row["id"] for row in resilience.read_rows(self.state / "backlog.jsonl")], ["a", "b"])
        self.assertEqual((self.state / "backlog.jsonl.damaged").read_text(), "{bad\n[1]\n")
        self.assertEqual(resilience.repair_ledgers(self.state), [])


if __name__ == "__main__":
    unittest.main()
