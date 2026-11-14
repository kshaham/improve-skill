"""Regression tests use disposable repositories and a fake CLI, never a paid agent."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from support import MAX_INSTALLED, budget, requires_max  # noqa: E402
sys.path.insert(0, str(ROOT / "scripts"))
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


class EvidenceTests(unittest.TestCase):
    def test_user_notes_and_priority_edits_are_not_improvement_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backlog.jsonl"
            row = dict(id="a", claim="An existing finding", evidence="Reproduction saved")
            path.write_text(json.dumps(row) + "\n")
            before = runtime.evidence_rows(path)
            row.update(user_priority="high", focus_priority=2, board_request_id="request-uuid",
                       user_notes=[dict(request_id="note-uuid", text="Please check mobile", at="new")])
            path.write_text(json.dumps(row) + "\n")
            self.assertEqual(runtime.evidence_rows(path), before)
            row["verification"] = "New failing mobile scenario reproduced"
            path.write_text(json.dumps(row) + "\n")
            self.assertNotEqual(runtime.evidence_rows(path), before)

    def test_lifecycle_edits_do_not_count_as_new_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backlog.jsonl"
            row = {"id": "a", "title": "Old label", "claim": "A real finding", "status": "ready",
                   "created_at": "old", "started_at": "old", "completed_at": "old"}
            path.write_text(json.dumps(row) + "\n")
            before = runtime.evidence_rows(path)
            row.update(id="new-id", title="Renamed", status="in_progress", created_at="new",
                       started_at="new", completed_at="new")
            path.write_text(json.dumps(row) + "\n")
            self.assertEqual(runtime.evidence_rows(path), before)
            row["verification"] = {"command": "pytest test_retry", "result": "passed"}
            path.write_text(json.dumps(row) + "\n")
            self.assertNotEqual(runtime.evidence_rows(path), before)

    def test_experiment_piece_commits_count_but_labels_and_timestamps_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bets.jsonl"
            row = {"id": "b", "hypothesis": "Faster feed", "pieces": [
                {"name": "handler", "status": "in-progress", "updated_at": "old"}]}
            path.write_text(json.dumps(row) + "\n")
            before = runtime.evidence_rows(path)
            row["pieces"][0].update(status="landed", updated_at="new")
            path.write_text(json.dumps(row) + "\n")
            self.assertEqual(runtime.evidence_rows(path), before)
            row["pieces"][0]["commit"] = "verified-commit"
            path.write_text(json.dumps(row) + "\n")
            self.assertNotEqual(runtime.evidence_rows(path), before)


FAKE_AGENT = r'''import json, os, pathlib, signal, subprocess, sys, time
state = pathlib.Path('.improve')
prompt = sys.argv[-1]
with (state / 'launches.jsonl').open('a') as stream:
    stream.write(json.dumps({'prompt': prompt, 'engine': pathlib.Path(sys.argv[0]).name,
                            'started_epoch': time.time(),
                            'argv': sys.argv[1:], 'nested_claude': os.environ.get('CLAUDECODE'),
                            'config_home': os.environ.get('CODEX_HOME'),
                            'stdin': sys.stdin.read()}) + '\n')
mode = os.environ.get('FAKE_MODE', 'empty')
script = json.loads(os.environ.get('FAKE_SCRIPT', 'null'))
if script:
    # One mode per launch, in order; the last entry repeats.
    launched = sum(1 for _ in (state / 'launches.jsonl').open())
    mode = script[min(launched - 1, len(script) - 1)]
scripts_dir = os.environ.get('IMPROVE_SCRIPTS', '')
if mode == 'crash_parent':
    os.kill(os.getppid(), signal.SIGKILL)
    time.sleep(8)  # Stay alive as the stale child a relaunched daemon must stop.
    sys.exit(0)
if mode == 'exit2':
    print('error: not logged in', file=sys.stderr)
    sys.exit(2)
if mode == 'limit0':
    print("You've hit your usage limit. Try again later.")
    sys.exit(0)
if mode == 'pause_break':
    (state / 'pause.json').write_text('{malformed')
if mode == 'big_output':
    print('x' * 200000)
if mode == 'bad_utf8':
    with (state / 'backlog.jsonl').open('ab') as stream:
        stream.write(b'\xff\xfe{"id":"zz"}\n')
if mode == 'orphan':
    orphan = subprocess.Popen([sys.executable, '-c', 'import signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(3); pathlib.Path(".improve/orphan-wrote").touch()'])
    (state / 'orphan.pid').write_text(str(orphan.pid))
if mode in ('timeout', 'orphan') or (mode == 'overrun' and 'FINALIZATION ONLY' not in prompt):
    time.sleep(30)
    sys.exit(0)
control = json.loads((state / 'supervisor.json').read_text())
if mode == 'controlled' and 'FINALIZATION ONLY' not in prompt:
    until = time.monotonic() + 10
    while time.monotonic() < until:
        pause_file = state / 'pause.json'
        paused = json.loads(pause_file.read_text()).get('paused') if pause_file.exists() else False
        if paused or (state / 'stop.request').exists():
            break
        time.sleep(0.02)
path = state / 'run.json'
run = json.loads(path.read_text()) if path.exists() else dict(
    started_at=control['started_at'], deadline=control['deadline'], outcome=None, cycle=0)
if mode.startswith('once_') and 'FINALIZATION ONLY' not in prompt:
    kind, number = mode[5:], run.get('cycle', 0) + 1
    if not (state / 'once-done').exists():
        (state / 'once-done').touch()
        with (state / 'backlog.jsonl').open('a') as stream:
            stream.write(json.dumps({'id': 't-1', 'title': 'Fragile task', 'area': 'ui', 'status': 'in_progress'}) + '\n')
        run['active_item'] = {'id': 't-1', 'title': 'Fragile task'}
        run['cycle'] = number
        if kind == 'halt':
            run['outcome'] = 'halted: test database unavailable'
        elif kind == 'dirty':
            pathlib.Path('half-done.txt').write_text('unverified work')
        elif kind == 'corrupt':
            with (state / 'backlog.jsonl').open('a') as stream:
                stream.write('{broken line\n')
            path.write_text('{not json')
            sys.exit(0)
        path.write_text(json.dumps(run))
        sys.exit(0)
    run['cycle'] = number
    with (state / 'discovery.jsonl').open('a') as stream:
        stream.write(json.dumps({'scope': 'lane-' + str(number), 'lane': 'ui', 'accepted': 0}) + '\n')
    if number >= 3:
        run['outcome'] = 'stopped by user'
    path.write_text(json.dumps(run))
    sys.exit(0)
if 'FINALIZATION ONLY' in prompt:
    if mode == 'final_fail':
        sys.exit(1)
    if mode == 'final_no_report':
        sys.exit(0)
    if mode == 'limit_final' and not (state / 'limit-final-done').exists():
        (state / 'limit-final-done').touch()
        print('usage limit reached')
        sys.exit(1)
    if mode == 'limit_final0' and not (state / 'limit-final-done').exists():
        (state / 'limit-final-done').touch()
        print("You've hit your usage limit. Try again later.")
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
    if mode == 'bet_progress':
        (state / 'bets.jsonl').write_text(json.dumps({'id':'b-1', 'hypothesis':'Improve feed latency',
            'spike': {'runs': list(range(run['cycle']))}}) + '\n')
        if run['cycle'] >= 5:
            run['outcome'] = 'stopped by user'
    elif mode == 'lifecycle_only':
        (state / 'backlog.jsonl').write_text(json.dumps({'id':'q-1', 'claim':'Same finding',
            'created_at': str(run['cycle']), 'completed_at': str(run['cycle'])}) + '\n')
    elif mode != 'counter_only':
        with (state / 'discovery.jsonl').open('a') as stream:
            stream.write(json.dumps({'scope': 'same' if mode == 'duplicate_scan' else str(run['cycle']),
                                     'accepted': 0, 'at': str(time.time()), 'generation': run['cycle']}) + '\n')
    if mode == 'early':
        run['outcome'] = 'completed' if run['cycle'] == 1 else 'stopped by user'
    elif mode == 'stop':
        run['outcome'] = 'stopped by user'
    elif mode == 'bad_outcome':
        run['outcome'] = 'done'
    elif mode == 'bad_report_hour':
        run['last_report_hour'] = 3.0
    elif mode == 'bad_cycle':
        run['cycle'] = str(run['cycle'])
    elif mode == 'artifacts':
        pathlib.Path('build').mkdir(exist_ok=True)
        pathlib.Path('build/out.o').write_text('object code')
        pathlib.Path('__pycache__').mkdir(exist_ok=True)
        pathlib.Path('__pycache__/m.cpython-313.pyc').write_text('bytecode')
    elif mode == 'newfile':
        pathlib.Path('notes.txt').write_text('unverified work')
    elif mode == 'custom_artifact':
        run['artifact_paths'] = ['generated/*']
        pathlib.Path('generated').mkdir(exist_ok=True)
        pathlib.Path('generated/schema.bin').write_text('generated output')
    elif mode == 'unreadable':
        secret = pathlib.Path('secret.txt')
        secret.write_text('unverified work git cannot read')
        secret.chmod(0)
    elif mode == 'tracked_dirty':
        with open('README.txt', 'a') as stream:
            stream.write('edited\n')
    elif mode == 'commit':
        with open('README.txt', 'a') as stream:
            stream.write('verified change ' + str(run['cycle']) + '\n')
        subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false',
                        'commit', '-qam', 'fix: verified change'], check=True)
    elif mode == 'uses_helpers':
        results = {}
        for name, args in (('pending', ['improve_control.py', '--repo', '.', 'pending']),
                           ('note', ['improve_graph.py', '--repo', '.', 'note', '--kind', 'pitfall', '--text',
                                     'The fixture gate needs a warm cache first.', '--about', 'area:ui']),
                           ('context', ['improve_graph.py', '--repo', '.', 'context', '--area', 'ui']),
                           ('lesson', ['improve_learning.py', '--repo', '.', 'lesson', '--area', 'ui', '--text',
                                       'Warm the cache before the fixture gate.'])):
            done = subprocess.run([sys.executable, os.path.join(scripts_dir, args[0]), *args[1:]], capture_output=True, text=True)
            results[name] = {'rc': done.returncode, 'out': done.stdout[-2000:], 'err': done.stderr[-500:]}
        (state / 'helpers.json').write_text(json.dumps(results))
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
        (self.repo / "README.txt").write_text("fixture\n")
        self.git("add", "README.txt")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
                 "commit", "-qm", "baseline")
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
                        IMPROVE_ENGINE="claude", IMPROVE_CYCLE_GAP="0", PYTHONDONTWRITEBYTECODE="1",
                        IMPROVE_LEARNING_HOME=str(self.root / "learning-home"), IMPROVE_RECOVERY_COOLDOWN="0",
                        IMPROVE_IDLE_AFTER="1000", IMPROVE_SCRIPTS=str(ROOT / "scripts"),
                        GIT_CONFIG_GLOBAL=str(self.root / "gitconfig"), GIT_CONFIG_NOSYSTEM="1")
        (self.root / "gitconfig").write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n[commit]\n\tgpgsign = false\n")
        # Legacy tests pin the strict halting contract; ResilienceTests opt into recovery.
        self.env.update(IMPROVE_MAX_RECOVERIES="0", IMPROVE_MAX_WORKER_HALTS="0", IMPROVE_QUARANTINE="0")

    def tearDown(self):
        if (self.state / "board.json").exists():
            subprocess.run([str(ROOT / "scripts/improve-board.sh"), "--repo", str(self.repo), "--stop"],
                           capture_output=True, timeout=budget(5))
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
                                capture_output=True, text=True, timeout=budget(5))
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

    def invoke(self, mode="early", *extra, intake=True, duration="60s", env=None, script=None, timeout=budget(25)):
        args = ["--for", duration, *extra]
        if intake:
            args += ["--intake", str(self.intake)]
        environment = {**self.env, "FAKE_MODE": mode, **(env or {})}
        if script is not None:
            environment["FAKE_SCRIPT"] = json.dumps(script)
        return subprocess.run(self.command(*args), env=environment, capture_output=True, text=True, timeout=timeout)

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

    def wait_control(self, predicate, timeout=budget(5)):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            try:
                value = self.load('supervisor.json')
                if predicate(value):
                    return value
            except (FileNotFoundError, ValueError):
                pass
            time.sleep(0.02)
        self.fail('Supervisor did not reach the expected state')

    def check_board_pause(self, engine):
        import improve_control as controls
        self.seed_control(engine=engine, consecutive_failures=2, model='saved-model')
        proc = subprocess.Popen(self.command('--for', '60s', '--intake', str(self.intake), '--engine', engine),
                                env={**self.env, 'FAKE_MODE': 'controlled', 'IMPROVE_FINISH_GRACE': '1'},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            saved = self.wait_control(lambda value: bool(value.get('child_pid')))
            with controls.operator_lock(self.repo):
                controls.set_pause(self.repo, saved['run_id'], True)
            paused = self.wait_control(lambda value: value.get('phase') == 'paused')
            self.assertEqual(paused['consecutive_failures'], 2)
            self.assertIsNone(paused.get('child_pid'))
            launches = len(self.launches())
            time.sleep(0.25)
            self.assertEqual(len(self.launches()), launches)
            with controls.operator_lock(self.repo):
                controls.set_pause(self.repo, saved['run_id'], False)
            self.wait_control(lambda value: bool(value.get('child_pid')))
            with controls.operator_lock(self.repo):
                controls.set_pause(self.repo, saved['run_id'], True)
            self.wait_control(lambda value: value.get('phase') == 'paused')
            self.assertEqual(len(self.launches()), launches + 1)
            (self.state / 'stop.request').touch()
            out, err = proc.communicate(timeout=budget(5))
            self.assertEqual(proc.returncode, 0, out + err)
            self.assertEqual(self.load('supervisor.json')['outcome'], 'stopped by user')
            self.assertEqual(self.load('supervisor.json')['deadline'], saved['deadline'])
            self.assertEqual(self.load('supervisor.json')['model'], 'saved-model')
            self.assertEqual({row['engine'] for row in self.launches()}, {engine})
        finally:
            if proc.poll() is None:
                (self.state / 'stop.request').touch()
                proc.communicate(timeout=budget(5))
            proc.stdout.close()
            proc.stderr.close()

    def test_board_pause_resume_and_stop_claude(self):
        self.check_board_pause('claude')

    def test_board_pause_resume_and_stop_codex(self):
        self.check_board_pause('codex')

    def test_pause_survives_restart_but_never_extends_deadline(self):
        import improve_control as controls
        saved = self.seed_control(run_id='stable-run', deadline=runtime.stamp(time.time() + 3))
        with controls.operator_lock(self.repo):
            controls.set_pause(self.repo, saved['run_id'], True)
        result = self.invoke('early')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.load('supervisor.json')['deadline'], saved['deadline'])
        self.assertEqual(self.load('supervisor.json')['outcome'], 'completed')
        self.assertEqual(len(self.launches()), 1)
        self.assertIn('FINALIZATION ONLY', self.launches()[0]['prompt'])

    def test_explicit_resume_clears_pause_but_keeps_run_identity(self):
        import improve_control as controls
        saved = self.seed_control(run_id='stable-run', outcome='stopped by user')
        with controls.operator_lock(self.repo):
            controls.set_pause(self.repo, saved['run_id'], True)
        result = self.invoke('early', '--resume')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(controls.pause_requested(self.repo, saved['run_id']))
        self.assertEqual(self.load('supervisor.json')['run_id'], saved['run_id'])

    def test_new_run_archives_requests_and_preserves_operator_lock(self):
        import improve_control as controls
        self.assertEqual(self.invoke().returncode, 0)
        old_id = self.load('supervisor.json')['run_id']
        with controls.operator_lock(self.repo):
            controls.set_pause(self.repo, old_id, True)
        runtime.atomic_json(self.state / 'operator.json', {'requests': []})
        runtime.atomic_json(self.state / 'board-receipts.json', {})
        inode = (self.state / 'operator.lock').stat().st_ino
        self.assertEqual(self.invoke('early', '--new-run').returncode, 0)
        self.assertNotEqual(self.load('supervisor.json')['run_id'], old_id)
        self.assertEqual((self.state / 'operator.lock').stat().st_ino, inode)
        archive = next((self.state / 'history').iterdir())
        for name in ('operator.json', 'board-receipts.json', 'pause.json'):
            self.assertTrue((archive / name).is_file())
            self.assertFalse((self.state / name).exists())

    def test_malformed_pause_state_halts_without_losing_recovery_state(self):
        self.seed_control(run_id='stable-run')
        path = self.state / 'pause.json'
        path.write_text('{malformed')
        result = self.invoke()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertTrue(self.load('supervisor.json')['outcome'].startswith('halted:'))
        self.assertEqual(path.read_text(), '{malformed')
        self.assertFalse((self.state / 'launches.jsonl').exists())

    def test_pause_preserves_account_limit_retry_and_resume_restores_wait_phase(self):
        import improve_control as controls
        saved = self.seed_control(run_id='rate-limited-run', phase='account-limit',
                                  retry_at=runtime.stamp(time.time() + 10),
                                  limit_wait_started_at=runtime.stamp(time.time()), limit_waited_seconds=3)
        proc = subprocess.Popen(self.command('--for', '60s', '--intake', str(self.intake)),
                                env={**self.env, 'FAKE_MODE': 'early'},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.wait_control(lambda value: value.get('board_control_version') == 1)
            with controls.operator_lock(self.repo):
                controls.set_pause(self.repo, saved['run_id'], True)
            paused = self.wait_control(lambda value: value.get('phase') == 'paused')
            self.assertEqual(paused['retry_at'], saved['retry_at'])
            with controls.operator_lock(self.repo):
                controls.set_pause(self.repo, saved['run_id'], False)
            self.wait_control(lambda value: value.get('phase') == 'account-limit')
            self.assertFalse((self.state / 'launches.jsonl').exists())
            (self.state / 'stop.request').touch()
            out, err = proc.communicate(timeout=budget(5))
            self.assertEqual(proc.returncode, 0, out + err)
            self.assertGreaterEqual(self.load('supervisor.json')['limit_waited_seconds'], 3)
        finally:
            if proc.poll() is None:
                (self.state / 'stop.request').touch()
                proc.communicate(timeout=budget(5))
            proc.stdout.close()
            proc.stderr.close()

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

    @requires_max
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

    @unittest.skipIf(MAX_INSTALLED, "improve-max is installed")
    def test_improve_max_without_its_package_is_refused_before_any_launch(self):
        result = self.invoke("early", "--skill", "improve-max")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("improve-max is not installed", result.stderr)
        self.assertFalse((self.state / "launches.jsonl").exists())

    @requires_max
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
        import improve_resilience as resilience
        pid = int((self.state / "orphan.pid").read_text())
        until = time.monotonic() + budget(2)
        while resilience.process_command(pid) is not None and time.monotonic() < until:
            time.sleep(0.05)
        self.assertIsNone(resilience.process_command(pid), "the TERM-ignoring descendant was killed with its group")
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
            until = time.monotonic() + budget(5)
            while time.monotonic() < until:
                journal = self.state / "journal.md"
                if journal.exists() and "Account usage limit" in journal.read_text():
                    break
                time.sleep(0.05)
            else:
                self.fail("daemon never reached backoff")
            result = subprocess.run(self.command("--stop"), env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            process.communicate(timeout=budget(3))
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

    @requires_max
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

    @requires_max
    def test_experiment_evidence_keeps_max_running_across_cycles(self):
        result = self.invoke("bet_progress", "--skill", "improve-max")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.launches()), 5)
        self.assertEqual(self.load("supervisor.json")["consecutive_failures"], 0)

    def test_task_lifecycle_timestamps_do_not_bypass_failure_breaker(self):
        result = self.invoke("lifecycle_only")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(self.launches()), 4)

    def test_repeated_intake_preserves_later_authorization_and_metadata(self):
        self.assertEqual(self.invoke("halt").returncode, 1)
        saved = self.load("intake.json")
        saved.update(authorization={"grants": ["Push and merge after tests"], "exclusions": ["No paid services"]},
                     steering=["Prioritize checkout"], status="complete")
        runtime.atomic_json(self.state / "intake.json", saved)
        before = (self.state / "intake.json").read_bytes()
        result = self.invoke("early", "--resume")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.state / "intake.json").read_bytes(), before)

    def test_rejected_recovery_does_not_install_supplied_intake(self):
        self.seed_control(outcome=None)
        result = self.invoke("early", "--resume")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.state / "intake.json").exists())
        self.assertFalse((self.state / "launches.jsonl").exists())

    def test_awaiting_answers_intake_cannot_start_or_archive_a_run(self):
        self.seed_control(outcome="stopped by user")
        before = (self.state / "supervisor.json").read_bytes()
        supplied = json.loads(self.intake.read_text())
        supplied["status"] = "awaiting_answers"
        self.intake.write_text(json.dumps(supplied))
        result = self.invoke("early", "--new-run")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual((self.state / "supervisor.json").read_bytes(), before)
        self.assertFalse((self.state / "history").exists())
        self.assertFalse((self.state / "launches.jsonl").exists())

    def test_deadline_grace_bounds_long_cycle(self):
        self.env.update(IMPROVE_FINISH_GRACE="1", IMPROVE_CYCLE_TIMEOUT="3600")
        result = self.invoke("overrun", duration="1s")
        self.assertEqual(result.returncode, 0, result.stderr)
        run = self.load()
        # Bound cleanup against the saved deadline, excluding Python/git startup.
        # Allow process termination and report I/O on a busy test machine.
        self.assertLess(runtime.epoch(run["ended_at"]) - runtime.epoch(run["deadline"]), budget(7))
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
        result = subprocess.run(self.command("--finalize"), env=self.env, capture_output=True, text=True, timeout=budget(5))
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
            until = time.monotonic() + budget(5)
            while time.monotonic() < until:
                control_path = self.state / "supervisor.json"
                if control_path.exists() and json.loads(control_path.read_text()).get("child_pid"):
                    break
                time.sleep(0.05)
            else:
                self.fail("cycle never started")
            subprocess.run(self.command("--stop"), env=self.env, check=True, capture_output=True)
            process.communicate(timeout=budget(4))
            self.assertEqual(self.load("supervisor.json")["outcome"], "stopped by user")
            self.assertIsNone(self.load("supervisor.json")["child_pid"])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


# Engine routing, pause, intake, finalization, and limit behavior that must hold in the default
# resilient configuration too, where run() contains errors instead of re-raising them. Tests that
# rely on a worker halt ending the run stay strict-only: by default such halts are softened.
DEFAULT_MODE_TESTS = (
    "test_codex_host_launches_codex_and_relaunches_until_stopped",
    "test_claude_host_launches_without_nested_session_marker",
    "test_auto_detection_refuses_ambiguity_without_launch",
    "test_codex_max_uses_max_prompt_and_target_rules",
    "test_dry_run_shows_codex_command_without_state_or_launch",
    "test_engine_environment_is_validated",
    "test_codex_expiry_and_report_retry_keep_engine",
    "test_board_pause_resume_and_stop_claude",
    "test_board_pause_resume_and_stop_codex",
    "test_pause_survives_restart_but_never_extends_deadline",
    "test_explicit_resume_clears_pause_but_keeps_run_identity",
    "test_pause_preserves_account_limit_retry_and_resume_restores_wait_phase",
    "test_missing_intake_launches_no_agent",
    "test_rejected_recovery_does_not_install_supplied_intake",
    "test_awaiting_answers_intake_cannot_start_or_archive_a_run",
    "test_expired_legacy_run_does_not_require_new_intake",
    "test_finalize_retries_pending_report_without_restarting_work",
    "test_failed_finalization_reports_pending_summary",
    "test_stale_report_does_not_count_as_new_finalization",
    "test_finalization_cannot_commit_and_report_success",
    "test_expired_resume_only_finalizes",
    "test_cycle_completion_at_deadline_still_gets_final_report",
    "test_empty_discovery_keeps_running_to_deadline",
    "test_restart_waits_for_recorded_rate_limit_retry",
    "test_expired_retry_accounts_downtime_without_sleeping_again",
    "test_restart_after_deadline_accounts_pending_wait_before_final_report",
    "test_invalid_saved_retry_does_not_launch_or_erase_state",
    "test_rate_limit_wait_is_capped_at_deadline_and_finalized",
    "test_stop_interrupts_rate_limit_wait",
    "test_restart_preserves_deadline_and_repairs_reset",
    "test_early_completion_relaunches_and_fast_empty_scan_is_not_failure",
    "test_stop_bounds_an_uncooperative_running_cycle",
    "test_deadline_grace_bounds_long_cycle",
    "test_status_and_dry_run_do_not_create_state",
)


class DefaultModeTests(DaemonTests):
    """DaemonTests' routing, pause, intake, finalize, and limit cases with no strict switches."""

    def setUp(self):
        super().setUp()
        for key in ("IMPROVE_MAX_RECOVERIES", "IMPROVE_MAX_WORKER_HALTS", "IMPROVE_QUARANTINE"):
            self.env.pop(key)


for _name in [name for name in vars(DaemonTests) if name.startswith("test_")]:
    if _name not in DEFAULT_MODE_TESTS:
        setattr(DefaultModeTests, _name, None)


class ResilienceTests(DaemonTests):
    """Default resilient behavior: block, work around, and keep the run going."""

    def setUp(self):
        super().setUp()
        for key in ("IMPROVE_MAX_RECOVERIES", "IMPROVE_MAX_WORKER_HALTS", "IMPROVE_QUARANTINE"):
            self.env.pop(key)

    def tasks(self):
        merged = {}
        for line in (self.state / "backlog.jsonl").read_text().splitlines():
            row = json.loads(line)
            merged[row["id"]] = {**merged.get(row["id"], {}), **row}
        return merged

    def test_worker_halt_blocks_the_task_and_the_run_continues(self):
        result = self.invoke("once_halt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(len(self.launches()), 3)
        task = self.tasks()["t-1"]
        self.assertEqual(task["status"], "blocked")
        self.assertIn("test database unavailable", task["blocked_reason"])
        self.assertEqual(self.load("supervisor.json")["outcome"], "stopped by user")
        self.assertIn("Work around it", self.launches()[1]["prompt"])
        self.assertIn("Blocked tasks", self.launches()[1]["prompt"])

    def test_repeated_worker_halts_park_and_the_run_reaches_its_deadline(self):
        result = self.invoke("halt", duration=f"{round(budget(5))}s", env={"IMPROVE_PARK_WAIT": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        control = self.load("supervisor.json")
        self.assertEqual(control["outcome"], "completed", "repeated halts park like every other breaker")
        self.assertGreaterEqual(control["parks"], 1)
        self.assertIn("worker halts in a row", (self.state / "journal.md").read_text())
        self.assertGreater(len(self.launches()), 3)
        self.assertIn("FINALIZATION ONLY", self.launches()[-1]["prompt"])

    def test_repeated_worker_halts_are_honored_when_parking_is_disabled(self):
        result = self.invoke("halt", env={"IMPROVE_PARK": "0"})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.launches()), 3)  # Two softened halts, then the third is honored.
        self.assertIn("required gate unavailable", self.load("supervisor.json")["outcome"])

    def test_uncommitted_cycle_work_is_quarantined_not_lost(self):
        result = self.invoke("once_dirty")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.repo / "half-done.txt").exists())
        refs = self.git("for-each-ref", "--format=%(refname)", "refs/improve/quarantine").stdout.decode().split()
        self.assertEqual(len(refs), 1)
        saved = self.git("show", refs[0] + ":half-done.txt").stdout.decode()
        self.assertEqual(saved, "unverified work")
        self.assertEqual(self.tasks()["t-1"]["status"], "blocked")
        self.assertIn(refs[0], self.launches()[1]["prompt"])
        self.assertEqual(self.load("supervisor.json")["quarantines"], 1)

    def test_corrupted_state_is_repaired_and_work_continues(self):
        result = self.invoke("once_corrupt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("{broken line", (self.state / "backlog.jsonl.damaged").read_text())
        self.assertTrue((self.state / "run.damaged.json").exists())
        self.assertIn("damaged saved state", self.launches()[1]["prompt"])

    def test_failure_breaker_recovers_before_halting(self):
        result = subprocess.run(self.command("--for", "60s", "--intake", str(self.intake)),
                                env={**self.env, "FAKE_MODE": "no_checkpoint", "IMPROVE_MAX_RECOVERIES": "1", "IMPROVE_PARK": "0"},
                                capture_output=True, text=True, timeout=budget(15))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.launches()), 6)
        self.assertIn("Supervisor recovery 1", self.launches()[3]["prompt"])
        self.assertIn("after 1 recoveries", self.load("supervisor.json")["outcome"])

    def test_learning_survives_new_runs_and_reaches_the_next_prompt(self):
        self.assertEqual(self.invoke("once_halt").returncode, 0)
        learning = self.load("learning.json")
        self.assertEqual(len(learning["runs"]), 1)
        self.assertEqual(learning["areas"]["ui"]["blocked"], 1)
        helper = [sys.executable, str(ROOT / "scripts/improve_learning.py"), "--repo", str(self.repo), "lesson",
                  "--area", "coverage", "--text", "Run the integration suite with --serial; parallel runs flake."]
        for _ in range(2):  # Repeating a lesson reinforces it instead of duplicating it.
            subprocess.run(helper, env=self.env, check=True, capture_output=True)
        subprocess.run([*helper[:4], "lesson", "--scope", "global", "--text", "Prefer atomic ledger writes."],
                       env=self.env, check=True, capture_output=True)
        self.assertEqual(self.load("learning.json")["lessons"][0]["reinforced"], 2)
        (self.state / "once-done").unlink()
        result = self.invoke("once_halt", "--new-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = self.launches()[0]["prompt"]
        self.assertIn("parallel runs flake", prompt)
        self.assertIn("Prefer atomic ledger writes", prompt)
        self.assertEqual(len(self.load("learning.json")["runs"]), 2)
        self.assertIn("test database unavailable", self.load("learning.json")["blockers"][0]["example"])

    def graph(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import improve_graph
        return improve_graph.load(improve_graph.graph_path(self.repo), repair=False)

    def test_knowledge_graph_survives_new_runs_and_reaches_the_next_prompt(self):
        self.assertEqual(self.invoke("once_halt").returncode, 0)
        saved = self.graph()
        self.assertEqual(len(saved["runs"]), 1)
        self.assertTrue(saved["refine"]["history"], "a finished run is refined")
        self.assertEqual(saved["tick"], 1, "scheduled dreams wait for their interval; the run-end dream always runs")
        self.assertEqual(len(saved["dream"]["history"]), 1)
        self.assertTrue((self.state / "graph.backup.db").exists(), "a dream leaves a checkpoint")
        self.assertTrue(any(node["type"] == "task" for node in saved["nodes"].values()))
        helper = [sys.executable, str(ROOT / "scripts/improve_graph.py"), "--repo", str(self.repo), "note",
                  "--kind", "pitfall", "--text", "The fixture gate needs a warm cache first.", "--about", "area:ui"]
        import improve_toon as toon
        note = toon.loads(subprocess.run(helper, env=self.env, check=True, capture_output=True, text=True).stdout)
        (self.state / "once-done").unlink()
        result = self.invoke("once_halt", "--new-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = self.launches()[0]["prompt"]
        self.assertIn("improve_graph.py", prompt)
        self.assertIn("graph_refs", prompt)
        self.assertIn("restore ID", prompt, "agents are told how to bring back forgotten knowledge")
        self.assertIn(note["id"], prompt)
        self.assertIn("warm cache", prompt)
        saved = self.graph()
        self.assertEqual(len(saved["runs"]), 2)
        self.assertEqual(saved["tick"], 2, "the last dream is recent, so only the run-end dream ran")
        archives = list((self.state / "history").iterdir())
        for name in ("graph.db", "graph.backup.db", "graph.lock"):
            self.assertFalse(any((path / name).exists() for path in archives), f"{name} must stay with the repository")

    def test_daemon_dreams_on_schedule_during_a_long_run(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import improve_graph
        self.env.update(IMPROVE_GRAPH_DREAM_MINUTES="0")
        self.assertEqual(self.invoke("once_halt").returncode, 0)
        self.assertEqual(self.graph()["tick"], 1, "0 disables scheduled dreams; only the end-of-run dream ran")
        with improve_graph.locked(self.repo) as value:
            value["dream"]["last_at"] = "2000-01-01T00:00:00Z"  # Long overdue.
        (self.state / "once-done").unlink()
        self.env.update(IMPROVE_GRAPH_DREAM_MINUTES="180")
        result = self.invoke("once_halt", "--new-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.graph()["tick"], 3, "an overdue dream ran after a cycle, then the run-end dream")
        self.assertIn("knowledge graph dreamed (tick 2)", (self.state / "daemon.log").read_text())


# Inherit DaemonTests' fixture, not its strict-mode assertions.
for _name in [name for name in vars(DaemonTests) if name.startswith("test_")]:
    if _name not in vars(ResilienceTests):
        setattr(ResilienceTests, _name, None)


class LongRunResilienceTests(ResilienceTests):
    """What a 48-72 hour unattended run meets: bad agent writes, limits with exit 0, build
    artifacts, CLI crashes, supervisor errors, daemon crashes, signals, and dry stretches."""

    def control(self):
        return self.load("supervisor.json")

    def journal(self):
        return (self.state / "journal.md").read_text() if (self.state / "journal.md").exists() else ""

    def test_malformed_agent_fields_are_repaired_and_the_run_continues(self):
        result = self.invoke(script=["bad_outcome", "bad_report_hour", "bad_cycle", "stop"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.launches()), 4)
        self.assertEqual(self.control()["outcome"], "stopped by user")
        journal = self.journal()
        self.assertIn("outcome 'done' is not a recognized terminal outcome", journal)
        self.assertIn("last_report_hour 3.0", journal)
        run = self.load()
        self.assertIs(type(run["cycle"]), int)
        self.assertEqual(run["cycle"], 4)
        clock = subprocess.run([str(ROOT / "scripts/improve-clock.sh"), "--json", str(self.repo)],
                               capture_output=True, text=True, timeout=budget(5))
        self.assertEqual(clock.returncode, 0, clock.stderr)

    def test_undecodable_ledger_bytes_are_repaired_not_fatal(self):
        result = self.invoke(script=["bad_utf8", "stop"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.control()["outcome"], "stopped by user")
        self.assertIn("not UTF-8", self.journal())
        (self.state / "backlog.jsonl").read_text()  # Decodes cleanly after repair.

    def test_usage_limit_with_exit_zero_waits_instead_of_failing(self):
        result = self.invoke(script=["limit0", "stop"], env={"IMPROVE_LIMIT_WAIT": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        control = self.control()
        self.assertEqual(control["outcome"], "stopped by user")
        self.assertEqual(control.get("consecutive_failures", 0), 0)
        self.assertGreaterEqual(control["limit_waited_seconds"], 1)
        self.assertFalse((self.state / "backlog.jsonl").exists(), "nothing was blocked for a usage limit")
        self.assertIn("Account usage limit", self.journal())
        self.assertTrue(control["cycle_history"][0]["limited"])

    def test_build_artifacts_are_cleaned_without_blocking_or_failing(self):
        result = self.invoke(script=["artifacts", "artifacts", "artifacts", "artifacts", "stop"])
        self.assertEqual(result.returncode, 0, result.stderr)
        control = self.control()
        self.assertEqual(control["outcome"], "stopped by user")
        self.assertEqual(control["artifact_cleanups"], 4)
        self.assertNotIn("quarantines", control)
        self.assertEqual(control.get("recoveries", 0), 0)
        self.assertFalse((self.state / "backlog.jsonl").exists(), "artifacts never block a task")
        self.assertIn("build artifacts", self.launches()[1]["prompt"])
        self.assertFalse((self.repo / "build").exists())
        self.assertEqual(self.git("status", "--porcelain", "--", ".", ":(exclude).improve").stdout, b"")
        self.assertEqual(self.git("for-each-ref", "refs/improve").stdout, b"",
                         "build output is never committed to a quarantine ref")
        self.assertIn("build/", self.journal())
        self.assertIn("build/", control["last_artifacts"])

    def test_new_untracked_source_files_still_count_as_unverified_work(self):
        (self.state).mkdir(exist_ok=True)
        result = self.invoke(script=["newfile", "stop"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.control()["quarantines"], 1)
        self.assertIn("uncommitted changes", self.launches()[1]["prompt"])

    def test_cli_crashes_do_not_block_the_task_in_flight(self):
        self.state.mkdir(exist_ok=True)
        (self.state / "backlog.jsonl").write_text(json.dumps({"id": "t-9", "title": "Real task", "area": "ui",
                                                              "status": "in_progress", "note": "evidence"}) + "\n")
        result = self.invoke(script=["exit2", "exit2", "exit2", "stop"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.launches()), 4)
        task = self.tasks()["t-9"]
        self.assertEqual(task["status"], "in_progress", "a CLI exit says nothing about the task")
        self.assertEqual(task["note"], "evidence")
        self.assertIn("Supervisor recovery 1", self.journal())
        self.assertIn("exited 2 without a checkpoint", self.launches()[1]["prompt"])

    def test_exhausted_breakers_park_the_run_instead_of_halting(self):
        result = self.invoke("no_checkpoint", duration=f"{round(budget(6))}s",
                             env={"IMPROVE_MAX_FAILURES": "2", "IMPROVE_MAX_RECOVERIES": "1", "IMPROVE_PARK_WAIT": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        control = self.control()
        self.assertEqual(control["outcome"], "completed", "the run reached its deadline instead of halting")
        self.assertGreaterEqual(control["parks"], 1)
        self.assertIn("Parked (1)", self.journal())
        self.assertGreater(len(self.launches()), 4)

    def test_supervisor_errors_are_contained_and_the_run_reaches_its_deadline(self):
        result = self.invoke(script=["pause_break", "empty"], duration=f"{round(budget(5))}s",
                             env={"IMPROVE_MAX_CRASHES": "2", "IMPROVE_PARK_WAIT": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        control = self.control()
        self.assertEqual(control["outcome"], "completed")
        self.assertIn("Supervisor error during a cycle", self.journal())
        self.assertIn("Parked", self.journal())
        self.assertEqual((self.state / "pause.json").read_text(), "{malformed", "diagnostic state is preserved")

    def test_supervise_relaunches_after_a_daemon_crash_and_stops_the_stale_child(self):
        result = self.invoke("early", "--supervise", script=["crash_parent", "stop"], env={"IMPROVE_RELAUNCH_WAIT": "0"})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        control = self.control()
        self.assertEqual(control["outcome"], "stopped by user")
        self.assertIn("relaunching", result.stdout)
        self.assertIn("Stopped a stale agent process", self.journal())
        self.assertEqual(len(self.launches()), 2)  # the crashed cycle, then the resumed one

    def test_low_disk_space_parks_instead_of_launching(self):
        result = self.invoke("stop", duration="4s", env={"IMPROVE_MIN_FREE_MB": "99999999", "IMPROVE_PARK_WAIT": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        prompts = [row["prompt"] for row in self.launches()]
        self.assertTrue(all("FINALIZATION ONLY" in prompt for prompt in prompts), "no work cycle was launched onto a full disk")
        self.assertIn("MB free", self.journal())
        self.assertEqual(self.control()["outcome"], "completed")

    def test_status_reports_a_crashed_daemon(self):
        self.seed_control(run_id="crashed-run", phase="working", child_pid=None)
        status = subprocess.run(self.command("--status", "--json"), env=self.env, capture_output=True, text=True, timeout=budget(5))
        info = json.loads(status.stdout)
        self.assertFalse(info["running"])
        self.assertTrue(info["crashed"])
        self.assertEqual(info["quarantine"], [])
        text = subprocess.run(self.command("--status"), env=self.env, capture_output=True, text=True, timeout=budget(5))
        self.assertIn("crashed", text.stdout)

    def test_stale_child_detection_and_termination(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import improve_resilience as resilience
        self.state.mkdir(exist_ok=True)
        child = subprocess.Popen([str(self.bin / "claude"), "-p", "stale"], cwd=self.repo, env={**self.env, "FAKE_MODE": "timeout"},
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            time.sleep(0.5)
            self.assertEqual(resilience.stale_child({"child_pid": child.pid, "engine": "claude"}), child.pid)
            self.assertIsNone(resilience.stale_child({"child_pid": child.pid, "engine": "codex"}), "a reused pid runs something else")
            self.assertIsNone(resilience.stale_child({"child_pid": os.getpid(), "engine": "claude"}))
            self.assertTrue(resilience.terminate_group(child.pid, grace=1))
            self.assertIn(child.wait(timeout=budget(3)), (-signal.SIGTERM, -signal.SIGKILL), "ended by the group signal")
        finally:
            if child.poll() is None:
                child.kill()
        self.assertIsNone(resilience.stale_child({"child_pid": child.pid, "engine": "claude"}))

    def test_sigterm_ends_the_run_as_a_user_stop(self):
        process = subprocess.Popen(self.command("--for", "60s", "--intake", str(self.intake)),
                                   env={**self.env, "FAKE_MODE": "controlled"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            until = time.monotonic() + budget(10)
            while time.monotonic() < until and not (self.state / "launches.jsonl").exists():
                time.sleep(0.05)
            process.send_signal(signal.SIGTERM)
            out, err = process.communicate(timeout=budget(15))
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertEqual(process.returncode, 0, err)
        self.assertEqual(self.control()["outcome"], "stopped by user")

    def test_finalization_retries_after_a_usage_limit(self):
        result = self.invoke(script=["completed_at_deadline", "limit_final", "empty"], duration="2s",
                             env={"IMPROVE_LIMIT_WAIT": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        control = self.control()
        self.assertEqual(control["outcome"], "completed")
        self.assertFalse(control["summary_pending"])
        self.assertEqual(len(self.launches()), 3)
        self.assertIn("limit during the final report", self.journal())

    def test_prompt_states_the_cycle_budget_and_history_is_recorded(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = self.launches()[0]["prompt"]
        self.assertIn("Cycle budget", prompt)
        self.assertIn("3600s", prompt)
        self.assertIn("never a reason to block a task", prompt)
        # The TOON hint covers every form the encoder emits, and the text-output commands.
        for form in ("f{a,b}", "stats.done", "name[N:]{f1,f2}:", "`null`", "…[+N]", "before or after the command"):
            self.assertIn(form, prompt)
        self.assertIn("--graph-ref INSIGHT_ID", prompt)
        self.assertIn("`impact PATH`", prompt)
        control = self.control()
        self.assertEqual(control["cycle_timeout_seconds"], 3600)
        history = control["cycle_history"]
        self.assertEqual(len(history), 2, "a premature completion and the cycle that followed it")
        self.assertEqual(set(history[0]), {"started_at", "seconds", "rc", "advanced", "outcome", "limited"})
        self.assertTrue(history[0]["advanced"])
        self.assertEqual(history[1]["outcome"], "stopped by user")

    def test_discovery_only_cycles_back_off_and_a_commit_resets_the_backoff(self):
        result = self.invoke(script=["empty", "empty", "commit", "empty", "stop"],
                             env={"IMPROVE_IDLE_AFTER": "2", "IMPROVE_IDLE_GAP_CAP": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        control = self.control()
        self.assertEqual(control["idle_cycles"], 1)
        self.assertEqual(control["idle_gap"], 1)
        self.assertIn("discovery-only cycles", (self.state / "daemon.log").read_text())

    def test_daemon_log_rotates_when_large(self):
        result = self.invoke(script=["big_output", "stop"], env={"IMPROVE_LOG_MAX_BYTES": "65536"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.state / "daemon.log.1").exists())
        self.assertLess((self.state / "daemon.log").stat().st_size, 65536)

    def test_worker_can_use_the_helpers_it_is_told_to_use(self):
        result = self.invoke(script=["uses_helpers", "stop"])
        self.assertEqual(result.returncode, 0, result.stderr)
        results = json.loads((self.state / "helpers.json").read_text())
        for name, info in results.items():
            self.assertEqual(info["rc"], 0, f"{name}: {info['err']}")
        self.assertIn("run_id", results["pending"]["out"])
        self.assertIn("warm cache", self.launches()[1]["prompt"])

    @requires_max
    def test_false_success_claim_from_a_failed_exit_is_rejected_not_fatal(self):
        result = self.invoke("early", "--skill", "improve-max", "--args=--stop-at-target", script=["failed_target", "stop"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.control()["outcome"], "stopped by user")
        self.assertIn("Rejected a claim of 'target reached'", self.journal())


# Inherit the resilient fixture only; the parent's tests already ran once.
for _name in [name for name in vars(ResilienceTests) if name.startswith("test_")]:
    if _name not in vars(LongRunResilienceTests):
        setattr(LongRunResilienceTests, _name, None)


class ClockSkewTests(unittest.TestCase):
    def test_small_backward_clock_steps_are_tolerated(self):
        now = int(time.time())
        info = runtime.timing({"started_at": runtime.stamp(now + 60), "deadline": runtime.stamp(now + 3600)}, now=now)
        self.assertEqual(info["elapsed_seconds"], 0)
        self.assertEqual(info["status"], "RUNNING")
        with self.assertRaises(runtime.StateError):
            runtime.timing({"started_at": runtime.stamp(now + 400), "deadline": runtime.stamp(now + 3600)}, now=now)

    def test_evidence_rows_skip_damage_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "backlog.jsonl"
            path.write_bytes(b'{"id":"a","claim":"x"}\n\xff\xfe{"id":"b"}\nnot json\n[1]\n')
            rows = runtime.evidence_rows(path)
            self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
