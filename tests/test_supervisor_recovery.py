"""Supervisor recovery paths from the 2026-11-12 review (section 2), in process and through the daemon.

The in-process tests drive improve_runtime.Supervisor with a stubbed launch, so a persistent
error, a full disk, or a clock jump can be staged exactly. The daemon tests reuse the fake CLI
and fixture of test_runtime and run in the default resilient configuration.
"""
import argparse
import errno
import io
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))
from support import budget  # noqa: E402
import improve_resilience as resilience  # noqa: E402
import improve_runtime as runtime  # noqa: E402
import test_runtime as daemon_tests  # noqa: E402


class SupervisorFixture(unittest.TestCase):
    """A real repository and Supervisor whose agent launch is replaced by the test."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve supervisor ")
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        for args in (("init", "-q"), ("add", "-A"), ("commit", "-qm", "baseline")):
            if args[0] == "add":
                (self.repo / "README.txt").write_text("fixture\n")
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)
        self.state = self.repo / ".improve"
        self.state.mkdir()
        self.saved_env = dict(os.environ)
        for key in [key for key in os.environ if key.startswith("IMPROVE_")]:
            del os.environ[key]
        os.environ.update(IMPROVE_LEARNING_HOME=str(Path(self.temp.name) / "learning-home"),
                          IMPROVE_GRAPH_DREAM_MINUTES="0", IMPROVE_GRAPH_REFINE_MINUTES="0")
        self.launched = []
        # Supervisor.log also prints each line; keep the test report readable.
        self.saved_stdout, sys.stdout = sys.stdout, io.StringIO()

    def tearDown(self):
        sys.stdout = self.saved_stdout
        os.environ.clear()
        os.environ.update(self.saved_env)
        self.temp.cleanup()

    def supervisor(self, skill="improve", extra="", remaining=3600):
        args = argparse.Namespace(skill=skill, args=extra, finalize=False, resume=False, intake=None,
                                  duration="1h", no_board=True)
        sup = runtime.Supervisor(args, self.repo)
        sup.engine, sup.codex_sandbox, sup.model = "claude", None, None
        now = int(time.time())
        sup.control = dict(started_at=runtime.stamp(now - 60), deadline=runtime.stamp(now + remaining), run_id="r1",
                           engine="claude", skill=skill, args=extra)
        runtime.atomic_json(sup.control_path, sup.control)
        sup.cooldown, sup.park_wait, sup.park_cap = 0, 1, 1
        sup.failures, sup.next_wait = 0, 900
        return sup

    def stop_after_launch(self, sup):
        """Each launch is recorded and asks for a stop, so the run ends after one cycle."""
        def launch(final=False):
            self.launched.append(final)
            sup.stop_path.touch()
            return 0, 0.0, ""
        sup.launch = launch

    def run_bounded(self, sup, seconds):
        """Run the loop in a thread; a loop that never returns fails instead of hanging the suite."""
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault("rc", sup.run()), daemon=True)
        thread.start()
        thread.join(budget(seconds))
        self.assertFalse(thread.is_alive(), "the supervisor loop did not return")
        return result.get("rc")

    def journal(self):
        path = self.state / "journal.md"
        return path.read_text() if path.exists() else ""

    def write_run(self, sup, **fields):
        run = dict(started_at=sup.control["started_at"], deadline=sup.control["deadline"], cycle=3, outcome=None)
        run.update(fields)
        runtime.atomic_json(sup.run_path, run)
        return run


class StateErrorRecoveryTests(SupervisorFixture):
    """Section 2.1: a state error at the top of the loop must not stop every cycle."""

    def test_unauthorized_target_reached_is_cleared_and_the_next_cycle_launches(self):
        sup = self.supervisor()
        self.write_run(sup, outcome="target reached")
        self.stop_after_launch(sup)
        self.assertEqual(self.run_bounded(sup, 10), 0)
        self.assertEqual(self.launched, [False], "a work cycle ran instead of a crash loop")
        self.assertIn("valid only for improve-max with --stop-at-target", self.journal())
        self.assertEqual(runtime.read_json(sup.control_path)["outcome"], "stopped by user")

    def test_target_reached_still_ends_an_authorized_improve_max_run(self):
        sup = self.supervisor("improve-max", "--stop-at-target")
        self.write_run(sup, outcome="target reached")
        self.stop_after_launch(sup)
        self.assertEqual(self.run_bounded(sup, 10), 0)
        self.assertEqual(self.launched, [])
        self.assertEqual(runtime.read_json(sup.control_path)["outcome"], "target reached")

    def test_stop_with_a_bad_outcome_returns_at_once_with_a_small_journal(self):
        # The review's probe: this spun for good and wrote 2.5 MB of tracebacks in 3 seconds.
        sup = self.supervisor()
        self.write_run(sup, outcome="target reached")
        sup.stop_path.touch()
        self.stop_after_launch(sup)
        self.assertEqual(self.run_bounded(sup, 5), 0)
        self.assertEqual(self.launched, [])
        self.assertLess(len(self.journal()), 4096)

    def test_corrupt_run_json_between_cycles_is_moved_aside_and_work_continues(self):
        sup = self.supervisor()
        sup.run_path.write_text("{not json")
        self.stop_after_launch(sup)
        self.assertEqual(self.run_bounded(sup, 10), 0)
        self.assertEqual(self.launched, [False])
        self.assertEqual((self.state / "run.damaged.json").read_text(), "{not json")
        self.assertIn("run.json could not be read", self.journal())

    def test_corrupt_run_json_is_restored_from_the_last_intact_copy(self):
        sup = self.supervisor()
        self.write_run(sup, next_action="Profile the importer")
        self.assertIsNone(sup.reconcile())
        sup.run_path.write_text('{"cycle": ')
        self.assertIsNone(sup.reconcile())
        self.assertEqual(runtime.read_json(sup.run_path)["next_action"], "Profile the importer")
        self.assertEqual((self.state / "run.damaged.json").read_text(), '{"cycle": ')

    def test_repeated_repairs_are_journaled_once(self):
        sup = self.supervisor()
        for _ in range(5):
            self.write_run(sup, outcome="target reached")
            sup.reconcile()
        self.assertEqual(self.journal().count("valid only for improve-max"), 1)

    def test_stop_during_a_persistent_error_exits_promptly_with_a_bounded_journal(self):
        sup = self.supervisor()
        sup.max_crashes = 3

        def broken():
            raise runtime.StateError("persistent state problem")
        sup.reconcile = broken
        timer = threading.Timer(0.5, sup.stop_path.touch)
        timer.start()
        try:
            self.assertEqual(self.run_bounded(sup, 10), 0)
        finally:
            timer.cancel()
        self.assertEqual(runtime.read_json(sup.control_path)["outcome"], "stopped by user")
        journal = self.journal()
        self.assertEqual(journal.count("Traceback"), 1, "a repeated traceback is journaled once")
        self.assertLess(len(journal), 16384)

    def test_errors_past_the_deadline_halt_instead_of_spinning(self):
        sup = self.supervisor(remaining=-5)
        sup.max_crashes = 3

        def broken():
            raise RuntimeError("persistent")
        sup.reconcile = broken
        sup.learn = lambda: None
        rc = self.run_bounded(sup, 10)
        self.assertEqual(rc, 1)
        self.assertIn("supervisor error repeated 3 times", runtime.read_json(sup.control_path)["outcome"])
        self.assertNotIn("parks", sup.control, "parking past the deadline would only spin")

    def test_prompt_lists_outcomes_per_skill(self):
        plain = self.supervisor().prompt()
        self.assertNotIn('"target reached"', plain)
        self.assertIn('"halted: <reason>"', plain)
        target = self.supervisor("improve-max", "--stop-at-target").prompt()
        self.assertIn('"target reached"', target)
        self.assertNotIn('"target reached"', self.supervisor("improve-max").prompt())


class PromptTests(SupervisorFixture):
    """Section 5.2/5.3: one source for state reads and the TOON grammar."""

    def test_prompt_reads_state_through_helpers_and_names_the_write_commands(self):
        prompt = self.supervisor().prompt()
        for text in (" run\n", " queue\n", " discovery --limit 5", "preflight_complete", "authorization",
                     "run-set KEY=VALUE", "--json-field KEY=JSON", "discovery-add --json", "show ID"):
            self.assertIn(text, prompt)
        self.assertNotIn("Read .improve/run.json", prompt)
        self.assertIn("do not read it", prompt, "supervisor.json history is not agent input")
        self.assertIn("SKILL.md", prompt)
        self.assertIn("…[+N]", prompt)
        self.assertIn("After real work or discovery, persist cycle + 1", prompt)
        self.assertEqual(runtime.VERSION, "1.17.0")

    def test_late_improve_max_prompt_keeps_the_run_its_terms(self):
        self.assertIn("still the same improve-max run", self.supervisor("improve-max", remaining=3600).prompt())
        self.assertNotIn("still the same improve-max run", self.supervisor("improve-max", remaining=3 * 86400).prompt())
        self.assertNotIn("still the same improve-max run", self.supervisor("improve", remaining=3600).prompt())


class DiskFullTests(SupervisorFixture):
    """Section 2.3: ENOSPC anywhere in an iteration parks instead of halting."""

    def test_disk_full_is_recognized_through_wrapping_errors(self):
        try:
            try:
                raise OSError(errno.ENOSPC, "No space left on device")
            except OSError as exc:
                raise runtime.StateError("cannot write run.json") from exc
        except runtime.StateError as wrapped:
            self.assertTrue(runtime.disk_full(wrapped))
        self.assertTrue(runtime.disk_full(sqlite3.OperationalError("database or disk is full")))
        self.assertTrue(runtime.disk_full(runtime.StateError("fatal: write error: No space left on device")))
        self.assertFalse(runtime.disk_full(OSError(errno.EACCES, "Permission denied")))

    def test_disk_full_mid_iteration_parks_and_resumes(self):
        sup = self.supervisor()
        full = {"on": True}
        real_reconcile, real_journal = sup.reconcile, sup.journal

        def reconcile():
            if full["on"]:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_reconcile()

        def journal(message):
            if full["on"]:
                raise OSError(errno.ENOSPC, "No space left on device")
            real_journal(message)

        def wait(seconds, past_deadline=False):
            full["on"] = False  # Space comes back while parked.
            return 0.0
        sup.reconcile, sup.journal, sup.wait = reconcile, journal, wait
        self.stop_after_launch(sup)
        self.assertEqual(self.run_bounded(sup, 10), 0)
        control = runtime.read_json(sup.control_path)
        self.assertEqual(control["parks"], 1)
        self.assertIn("the disk is full", control["last_error"])
        self.assertEqual(self.launched, [False], "the run resumed work after the park")
        self.assertEqual(control["outcome"], "stopped by user")

    def test_crash_handling_survives_failing_writes(self):
        sup = self.supervisor()
        calls = []

        def iterate():
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("unexpected")
            return 0

        def unwritable(*_args, **_kwargs):
            raise OSError(errno.EIO, "I/O error")
        sup.iterate, sup.journal, sup.save_control = iterate, unwritable, unwritable
        self.assertEqual(self.run_bounded(sup, 10), 0)
        self.assertEqual(sup.control["crashes"], 1)


class ClockJumpTests(SupervisorFixture):
    """Section 2.4: a laptop asleep past the deadline must not let the cycle keep working."""

    class JumpingClock:
        """time.time() jumps forward once the cycle has started; the monotonic clock does not."""

        # Bound before `time` below, which shadows the module inside this class body.
        monotonic = staticmethod(time.monotonic)
        sleep = staticmethod(time.sleep)

        def __init__(self, after, jump):
            self.started, self.after, self.jump = time.monotonic(), after, jump

        def time(self):
            return time.time() + (self.jump if time.monotonic() - self.started >= self.after else 0)

    def test_wall_clock_deadline_stops_the_child(self):
        bin_dir = Path(self.temp.name) / "bin"
        bin_dir.mkdir()
        cli = bin_dir / "claude"
        cli.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(120)\n")
        cli.chmod(0o755)
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]
        sup = self.supervisor(remaining=3600)
        sup.finish_grace = 1
        sup.memory_brief = lambda final=False: ""
        runtime.time = self.JumpingClock(after=0.5, jump=7200)
        try:
            started = time.monotonic()
            rc, _, _ = sup.launch()
        finally:
            runtime.time = time
        self.assertEqual(rc, 124)
        self.assertLess(time.monotonic() - started, budget(15))
        self.assertIsNone(runtime.read_json(sup.control_path)["child_pid"])


class LimitDetectionTests(unittest.TestCase):
    """Section 2.5: a limit is read from the end of the output, not from echoed tool output."""

    def test_limit_in_the_closing_message_counts(self):
        self.assertTrue(runtime.limit_hit("work output\n" * 100 + "You've hit your usage limit. Try again at 5pm."))

    def test_limit_text_early_in_tool_output_does_not(self):
        output = "curl: HTTP/1.1 429 Too Many Requests\n" + "test passed\n" * 1000 + "Summary: fixed the retry."
        self.assertFalse(runtime.limit_hit(output))


class BriefIsolationTests(SupervisorFixture):
    """Section 2.5: one bad store costs only its own part of the cycle brief."""

    def test_learning_failure_keeps_the_recovery_brief(self):
        import improve_learning
        sup = self.supervisor()
        original = improve_learning.guidance
        improve_learning.guidance = lambda repo: (_ for _ in ()).throw(KeyError("text"))
        try:
            brief = sup.memory_brief()
        finally:
            improve_learning.guidance = original
        self.assertIn("ready queue is empty", brief)
        self.assertIn("learning brief unavailable", (self.state / "daemon.log").read_text())

    def test_malformed_lessons_do_not_drop_the_brief(self):
        sup = self.supervisor()
        (self.state / "learning.json").write_text(json.dumps({"lessons": [
            {"key": "k", "reinforced": 1}, {"text": "Warm the fixture cache before the gate.", "reinforced": "x"}, "junk"]}))
        brief = sup.memory_brief()
        self.assertIn("Warm the fixture cache", brief)
        self.assertIn("ready queue is empty", brief)


class RecoveryDaemonTests(daemon_tests.ResilienceTests):
    """The section 2 recovery paths end to end, in the default resilient configuration."""

    def journal(self):
        path = self.state / "journal.md"
        return path.read_text() if path.exists() else ""

    def control(self):
        return self.load("supervisor.json")

    def quarantine_refs(self):
        return self.git("for-each-ref", "--format=%(refname)", "refs/improve/quarantine").stdout.decode().split()

    # ---------------------------------------------------------------- 2.1

    def test_plain_improve_agent_writing_target_reached_still_reaches_finalization(self):
        result = self.invoke("target", duration=f"{round(budget(3))}s")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.control()["outcome"], "completed")
        self.assertGreater(len(self.launches()), 1)
        self.assertIn("FINALIZATION ONLY", self.launches()[-1]["prompt"])
        self.assertNotIn('"target reached"', self.launches()[0]["prompt"])

    # ---------------------------------------------------------------- 2.2

    def seed_crashed_run(self, **changes):
        return self.seed_control(run_id="crashed-run", phase="working", **changes)

    def test_relaunch_with_a_dirty_tree_quarantines_and_continues(self):
        self.seed_crashed_run()
        (self.repo / "README.txt").write_text("half-edited by the dead cycle\n")
        result = self.invoke("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        refs = self.quarantine_refs()
        self.assertEqual(len(refs), 1)
        self.assertTrue(refs[0].endswith("-between"))
        self.assertEqual(self.git("show", refs[0] + ":README.txt").stdout, b"half-edited by the dead cycle\n")
        self.assertEqual((self.repo / "README.txt").read_text(), "fixture\n")
        self.assertEqual(self.control()["outcome"], "stopped by user")
        self.assertEqual(len(self.launches()), 1)

    def test_supervised_relaunch_with_a_dirty_tree_survives(self):
        self.seed_crashed_run()
        (self.repo / "README.txt").write_text("half-edited by the dead cycle\n")
        result = self.invoke("stop", "--supervise", env={"IMPROVE_RELAUNCH_WAIT": "0"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.quarantine_refs()), 1)
        self.assertEqual(self.control()["outcome"], "stopped by user")

    def test_first_launch_still_refuses_a_dirty_tree_and_dry_run_reports_it(self):
        (self.repo / "README.txt").write_text("the user's own edit\n")
        result = self.invoke("stop")
        self.assertEqual(result.returncode, 2)
        self.assertIn("dirty", result.stderr)
        self.assertFalse((self.state / "launches.jsonl").exists())
        dry = self.invoke("stop", "--dry-run", intake=False)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("Working tree is dirty", dry.stdout)
        self.assertEqual((self.repo / "README.txt").read_text(), "the user's own edit\n")

    # ---------------------------------------------------------------- 2.4 final report retry

    def kill_during_final_retry_wait(self):
        """Start an expired run whose final report hits a limit, then SIGKILL the daemon while it
        waits to retry. The saved retry time is moved close so the relaunch need not wait long."""
        self.seed(remaining=-1)
        env = {**self.env, "FAKE_MODE": "limit_final", "IMPROVE_LIMIT_WAIT": "120"}
        process = subprocess.Popen(self.command("--for", "60s", "--intake", str(self.intake)), env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            waiting = self.wait_control(lambda value: value.get("final_retry_at") and value.get("phase") == "account-limit",
                                        timeout=budget(10))
            self.assertGreater(runtime.epoch(waiting["final_retry_at"]), runtime.epoch(waiting["deadline"]))
            process.kill()
            process.wait(timeout=budget(5))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        control = self.control()
        retry = int(time.time()) + 1
        control["final_retry_at"] = runtime.stamp(retry)
        runtime.atomic_json(self.state / "supervisor.json", control)
        return retry

    def assert_finalized_after(self, retry):
        control = self.control()
        self.assertEqual(control["outcome"], "completed")
        self.assertFalse(control["summary_pending"])
        self.assertIsNone(control.get("final_retry_at"))
        final = self.launches()[-1]
        self.assertIn("FINALIZATION ONLY", final["prompt"])
        self.assertGreaterEqual(final["started_epoch"], retry)

    def test_relaunch_after_a_kill_during_the_final_retry_wait_finalizes(self):
        retry = self.kill_during_final_retry_wait()
        result = self.invoke("limit_final", intake=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_finalized_after(retry)

    def test_finalize_after_a_kill_during_the_final_retry_wait(self):
        retry = self.kill_during_final_retry_wait()
        result = subprocess.run(self.command("--finalize"), env={**self.env, "FAKE_MODE": "limit_final"},
                                capture_output=True, text=True, timeout=budget(25))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_finalized_after(retry)

    def test_supervise_after_a_kill_during_the_final_retry_wait(self):
        retry = self.kill_during_final_retry_wait()
        result = self.invoke("limit_final", "--supervise", intake=False, env={"IMPROVE_RELAUNCH_WAIT": "0"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_finalized_after(retry)

    def test_pre_1_17_final_retry_past_the_deadline_is_honored(self):
        now = int(time.time())
        self.seed_control(deadline=runtime.stamp(now - 30), phase="account-limit", retry_at=runtime.stamp(now + 1))
        result = self.invoke("empty", intake=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_finalized_after(now + 1)

    def test_final_report_limit_with_exit_zero_is_retried(self):
        self.seed(remaining=-1)
        result = self.invoke(script=["limit_final0", "empty"], env={"IMPROVE_LIMIT_WAIT": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.launches()), 2)
        self.assertFalse(self.control()["summary_pending"])
        self.assertIn("limit during the final report", self.journal())

    # ---------------------------------------------------------------- 2.4/2.5 watchdog and stop

    def test_resume_with_supervise_survives_a_crash_after_the_resume(self):
        self.seed_control(run_id="halted-run", outcome="halted: gate unavailable")
        # As documented: no --for on recovery. The watchdog's relaunch drops --resume too.
        command = self.command("--resume", "--supervise", "--intake", str(self.intake))
        environment = {**self.env, "FAKE_SCRIPT": json.dumps(["crash_parent", "stop"]), "IMPROVE_RELAUNCH_WAIT": "0"}
        result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=budget(40))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("relaunching", result.stdout)
        self.assertEqual(self.control()["outcome"], "stopped by user")
        self.assertEqual(len(self.launches()), 2)

    def start_watchdog_into_backoff(self):
        log = self.root / "watchdog.log"
        environment = {**self.env, "FAKE_SCRIPT": json.dumps(["crash_parent", "stop"]), "IMPROVE_RELAUNCH_WAIT": "60"}
        stream = log.open("w")
        process = subprocess.Popen(self.command("--for", "60s", "--intake", str(self.intake), "--supervise"),
                                   env=environment, stdout=stream, stderr=subprocess.STDOUT)
        stream.close()
        until = time.monotonic() + budget(20)
        while "relaunching in" not in log.read_text() and time.monotonic() < until and process.poll() is None:
            time.sleep(0.05)
        self.assertIn("relaunching in 60s", log.read_text())
        return process, log

    def assert_stopped_without_new_work(self, process, log):
        try:
            rc = process.wait(timeout=budget(45))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        self.assertEqual(rc, 0, log.read_text())
        self.assertIn("stop requested during the relaunch wait", log.read_text())
        self.assertEqual(len(self.launches()), 1, "no cycle was started after the stop")
        self.assertEqual(self.control()["outcome"], "stopped by user")

    def test_sigterm_during_relaunch_backoff_stops_the_run(self):
        process, log = self.start_watchdog_into_backoff()
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        self.assert_stopped_without_new_work(process, log)
        self.assertLess(time.monotonic() - started, 50, "the 60 s backoff was cut short")

    def test_stop_command_during_relaunch_backoff_stops_the_run(self):
        process, log = self.start_watchdog_into_backoff()
        stop = subprocess.run(self.command("--stop"), env=self.env, capture_output=True, text=True, timeout=budget(10))
        self.assertIn("stop recorded", stop.stdout)
        self.assert_stopped_without_new_work(process, log)

    def test_stop_while_the_daemon_is_down_is_honored_by_the_next_launch(self):
        self.seed_control(run_id="crashed-run", phase="working")
        stop = subprocess.run(self.command("--stop"), env=self.env, capture_output=True, text=True, timeout=budget(10))
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertIn("stop recorded", stop.stdout)
        result = self.invoke("early")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.control()["outcome"], "stopped by user")
        self.assertFalse((self.state / "launches.jsonl").exists())

    def test_stop_without_a_live_run_records_nothing(self):
        self.seed_control(outcome="completed")
        stop = subprocess.run(self.command("--stop"), env=self.env, capture_output=True, text=True, timeout=budget(10))
        self.assertEqual(stop.stdout.strip(), "not running")
        self.assertFalse((self.state / "stop.request").exists())

    def test_supervise_gives_up_after_the_relaunch_cap(self):
        result = self.invoke("early", "--supervise", script=["crash_parent"],
                             env={"IMPROVE_RELAUNCH_WAIT": "0", "IMPROVE_MAX_RELAUNCHES": "1"}, timeout=budget(60))
        try:
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("giving up", result.stderr)
            self.assertEqual(len(self.launches()), 2)
            self.assertIsNone(self.control().get("outcome"), "a crash is not an outcome; the run can still be relaunched")
        finally:
            pid = self.control().get("child_pid")
            if pid:
                resilience.terminate_group(pid, grace=1)

    # ---------------------------------------------------------------- 2.4 artifacts, new runs

    def test_custom_artifact_paths_are_cleaned_without_quarantine(self):
        result = self.invoke(script=["custom_artifact", "stop"])
        self.assertEqual(result.returncode, 0, result.stderr)
        control = self.control()
        self.assertEqual(control["artifact_cleanups"], 1)
        self.assertNotIn("quarantines", control)
        self.assertEqual(self.quarantine_refs(), [])
        self.assertFalse((self.repo / "generated").exists())
        self.assertIn("generated/", self.journal())
        self.assertFalse((self.state / "backlog.jsonl").exists(), "artifacts never block a task")

    def test_quarantine_save_failure_halts_and_keeps_the_work(self):
        if os.geteuid() == 0:
            self.skipTest("root can read a mode-000 file, so git add cannot fail this way")
        result = self.invoke("unreadable")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("could not be saved", self.control()["outcome"])
        self.assertTrue((self.repo / "secret.txt").exists(), "unsaved work is never deleted")
        (self.repo / "secret.txt").chmod(0o600)

    def test_new_run_stops_the_previous_agent_and_keeps_the_corpus(self):
        self.state.mkdir()
        child = subprocess.Popen([str(self.bin / "claude"), "-p", "old cycle"], cwd=self.repo,
                                 env={**self.env, "FAKE_MODE": "timeout"}, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            time.sleep(0.5)
            self.seed_control(run_id="old-run", phase="working", child_pid=child.pid)
            (self.state / "corpus" / "checkout").mkdir(parents=True)
            (self.state / "corpus" / "checkout" / "case-1.json").write_text("{}")
            result = self.invoke("early", "--new-run")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(child.wait(timeout=budget(5)), (-signal.SIGTERM, -signal.SIGKILL))
            self.assertIn("stopped the previous run's agent", result.stdout)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        self.assertTrue((self.state / "corpus" / "checkout" / "case-1.json").exists())
        archive = next((self.state / "history").iterdir())
        self.assertEqual(json.loads((archive / "supervisor.json").read_text())["run_id"], "old-run")

    # ---------------------------------------------------------------- 2.5 worker halt across a crash

    def test_worker_halt_left_across_a_crash_is_softened(self):
        self.seed_control(run_id="crashed-run", phase="working")
        run = self.load()
        run.update(outcome="halted: simulator unavailable", active_item={"id": "t-1", "title": "Snapshot tests"})
        runtime.atomic_json(self.state / "run.json", run)
        (self.state / "backlog.jsonl").write_text(json.dumps({"id": "t-1", "title": "Snapshot tests", "status": "in_progress"}) + "\n")
        result = self.invoke("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tasks()["t-1"]["status"], "blocked")
        self.assertIn("simulator unavailable", self.tasks()["t-1"]["blocked_reason"])
        self.assertEqual(len(self.launches()), 1)
        self.assertIn("Work around it", self.launches()[0]["prompt"])
        self.assertEqual(self.control()["outcome"], "stopped by user")

    # ---------------------------------------------------------------- untested behaviors (7.2)

    def test_account_limit_budget_exhaustion_parks(self):
        result = self.invoke("limit", duration=f"{round(budget(6))}s", env={"IMPROVE_LIMIT_WAIT": "1", "IMPROVE_LIMIT_WAIT_BUDGET": "1",
                                                          "IMPROVE_PARK_WAIT": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.control()["outcome"], "completed")
        self.assertIn("Parked (1): account-limit wait budget exhausted", self.journal())

    def test_account_limit_budget_exhaustion_halts_when_parking_is_disabled(self):
        result = self.invoke("limit", env={"IMPROVE_LIMIT_WAIT": "1", "IMPROVE_LIMIT_WAIT_BUDGET": "1", "IMPROVE_PARK": "0"})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.control()["outcome"], "halted: account-limit wait budget exhausted")
        self.assertEqual(len(self.launches()), 2)

    def test_final_timeout_kills_a_hung_report(self):
        self.seed(remaining=-1)
        started = time.monotonic()
        result = self.invoke("timeout", env={"IMPROVE_FINAL_TIMEOUT": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, budget(20))
        control = self.control()
        self.assertTrue(control["summary_pending"])
        self.assertIsNone(control["child_pid"])
        self.assertEqual(len(self.launches()), 1)


# Inherit the resilient fixture only; the parent classes' tests already ran in test_runtime.
for _name in [name for name in dir(daemon_tests.ResilienceTests) if name.startswith("test_")]:
    if _name not in vars(RecoveryDaemonTests):
        setattr(RecoveryDaemonTests, _name, None)


class WrapperTests(unittest.TestCase):
    """Section 2.5: the shell wrappers work through a symlink and with CDPATH set (bash 3.2 too)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve wrappers ")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def shells(self):
        """macOS /bin/bash is 3.2; the bash on PATH may be newer. Both must work."""
        return sorted({shell for shell in ("/bin/bash", shutil.which("bash")) if shell and Path(shell).exists()})

    def test_wrappers_resolve_a_chain_of_relative_symlinks(self):
        (self.root / "a" / "b").mkdir(parents=True)
        (self.root / "a" / "b" / "clock").symlink_to(ROOT / "scripts" / "improve-clock.sh")
        (self.root / "a" / "clock").symlink_to(Path("b") / "clock")
        (self.root / "clock").symlink_to(Path("a") / "clock")
        for shell in self.shells():
            with self.subTest(shell=shell):
                result = subprocess.run([shell, "./clock", "--help"], cwd=self.root, capture_output=True, text=True,
                                        timeout=budget(10))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("clock", result.stdout)

    def test_wrappers_ignore_cdpath(self):
        decoy = self.root / "decoy" / "scripts"
        decoy.mkdir(parents=True)
        for name, expected in (("improve-clock.sh", "clock"), ("improve-daemon.sh", "improve-daemon"),
                               ("improve-board.sh", "improve_board.py")):
            flag = "--version" if name == "improve-daemon.sh" else "--help"
            for shell in self.shells():
                with self.subTest(name=name, shell=shell):
                    result = subprocess.run([shell, f"scripts/{name}", flag], cwd=ROOT, capture_output=True, text=True,
                                            env={**os.environ, "CDPATH": str(self.root / "decoy")}, timeout=budget(10))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(expected, result.stdout)

    def test_wrappers_parse(self):
        for path in sorted((ROOT / "scripts").glob("*.sh")):
            for shell in self.shells():
                with self.subTest(path=path.name, shell=shell):
                    self.assertEqual(subprocess.run([shell, "-n", str(path)], capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
