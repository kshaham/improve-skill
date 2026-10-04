"""Regression tests use disposable repositories and a fake CLI, never a paid agent."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runtime", ROOT / "scripts/improve_runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.start = runtime.epoch("2026-10-04T12:00:00Z")
        self.run = dict(started_at=runtime.stamp(self.start), deadline=runtime.stamp(self.start + 7200))

    def test_exact_deadline_and_hour_report(self):
        info = runtime.timing(self.run, self.start + 3600)
        self.assertEqual((info["hour"], info["report_hour"], info["status"]), (2, 1, "RUNNING"))
        self.run["last_report_hour"] = 1
        self.assertIsNone(runtime.timing(self.run, self.start + 3601)["report_hour"])
        self.assertEqual(runtime.timing(self.run, self.start + 7200)["status"], "OVER")

    def test_invalid_state_is_not_expiry(self):
        for change in ({"deadline": "nonsense"}, {"deadline": self.run["started_at"]},
                       {"started_at": "2027-01-01T00:00:00Z"}, {"last_report_hour": "1"},
                       {"last_report_hour": True}):
            with self.subTest(change=change), self.assertRaises(runtime.StateError):
                runtime.timing({**self.run, **change}, self.start)

    def test_duration_parsing(self):
        for value, expected in (("08m", 480), ("2d", 172800), ("17", 17), ("90m", 5400)):
            self.assertEqual(runtime.duration(value), expected)
        for value in ("0", "-5m", "3weeks", "1h20m", "999999999999d"):
            with self.subTest(value=value), self.assertRaises(runtime.StateError):
                runtime.duration(value)

    def test_premature_completed_and_explicit_target(self):
        self.run["outcome"] = "completed"
        self.assertIsNone(runtime.terminal_outcome(self.run, "improve", "", self.start + 10))
        self.assertEqual(runtime.terminal_outcome(self.run, "improve", "", self.start + 7200), "completed")
        self.run["outcome"] = "target reached"
        with self.assertRaises(runtime.StateError):
            runtime.terminal_outcome(self.run, "improve-max", "", self.start)
        self.assertEqual(runtime.terminal_outcome(self.run, "improve-max", "--stop-at-target", self.start), "target reached")

    def test_intake_requires_all_ten_real_answers(self):
        complete = {"answers": {key: "no preference" for key in runtime.INTAKE_KEYS}}
        self.assertEqual(len(complete["answers"]), 10)
        runtime.validate_intake(complete)
        for bad in ({}, {"answers": {}}, {"answers": {**complete["answers"], "assets": " "}}):
            with self.assertRaises(runtime.StateError):
                runtime.validate_intake(bad)


class EngineTests(unittest.TestCase):
    def test_host_detection(self):
        for engine, markers in runtime.HOST_MARKERS.items():
            for marker in markers:
                with self.subTest(marker=marker):
                    self.assertEqual(runtime.resolve_engine(environ={marker: '1'}), engine)

    def test_unknown_or_conflicting_hosts_require_explicit_engine(self):
        for env in ({}, {'CODEX_THREAD_ID': '1', 'CLAUDECODE': '1'}):
            with self.subTest(env=env), self.assertRaisesRegex(runtime.StateError, 'pass --engine'):
                runtime.resolve_engine(environ=env)
            self.assertEqual(runtime.resolve_engine('codex', environ=env), 'codex')

    def test_saved_engine_wins_over_host_and_legacy_stays_claude(self):
        env = {'CODEX_THREAD_ID': '1'}
        self.assertEqual(runtime.resolve_engine(control={'engine': 'claude'}, environ=env), 'claude')
        self.assertEqual(runtime.resolve_engine(control={}, environ=env), 'claude')
        self.assertEqual(runtime.resolve_engine(control={'engine': 'codex'}, environ={}), 'codex')

    def test_invalid_or_changed_engine_is_rejected(self):
        for args in ({'requested': 'other'}, {'control': {'engine': None}},
                     {'requested': 'codex', 'control': {'engine': 'claude'}}):
            with self.subTest(args=args), self.assertRaises(runtime.StateError):
                runtime.resolve_engine(**args)

    def test_sandbox_inherits_configuration_unless_explicitly_selected(self):
        self.assertIsNone(runtime.resolve_sandbox('codex'))
        self.assertIsNone(runtime.resolve_sandbox('claude'))
        self.assertNotIn('--sandbox', runtime.agent_command('codex', 'prompt'))
        for mode in runtime.SANDBOX_MODES:
            self.assertEqual(runtime.resolve_sandbox('codex', mode), mode)
            self.assertEqual(runtime.resolve_sandbox('codex', control={'codex_sandbox': mode}), mode)

    def test_sandbox_cannot_change_on_restart_or_apply_to_claude(self):
        for engine, requested, control in (
            ('claude', 'workspace-write', None),
            ('codex', 'danger-full-access', {'codex_sandbox': 'workspace-write'}),
            ('codex', None, {'codex_sandbox': 'invalid'}),
        ):
            with self.subTest(engine=engine, requested=requested), self.assertRaises(runtime.StateError):
                runtime.resolve_sandbox(engine, requested, control)


FAKE_AGENT = r'''import json, os, pathlib, subprocess, sys, time
state = pathlib.Path('.improve')
prompt = sys.argv[-1]
with (state / 'launches.jsonl').open('a') as stream:
    stream.write(json.dumps({'prompt': prompt, 'engine': pathlib.Path(sys.argv[0]).name,
                            'started_epoch': time.time(),
                            'argv': sys.argv[1:], 'nested_claude': os.environ.get('CLAUDECODE'),
                            'config_home': os.environ.get('CODEX_HOME'),
                            'stdin': sys.stdin.read()}) + '\n')
mode = os.environ.get('FAKE_MODE', 'empty')
if mode == 'orphan':
    subprocess.Popen([sys.executable, '-c', 'import signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(3); pathlib.Path(".improve/orphan-wrote").touch()'])
if mode in ('timeout', 'orphan') or (mode == 'overrun' and 'FINALIZATION ONLY' not in prompt):
    time.sleep(30)
    sys.exit(0)
control = json.loads((state / 'supervisor.json').read_text())
path = state / 'run.json'
run = json.loads(path.read_text()) if path.exists() else dict(
    started_at=control['started_at'], deadline=control['deadline'], outcome=None, cycle=0)
if 'FINALIZATION ONLY' in prompt:
    if mode == 'final_fail':
        sys.exit(1)
    if mode == 'final_no_report':
        sys.exit(0)
    if mode == 'final_commit':
        subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'unauthorized final edit'], check=True)
    run['outcome'] = control.get('outcome') or 'completed'
    (state / 'final-report.md').write_text('Final report from actual fake run.\n')
    (state / 'journal.md').open('a').write('\nFinal report from fake CLI.\n')
elif mode == 'limit':
    print('usage limit reached')
    sys.exit(1)
elif mode == 'no_checkpoint':
    sys.exit(0)
else:
    run['cycle'] = run.get('cycle', 0) + 1
    run['next_action'] = 'Inspect a new UI interaction state'
    if mode != 'counter_only':
        with (state / 'discovery.jsonl').open('a') as stream:
            stream.write(json.dumps({'scope': 'same' if mode == 'duplicate_scan' else str(run['cycle']),
                                     'accepted': 0, 'at': str(time.time()), 'generation': run['cycle']}) + '\n')
    if mode == 'early':
        run['outcome'] = 'completed' if run['cycle'] == 1 else 'stopped by user'
    elif mode == 'reset_deadline':
        run['deadline'] = '2099-01-01T00:00:00Z'
        run['outcome'] = 'stopped by user'
    elif mode == 'halt':
        run['outcome'] = 'halted: required gate unavailable after recovery'
    elif mode == 'dirty':
        pathlib.Path('uncommitted.txt').write_text('unverified work')
        run['outcome'] = 'completed'
    elif mode in ('target', 'failed_target'):
        run['outcome'] = 'target reached'
    elif mode in ('completed_at_deadline', 'failed_completion'):
        deadline = __import__('datetime').datetime.fromisoformat(control['deadline'].replace('Z', '+00:00')).timestamp()
        time.sleep(max(0, deadline - time.time()))
        run['outcome'] = 'completed'
    print('Found missing rate limiting on auth endpoints; scanned a new scope.')
path.write_text(json.dumps(run))
if mode in ('failed_target', 'failed_completion'):
    sys.exit(1)
'''


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve tests ")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "baseline")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for engine in ('claude', 'codex'):
            cli = self.bin / engine
            cli.write_text(f"#!{sys.executable}\n" + FAKE_AGENT)
            cli.chmod(0o755)
        self.intake = self.root / "intake.json"
        self.intake.write_text(json.dumps({"answers": {key: "no preference" for key in runtime.INTAKE_KEYS}}))
        markers = {key for keys in runtime.HOST_MARKERS.values() for key in keys}
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("IMPROVE_") and k not in markers}
        self.env.update(PATH=str(self.bin) + os.pathsep + self.env["PATH"],
                        IMPROVE_ENGINE="claude", IMPROVE_CYCLE_GAP="0", PYTHONDONTWRITEBYTECODE="1")

    def tearDown(self):
        if (self.state / "board.json").exists():
            subprocess.run([str(ROOT / "scripts/improve-board.sh"), "--repo", str(self.repo), "--stop"],
                           capture_output=True, timeout=5)
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    @property
    def state(self):
        return self.repo / ".improve"

    def command(self, *extra):
        board_args = [] if getattr(self, "with_board", False) else ["--no-board"]
        return [str(ROOT / "scripts/improve-daemon.sh"), "--repo", str(self.repo), *board_args, *extra]

    def test_board_stays_available_and_survives_new_run(self):
        self.with_board = True
        result = self.invoke("early", "--board-port", "0", "--engine", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = self.load("board.json")
        lock_inode = (self.state / "board.lock").stat().st_ino
        status = subprocess.run(self.command("--status", "--json"), env=self.env,
                                capture_output=True, text=True, timeout=5)
        info = json.loads(status.stdout)
        self.assertFalse(info["running"])
        self.assertTrue(info["board"]["running"])
        self.assertEqual(info["board"]["url"], saved["url"])
        result = self.invoke("early", "--new-run", "--engine", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.load("board.json"), saved)
        self.assertEqual((self.state / "board.lock").stat().st_ino, lock_inode)
        archive = next((self.state / "history").iterdir())
        self.assertTrue((archive / "run.json").exists())
        self.assertFalse((archive / "board.json").exists())
        self.assertEqual(self.load("supervisor.json")["engine"], "claude")

    def test_board_failure_does_not_stop_work(self):
        import socket
        self.with_board = True
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            result = self.invoke("early", "--board-port", str(occupied.getsockname()[1]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Kanban board unavailable", result.stdout)
        self.assertEqual(self.load()["cycle"], 2)

    def test_no_board_keeps_board_disabled_in_worker_prompt(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.state / "board.json").exists())
        self.assertIn("disabled by --no-board", self.launches()[0]["prompt"])

    def invoke(self, mode="early", *extra, intake=True, duration="60s"):
        args = ["--for", duration, *extra]
        if intake:
            args += ["--intake", str(self.intake)]
        return subprocess.run(self.command(*args), env={**self.env, "FAKE_MODE": mode},
                              capture_output=True, text=True, timeout=15)

    def load(self, name="run.json"):
        return json.loads((self.state / name).read_text())

    def launches(self):
        return [json.loads(line) for line in (self.state / "launches.jsonl").read_text().splitlines()]

    def seed(self, remaining=60):
        self.state.mkdir(exist_ok=True)
        run = dict(started_at=runtime.stamp(int(time.time()) - 120),
                   deadline=runtime.stamp(int(time.time()) + remaining), cycle=7, outcome=None)
        runtime.atomic_json(self.state / "run.json", run)
        return run

    def seed_control(self, **changes):
        run = self.seed()
        control = {**run, 'skill': 'improve', 'args': '', 'engine': 'claude', **changes}
        runtime.atomic_json(self.state / 'supervisor.json', control)
        return control

    def test_clock_uses_supervisor_before_first_checkpoint(self):
        control = self.seed_control()
        (self.state / 'run.json').unlink()
        command = [str(ROOT / 'scripts/improve-clock.sh'), '--json', str(self.repo)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['deadline'], control['deadline'])

    def test_clock_and_status_ignore_agent_deadline_reset_but_keep_report_progress(self):
        control = self.seed_control(started_at=runtime.stamp(time.time() - 7205))
        run = self.load()
        run.update(deadline='2099-01-01T00:00:00Z', last_report_hour=2)
        runtime.atomic_json(self.state / 'run.json', run)
        commands = ([str(ROOT / 'scripts/improve-clock.sh'), '--json', str(self.repo)],
                    self.command('--status', '--json'))
        for command in commands:
            result = subprocess.run(command, env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            info = json.loads(result.stdout)
            info = info.get('clock', info)
            self.assertEqual(info['deadline'], control['deadline'])
            self.assertEqual(info['started_at'], control['started_at'])
            self.assertIsNone(info['report_hour'])
        self.assertEqual(self.load()['deadline'], '2099-01-01T00:00:00Z')  # Read-only clock.

    def test_resume_keeps_explicit_model_even_when_environment_changes(self):
        self.assertEqual(self.invoke('halt', '--model', 'original-model').returncode, 1)
        self.env['IMPROVE_MODEL'] = 'different-model'
        result = self.invoke('early', '--resume', intake=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        for row in self.launches():
            self.assertEqual(row['argv'][row['argv'].index('--model') + 1], 'original-model')
        self.assertEqual(self.load('supervisor.json')['model'], 'original-model')

    def test_explicit_model_change_requires_new_run(self):
        self.assertEqual(self.invoke('halt', '--model', 'original-model').returncode, 1)
        old = (self.state / 'supervisor.json').read_bytes()
        result = self.invoke('early', '--resume', '--model', 'different-model')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual((self.state / 'supervisor.json').read_bytes(), old)
        self.assertEqual(len(self.launches()), 1)

    def test_restart_waits_for_recorded_rate_limit_retry(self):
        now = int(time.time())
        retry = now + 3
        self.seed_control(phase='account-limit', retry_at=runtime.stamp(retry),
                          limit_wait_started_at=runtime.stamp(now - 2),
                          limit_waited_seconds=4, next_limit_wait=10)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(self.launches()[0]['started_epoch'], retry)
        control = self.load('supervisor.json')
        self.assertGreaterEqual(control['limit_waited_seconds'], 9)
        self.assertIsNone(control['retry_at'])

    def test_expired_retry_accounts_downtime_without_sleeping_again(self):
        now = int(time.time())
        self.seed_control(phase='account-limit', retry_at=runtime.stamp(now - 1),
                          limit_wait_started_at=runtime.stamp(now - 6), limit_waited_seconds=7)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.load('supervisor.json')['limit_waited_seconds'], 12)
        self.assertIsNone(self.load('supervisor.json')['retry_at'])

    def test_restart_after_deadline_accounts_pending_wait_before_final_report(self):
        now = int(time.time())
        self.seed_control(deadline=runtime.stamp(now - 1), phase='account-limit',
                          retry_at=runtime.stamp(now - 1), limit_wait_started_at=runtime.stamp(now - 6),
                          limit_waited_seconds=4)
        result = self.invoke('empty')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.load('supervisor.json')['limit_waited_seconds'], 9)
        self.assertIsNone(self.load('supervisor.json')['retry_at'])
        self.assertEqual(len(self.launches()), 1)
        self.assertIn('FINALIZATION ONLY', self.launches()[0]['prompt'])

    def test_invalid_saved_retry_does_not_launch_or_erase_state(self):
        self.seed_control(phase='account-limit', retry_at='not-a-timestamp')
        old = (self.state / 'supervisor.json').read_bytes()
        result = self.invoke()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.state / 'launches.jsonl').exists())
        self.assertEqual((self.state / 'supervisor.json').read_bytes(), old)

    def test_failed_cycle_cannot_claim_target_success(self):
        result = self.invoke('failed_target', '--skill', 'improve-max', '--args=--stop-at-target')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(self.load()['outcome'].startswith('halted:'))
        self.assertTrue(self.load()['summary_pending'])

    def test_failed_cycle_cannot_claim_deadline_success(self):
        result = self.invoke('failed_completion', duration='2s')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(self.load()['outcome'].startswith('halted:'))

    def test_cycle_completion_at_deadline_still_gets_final_report(self):
        result = self.invoke('completed_at_deadline', duration='2s')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.launches()), 2)
        self.assertIn('FINALIZATION ONLY', self.launches()[-1]['prompt'])
        self.assertFalse(self.load()['summary_pending'])

    def test_resumed_halt_does_not_reuse_a_stale_report(self):
        self.assertEqual(self.invoke('halt').returncode, 1)
        self.assertEqual(self.invoke('empty', '--finalize').returncode, 1)
        self.assertFalse(self.load()['summary_pending'])
        self.assertEqual(self.invoke('halt', '--resume').returncode, 1)
        self.assertTrue(self.load()['summary_pending'])

    def test_codex_host_launches_codex_and_relaunches_until_stopped(self):
        self.env.update(IMPROVE_ENGINE="auto", CODEX_THREAD_ID="test-thread")
        result = self.invoke("early", "--model", "test-codex-model")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.load("supervisor.json")["engine"], "codex")
        self.assertEqual(len(self.launches()), 2)
        for launch in self.launches():
            self.assertEqual(launch["engine"], "codex")
            self.assertTrue(launch["prompt"].startswith("$improve "))
            self.assertIn("--no-daemon", launch["argv"])
            self.assertIn("--ephemeral", launch["argv"])
            self.assertIn("test-codex-model", launch["argv"])
            self.assertNotIn("--permission-mode", launch["argv"])
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", launch["argv"])
            self.assertEqual(launch["stdin"], "")

    def test_claude_host_launches_without_nested_session_marker(self):
        self.env.update(IMPROVE_ENGINE="auto", CLAUDECODE="1", CODEX_HOME="/test/config-home")
        result = self.invoke("early", "--model", "test-claude-model")
        self.assertEqual(result.returncode, 0, result.stderr)
        for launch in self.launches():
            self.assertEqual(launch["engine"], "claude")
            self.assertTrue(launch["prompt"].startswith("/improve "))
            self.assertIn("test-claude-model", launch["argv"])
            self.assertIn("-p", launch["argv"])
            self.assertIsNone(launch["nested_claude"])
            self.assertEqual(launch["config_home"], "/test/config-home")

    def test_resume_from_other_host_keeps_original_codex_engine(self):
        self.assertEqual(self.invoke("halt", "--engine", "codex", "--codex-sandbox", "workspace-write").returncode, 1)
        deadline = self.load()["deadline"]
        self.env.update(IMPROVE_ENGINE="auto", CLAUDECODE="1")
        result = self.invoke("early", "--resume", intake=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(row["engine"] == "codex" for row in self.launches()))
        for row in self.launches():
            self.assertEqual(row['argv'][row['argv'].index('--sandbox') + 1], 'workspace-write')
        self.assertEqual(self.load("supervisor.json")["codex_sandbox"], "workspace-write")
        self.assertEqual(self.load()["deadline"], deadline)

    def test_recovery_rejects_changed_sandbox_without_launching(self):
        self.assertEqual(self.invoke("halt", "--engine", "codex", "--codex-sandbox", "read-only").returncode, 1)
        before = (self.state / "supervisor.json").read_bytes()
        result = self.invoke("early", "--engine", "codex", "--resume", "--codex-sandbox", "danger-full-access")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("saved Codex sandbox", result.stderr)
        self.assertEqual(len(self.launches()), 1)
        self.assertEqual((self.state / "supervisor.json").read_bytes(), before)

    def test_engine_change_requires_new_run_and_preserves_old_state(self):
        self.assertEqual(self.invoke("halt").returncode, 1)
        old = (self.state / "supervisor.json").read_bytes()
        result = self.invoke("early", "--resume", "--engine", "codex")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("--new-run", result.stderr)
        self.assertEqual((self.state / "supervisor.json").read_bytes(), old)
        self.assertEqual(len(self.launches()), 1)
        result = self.invoke("early", "--new-run", "--engine", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.load("supervisor.json")["engine"], "codex")
        archive = next((self.state / "history").iterdir())
        self.assertEqual(json.loads((archive / "supervisor.json").read_text())["engine"], "claude")

    def test_missing_selected_cli_never_falls_back_or_archives_state(self):
        self.assertEqual(self.invoke("halt").returncode, 1)
        old = (self.state / "supervisor.json").read_bytes()
        (self.bin / "codex").unlink()
        # Isolate PATH so a real installed Codex can never be selected by this test.
        for name in ("bash", "python3", "git", "dirname"):
            (self.bin / name).symlink_to(shutil.which(name))
        self.env["PATH"] = str(self.bin)
        result = self.invoke("early", "--engine", "codex", "--new-run")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("selected codex CLI is not on PATH", result.stderr)
        self.assertEqual((self.state / "supervisor.json").read_bytes(), old)
        self.assertFalse((self.state / "history").exists())
        self.assertEqual(len(self.launches()), 1)

    def test_auto_detection_refuses_ambiguity_without_launch(self):
        self.env.update(IMPROVE_ENGINE="auto", CODEX_THREAD_ID="test", CLAUDECODE="1")
        result = self.invoke()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("conflicting host markers", result.stderr)
        self.assertFalse((self.state / "supervisor.json").exists())
        self.assertFalse((self.state / "launches.jsonl").exists())

    def test_codex_expiry_and_report_retry_keep_engine(self):
        self.seed(-1)
        result = self.invoke("final_fail", "--engine", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.load()["summary_pending"])
        self.env.update(IMPROVE_ENGINE="auto", CLAUDECODE="1")
        result = self.invoke("empty", "--finalize", intake=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.load()["summary_pending"])
        for launch in self.launches():
            self.assertEqual(launch["engine"], "codex")
            self.assertIn("FINALIZATION ONLY", launch["prompt"])

    def test_codex_max_uses_max_prompt_and_target_rules(self):
        result = self.invoke("target", "--engine", "codex", "--skill", "improve-max", "--args=--stop-at-target")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.launches()[0]["prompt"].startswith("$improve-max "))
        self.assertIn("max/SKILL.md", self.launches()[0]["prompt"])
        self.assertEqual(self.load()["outcome"], "target reached")

    def test_codex_timeout_uses_same_process_supervision(self):
        self.env.update(IMPROVE_CYCLE_TIMEOUT="1", IMPROVE_MAX_FAILURES="1")
        result = self.invoke("timeout", "--engine", "codex")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("returned 124", result.stdout)
        self.assertIsNone(self.load("supervisor.json")["child_pid"])

    def test_dry_run_shows_codex_command_without_state_or_launch(self):
        result = self.invoke("early", "--engine", "codex", "--dry-run", intake=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("codex --no-daemon --ask-for-approval never exec", result.stdout)
        self.assertFalse(self.state.exists())

    def test_engine_environment_is_validated(self):
        self.env["IMPROVE_ENGINE"] = "unknown"
        result = self.invoke()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("IMPROVE_ENGINE", result.stderr)
        self.assertFalse((self.state / "launches.jsonl").exists())

    def test_early_completion_relaunches_and_fast_empty_scan_is_not_failure(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.launches()), 2)
        self.assertEqual(self.load()["outcome"], "stopped by user")
        self.assertIn("Rejected premature completion", (self.state / "journal.md").read_text())
        self.assertIn("fast but checkpointed", result.stdout)

    def test_restart_preserves_deadline_and_repairs_reset(self):
        old = self.seed()
        result = self.invoke("reset_deadline", duration="6h")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.load()["deadline"], old["deadline"])
        self.assertEqual(self.load("supervisor.json")["started_at"], old["started_at"])

    def test_expired_resume_only_finalizes(self):
        self.seed(remaining=-1)
        result = self.invoke("empty")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.launches()), 1)
        self.assertIn("FINALIZATION ONLY", self.launches()[0]["prompt"])
        self.assertEqual(self.load()["outcome"], "completed")

    def test_empty_discovery_keeps_running_to_deadline(self):
        result = self.invoke("empty", duration="2s")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(len(self.launches()), 1)
        run = self.load()
        self.assertGreaterEqual(runtime.epoch(run["ended_at"]), runtime.epoch(run["deadline"]))
        self.assertIn("FINALIZATION ONLY", self.launches()[-1]["prompt"])
        self.assertFalse(run["summary_pending"])

    def test_missing_intake_launches_no_agent(self):
        result = self.invoke(intake=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("ten intake questions", result.stderr)
        self.assertFalse((self.state / "launches.jsonl").exists())
        self.assertFalse((self.state / "supervisor.json").exists())

    def test_halt_and_dirty_tree_stop_after_one_launch(self):
        result = self.invoke("halt")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.launches()), 1)
        self.assertIn("required gate unavailable", self.load()["outcome"])

    def test_dirty_tree_is_never_reported_completed(self):
        result = self.invoke("dirty")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.launches()), 1)
        self.assertIn("uncommitted", self.load()["outcome"])
        self.assertTrue((self.repo / "uncommitted.txt").exists())

    def test_uncheckpointed_cycles_trip_breaker(self):
        result = self.invoke("no_checkpoint")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.launches()), 3)
        self.assertIn("uncheckpointed", self.load("supervisor.json")["outcome"])

    def test_timeout_works_without_coreutils(self):
        self.env.update(IMPROVE_CYCLE_TIMEOUT="1", IMPROVE_MAX_FAILURES="1")
        result = self.invoke("timeout")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(self.launches()), 1)
        self.assertIn("returned 124", result.stdout)

    def test_timeout_terminates_descendant_that_ignores_term(self):
        self.env.update(IMPROVE_CYCLE_TIMEOUT="1", IMPROVE_MAX_FAILURES="1")
        result = self.invoke("orphan")
        self.assertEqual(result.returncode, 1, result.stderr)
        time.sleep(2.2)
        self.assertFalse((self.state / "orphan-wrote").exists())

    def test_rate_limit_wait_is_capped_at_deadline_and_finalized(self):
        self.env["IMPROVE_LIMIT_WAIT"] = "900"
        result = self.invoke("limit", duration="2s")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.launches()), 2)
        self.assertEqual(self.load()["outcome"], "completed")

    def test_failed_finalization_reports_pending_summary(self):
        self.seed(-1)
        result = self.invoke("final_fail")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(self.load()["summary_pending"])

    def test_stop_interrupts_rate_limit_wait(self):
        command = self.command("--for", "60s", "--intake", str(self.intake))
        process = subprocess.Popen(command, env={**self.env, "FAKE_MODE": "limit"},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            until = time.monotonic() + 5
            while time.monotonic() < until:
                journal = self.state / "journal.md"
                if journal.exists() and "Account usage limit" in journal.read_text():
                    break
                time.sleep(0.05)
            else:
                self.fail("daemon never reached backoff")
            result = subprocess.run(self.command("--stop"), env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            process.communicate(timeout=3)
            self.assertEqual(self.load("supervisor.json")["outcome"], "stopped by user")
            self.assertEqual(len(self.launches()), 1)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_invalid_environment_is_rejected_before_launch(self):
        self.env["IMPROVE_CYCLE_TIMEOUT"] = "-1"
        result = self.invoke()
        self.assertEqual(result.returncode, 2)
        self.assertIn("IMPROVE_CYCLE_TIMEOUT", result.stderr)
        self.assertFalse(self.state.exists())

    def test_target_stop_requires_explicit_flag(self):
        result = self.invoke("target", "--skill", "improve-max", "--args=--stop-at-target")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.launches()), 1)
        self.assertEqual(self.load()["outcome"], "target reached")

    def test_status_and_dry_run_do_not_create_state(self):
        for args in (("--status",), ("--for", "08m", "--dry-run")):
            result = subprocess.run(self.command(*args), env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(self.state.exists())

    def test_lock_rejects_concurrent_runner(self):
        self.state.mkdir()
        path = self.state / "daemon.lock"
        with runtime.repo_lock(path):
            self.assertTrue(runtime.lock_active(path))
            result = self.invoke()
            self.assertEqual(result.returncode, 2)
            self.assertIn("already running", result.stderr)
        self.assertFalse(runtime.lock_active(path))

    def test_new_run_archives_finished_state(self):
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(self.invoke().returncode, 2)
        result = self.invoke("early", "--new-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        history = list((self.state / "history").iterdir())
        self.assertEqual(len(history), 1)
        self.assertTrue((history[0] / "run.json").exists())

    def test_clock_cli_exit_codes_and_json(self):
        run = self.seed()
        command = [str(ROOT / "scripts/improve-clock.sh"), "--json", str(self.repo)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["deadline"], run["deadline"])
        self.seed(-1)
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 10)
        (self.state / "run.json").write_text("{truncated")
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)

    def test_counter_only_is_not_progress(self):
        result = self.invoke("counter_only")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(self.launches()), 3)
        self.assertIn("no new evidence", self.launches()[1]["prompt"])

    def test_duplicate_scan_with_new_timestamps_is_not_progress(self):
        result = self.invoke("duplicate_scan")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(self.launches()), 4)

    def test_deadline_grace_bounds_long_cycle(self):
        self.env.update(IMPROVE_FINISH_GRACE="1", IMPROVE_CYCLE_TIMEOUT="3600")
        result = self.invoke("overrun", duration="1s")
        self.assertEqual(result.returncode, 0, result.stderr)
        run = self.load()
        # Bound cleanup against the saved deadline, excluding Python/git startup.
        # Allow process termination and report I/O on a busy test machine.
        self.assertLess(runtime.epoch(run["ended_at"]) - runtime.epoch(run["deadline"]), 7)
        self.assertEqual(run["outcome"], "completed")
        self.assertEqual(len(self.launches()), 2)

    def test_resume_preserves_intake_and_original_deadline(self):
        self.assertEqual(self.invoke("halt").returncode, 1)
        old = self.load()
        result = self.invoke("early", "--resume", intake=False, duration="6h")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.load()["deadline"], old["deadline"])
        self.assertEqual(self.load()["started_at"], old["started_at"])
        self.assertIn("Explicit resume", (self.state / "journal.md").read_text())

    def test_resume_requires_recovery_of_active_item(self):
        self.assertEqual(self.invoke("halt").returncode, 1)
        run = self.load()
        run["active_item"] = {"id": "q-1", "owned_paths": []}
        runtime.atomic_json(self.state / "run.json", run)
        result = self.invoke("early", "--resume")
        self.assertEqual(result.returncode, 2)
        self.assertIn("active_item", result.stderr)
        self.assertEqual(len(self.launches()), 1)

    def test_finalize_retries_pending_report_without_restarting_work(self):
        self.seed(-1)
        self.assertEqual(self.invoke("final_fail").returncode, 0)
        old = self.load()
        result = subprocess.run(self.command("--finalize"), env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.load()["ended_at"], old["ended_at"])
        self.assertEqual(self.load()["deadline"], old["deadline"])
        self.assertFalse(self.load()["summary_pending"])
        self.assertTrue(all("FINALIZATION ONLY" in row["prompt"] for row in self.launches()))

    def test_finalization_cannot_commit_and_report_success(self):
        self.seed(-1)
        result = self.invoke("final_commit")
        self.assertEqual(result.returncode, 1)
        self.assertIn("modified repository", self.load()["outcome"])
        self.assertTrue(self.load()["summary_pending"])

    def test_stale_report_does_not_count_as_new_finalization(self):
        self.seed(-1)
        (self.state / "final-report.md").write_text('Old report')
        result = self.invoke("final_no_report")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.load()["summary_pending"])

    def test_finalize_preserves_halt_outcome_and_end_time(self):
        self.assertEqual(self.invoke("halt").returncode, 1)
        old = self.load()
        result = self.invoke("empty", "--finalize")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.load()["outcome"], old["outcome"])
        self.assertEqual(self.load()["ended_at"], old["ended_at"])
        self.assertFalse(self.load()["summary_pending"])

    def test_restart_preserves_failure_breaker(self):
        run = self.seed()
        control = {**run, "skill": "improve", "args": "", "consecutive_failures": 2}
        runtime.atomic_json(self.state / "supervisor.json", control)
        self.env.update(IMPROVE_ENGINE="auto", CODEX_THREAD_ID="test")
        result = self.invoke("no_checkpoint")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.launches()), 1)
        self.assertEqual(self.launches()[0]["engine"], "claude")
        self.assertEqual(self.load("supervisor.json")["engine"], "claude")

    def test_expired_legacy_run_does_not_require_new_intake(self):
        self.seed(-1)
        result = self.invoke("empty", intake=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.launches()), 1)
        self.assertIn("FINALIZATION ONLY", self.launches()[0]["prompt"])

    def test_status_json_includes_next_action_and_clock(self):
        self.assertEqual(self.invoke().returncode, 0)
        result = subprocess.run(self.command("--status", "--json"), env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.assertFalse(info["running"])
        self.assertEqual(info["supervisor"]["phase"], "ended")
        self.assertEqual(info["supervisor"]["engine"], "claude")
        self.assertEqual(info["run"]["next_action"], "Inspect a new UI interaction state")
        self.assertGreater(info["clock"]["remaining_seconds"], 0)

    def test_stop_bounds_an_uncooperative_running_cycle(self):
        self.env["IMPROVE_FINISH_GRACE"] = "1"
        process = subprocess.Popen(self.command("--for", "60s", "--intake", str(self.intake)),
                                   env={**self.env, "FAKE_MODE": "timeout"},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            until = time.monotonic() + 5
            while time.monotonic() < until:
                control_path = self.state / "supervisor.json"
                if control_path.exists() and json.loads(control_path.read_text()).get("child_pid"):
                    break
                time.sleep(0.05)
            else:
                self.fail("cycle never started")
            subprocess.run(self.command("--stop"), env=self.env, check=True, capture_output=True)
            process.communicate(timeout=4)
            self.assertEqual(self.load("supervisor.json")["outcome"], "stopped by user")
            self.assertIsNone(self.load("supervisor.json")["child_pid"])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


if __name__ == "__main__":
    unittest.main()
