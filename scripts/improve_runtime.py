#!/usr/bin/env python3
"""Portable clock and supervisor for /improve. Python 3.9+, standard library only."""

import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

# Helpers import this module by name even when its CLI is executed as a script.
# Share one StateError class so control failures follow the normal recovery path.
if __name__ == "__main__":
    sys.modules.setdefault("improve_runtime", sys.modules[__name__])

VERSION = "1.13.0"
HOST_MARKERS = {"codex": ("CODEX_THREAD_ID", "CODEX_CI"),
                "claude": ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
INTAKE_KEYS = ("outcomes", "features", "ui", "assets", "performance", "bugs",
               "security", "quality", "boundaries", "success")
LIMIT_PATTERN = re.compile(r"hit your (usage )?limit|usage limit reached|rate_limit_error|"
                           r"429 too many requests|out of extra usage", re.I)


class StateError(ValueError):
    pass


def resolve_engine(requested="auto", control=None, environ=None):
    """Pin the provider for a run; never guess from CLI installation order."""
    if requested not in ("auto", "codex", "claude"):
        raise StateError("--engine / IMPROVE_ENGINE must be auto, codex, or claude")
    if control is not None:
        # Supervisors written before 1.6 always launched Claude.
        saved = control.get("engine", "claude")
        if saved not in ("codex", "claude"):
            raise StateError("supervisor engine must be codex or claude")
        if requested not in ("auto", saved):
            raise StateError(f"this run uses {saved}; use --engine auto to keep it, or --new-run to change engines")
        return saved
    if requested != "auto":
        return requested
    env = os.environ if environ is None else environ
    hosts = [engine for engine, markers in HOST_MARKERS.items() if any(env.get(key) for key in markers)]
    if len(hosts) == 1:
        return hosts[0]
    reason = "conflicting host markers" if hosts else "cannot detect the current assistant"
    raise StateError(f"{reason}; pass --engine codex or --engine claude")


def resolve_sandbox(engine, requested=None, control=None):
    saved = control.get("codex_sandbox") if control is not None else requested
    for value in (requested, saved):
        if value is not None and value not in SANDBOX_MODES:
            raise StateError("invalid Codex sandbox mode")
    if engine != "codex" and (requested is not None or saved is not None):
        raise StateError("--codex-sandbox is only valid with the codex engine")
    if control is not None and requested is not None and requested != saved:
        raise StateError("resume keeps the saved Codex sandbox; use --new-run to change it")
    return saved


def resolve_model(requested=None, control=None):
    if control is not None and "model" in control:
        selected = control["model"]
        if requested is not None and requested != selected:
            raise StateError("resume keeps the saved model; omit --model or use --new-run to change it")
    else:
        selected = requested if requested is not None else os.environ.get("IMPROVE_MODEL")
    if selected is not None and (not isinstance(selected, str) or not selected.strip()):
        raise StateError("model must be a nonempty string or null for the CLI default")
    return selected


def agent_command(engine, prompt, model=None, codex_sandbox=None):
    if engine == "codex":
        # Keep the user's sandbox/rules; unattended work must not request approvals.
        command = ["codex", "--no-daemon", "--ask-for-approval", "never", "exec", "--ephemeral", "--color", "never"]
        if codex_sandbox is not None:
            command += ["--sandbox", codex_sandbox]
    else:
        command = ["claude", "--permission-mode", "bypassPermissions", "--output-format", "text"]
    if model:
        command += ["--model", model]
    return command + ([prompt] if engine == "codex" else ["-p", prompt])


def read_json(path):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise StateError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StateError(f"{path} must contain a JSON object")
    return value


def atomic_json(path, value):
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def stamp(value=None):
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value):
        raise StateError(f"expected an ISO-8601 UTC timestamp ending in Z, got {value!r}")
    try:
        return int(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
    except ValueError as exc:
        raise StateError(f"invalid timestamp: {value!r}") from exc


def timing(run, now=None):
    now = int(time.time() if now is None else now)
    start, deadline = epoch(run.get("started_at")), epoch(run.get("deadline"))
    if deadline <= start:
        raise StateError("deadline must be later than started_at")
    if start > now:
        raise StateError("started_at is in the future; check the system clock")
    last = run.get("last_report_hour", 0)
    if type(last) is not int or last < 0:
        raise StateError("last_report_hour must be a nonnegative integer")
    elapsed, remaining, total = now - start, deadline - now, deadline - start
    return dict(now=stamp(now), started_at=run["started_at"], deadline=run["deadline"],
                elapsed_seconds=elapsed, remaining_seconds=remaining, total_seconds=total,
                hour=min(elapsed // 3600 + 1, math.ceil(total / 3600)),
                hours_total=math.ceil(total / 3600),
                report_hour=elapsed // 3600 if elapsed // 3600 > last else None,
                status="OVER" if remaining <= 0 else "RUNNING")


def human(seconds):
    sign, seconds = ("-" if seconds < 0 else ""), abs(int(seconds))
    if seconds >= 3600:
        return f"{sign}{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    return f"{sign}{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{sign}{seconds}s"


def recorded_timing(run, control):
    recorded = dict(run or {})
    if control is not None:
        recorded.update({key: control.get(key) for key in ("started_at", "deadline")})
    return timing(recorded)


def clock(args):
    state = Path(args.repo).expanduser() / ".improve"
    run = read_json(state / "run.json") if (state / "run.json").exists() else None
    control = read_json(state / "supervisor.json") if (state / "supervisor.json").exists() else None
    if run is None and control is None:
        raise StateError(f"no run.json or supervisor.json in {state}")
    info = recorded_timing(run, control)
    if args.remaining:
        print(info["remaining_seconds"])
    elif args.json:
        print(json.dumps(info))
    else:
        print(f"now        {info['now']}")
        print(f"started    {info['started_at']}  elapsed   {human(info['elapsed_seconds'])}")
        print(f"deadline   {info['deadline']}  remaining {human(info['remaining_seconds'])}")
        print(f"hour       {info['hour']} of {info['hours_total']} (current interval)")
        report = "final" if info["status"] == "OVER" else (
            f"hour {info['report_hour']} is due" if info["report_hour"] else "not due")
        print(f"report     {report}")
        print(f"status     {info['status']} - " + ("write the final report and stop" if info["status"] == "OVER"
              else "keep working; an empty queue means discover, not finish"))
    return 10 if info["status"] == "OVER" else 0


def duration(value):
    if not re.fullmatch(r"\d+[smhd]?", value):
        raise StateError("duration must be a positive integer with s, m, h or d (e.g. 90m)")
    unit = value[-1] if value[-1].isalpha() else "s"
    number = int(value[:-1] if value[-1].isalpha() else value)
    seconds = number * dict(s=1, m=60, h=3600, d=86400)[unit]
    if not 0 < seconds <= 366 * 86400:
        raise StateError("duration must be greater than zero and at most 366 days")
    return seconds


def env_integer(name, default, minimum=1):
    value = os.environ.get(name, str(default))
    if not re.fullmatch(r"\d+", value) or int(value) < minimum:
        raise StateError(f"{name} must be an integer >= {minimum}")
    return int(value)


def validate_intake(value):
    if "status" in value and value["status"] != "complete":
        raise StateError("intake status must be complete; collect the user's remaining answers before starting")
    answers = value.get("answers")
    if not isinstance(answers, dict):
        raise StateError("intake must contain an answers object; see references/intake.md")
    missing = [key for key in INTAKE_KEYS if not isinstance(answers.get(key), str) or not answers[key].strip()]
    if missing:
        raise StateError("intake is missing answers: " + ", ".join(missing))
    return value


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True)
    if result.returncode:
        raise StateError(result.stderr.strip() or "git command failed")
    return result.stdout.strip()


def dirty(repo):
    return git(repo, "status", "--porcelain", "--", ".", ":(exclude).improve")


def signal_group(pid, sig):
    # The leader can exit between poll() and killpg(). Its descendants may still live.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, sig)


def evidence_rows(path):
    """Ignore bookkeeping-only rewrites when looking for new recorded evidence."""
    if not path.exists():
        return set()
    ignored = {"at", "timestamp", "created_at", "updated_at", "started_at", "completed_at",
               "ended_at", "placed_at", "generation", "next_scope", "id", "title", "status"}
    fingerprints = set()
    with path.open() as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise StateError(f"invalid JSON in {path.name}:{number}") from exc
            if not isinstance(row, dict):
                raise StateError(f"{path.name}:{number} must contain an object")
            normalized = {k: v for k, v in row.items() if k not in ignored}
            if isinstance(normalized.get("pieces"), list):
                normalized["pieces"] = [
                    {k: v for k, v in piece.items() if k not in ignored} if isinstance(piece, dict) else piece
                    for piece in normalized["pieces"]]
            if normalized:
                fingerprints.add(json.dumps(normalized, sort_keys=True))
    return fingerprints


def snapshot(repo):
    state = repo / ".improve"
    run = read_json(state / "run.json") if (state / "run.json").exists() else {}
    cycle = run.get("cycle", 0)
    if type(cycle) is not int or cycle < 0:
        raise StateError("cycle must be a nonnegative integer")
    setup = {key: run[key] for key in ("preflight_complete", "gates", "baseline_commit",
             "focus", "journeys", "detectors", "scanners") if key in run}
    return dict(cycle=cycle, head=git(repo, "rev-parse", "HEAD"), setup=setup,
                discovery=evidence_rows(state / "discovery.jsonl"),
                backlog=evidence_rows(state / "backlog.jsonl"),
                bets=evidence_rows(state / "bets.jsonl"))


def advanced(before, after):
    return after["cycle"] > before["cycle"] and bool(
        after["head"] != before["head"] or after["setup"] != before["setup"] or
        after["discovery"] - before["discovery"] or after["backlog"] - before["backlog"] or
        after["bets"] - before["bets"])


@contextlib.contextmanager
def repo_lock(path):
    # Keep the inode: unlinking a flock file lets two daemons lock different files.
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StateError("a daemon is already running for this repo") from exc
        stream.seek(0)
        stream.truncate()
        stream.write(f"{os.getpid()}\n{stamp()}\n")
        stream.flush()
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def lock_active(path):
    if not path.exists():
        return False
    with path.open("r+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
    return False


def terminal_outcome(run, skill, extra_args, now=None):
    outcome = run.get("outcome")
    if outcome is None:
        return None
    if not isinstance(outcome, str):
        raise StateError("outcome must be null or a string")
    if outcome == "completed":
        return "completed" if timing(run, now)["remaining_seconds"] <= 0 else None
    if outcome == "stopped by user" or outcome.startswith("halted:"):
        return outcome
    if outcome == "target reached" and skill == "improve-max" and "--stop-at-target" in shlex.split(extra_args):
        return outcome
    raise StateError(f"unrecognized or unauthorized terminal outcome: {outcome!r}")


class Supervisor:
    def __init__(self, args, repo):
        self.args, self.repo = args, repo
        self.state = repo / ".improve"
        self.run_path = self.state / "run.json"
        self.control_path = self.state / "supervisor.json"
        self.stop_path = self.state / "stop.request"
        self.stopping = False
        self.last_report_ok = False
        self.timeout = env_integer("IMPROVE_CYCLE_TIMEOUT", 14400 if args.skill == "improve-max" else 3600)
        self.gap = env_integer("IMPROVE_CYCLE_GAP", 2, 0)
        self.too_fast = env_integer("IMPROVE_TOO_FAST", 25, 0)
        self.max_failures = env_integer("IMPROVE_MAX_FAILURES", 3)
        self.limit_wait = env_integer("IMPROVE_LIMIT_WAIT", 900)
        self.limit_cap = env_integer("IMPROVE_LIMIT_WAIT_CAP", 3600)
        self.limit_budget = env_integer("IMPROVE_LIMIT_WAIT_BUDGET", 14400)
        self.final_timeout = env_integer("IMPROVE_FINAL_TIMEOUT", 300)
        self.finish_grace = env_integer("IMPROVE_FINISH_GRACE", 120, 0)

    def log(self, message):
        line = f"{stamp()} {message}"
        print(line, flush=True)
        with (self.state / "daemon.log").open("a") as stream:
            stream.write(line + "\n")

    def journal(self, message):
        with (self.state / "journal.md").open("a") as stream:
            stream.write(f"\n## {stamp()} - daemon\n\n{message}\n")

    def request_stop(self, *_):
        self.stopping = True
        with contextlib.suppress(OSError):
            self.stop_path.touch()

    def stop_requested(self):
        return self.stopping or self.stop_path.exists()

    def pause_requested(self):
        from improve_control import pause_requested
        return pause_requested(self.repo, self.control.get("run_id", self.control["started_at"]))

    def wait_while_paused(self):
        """Pause between clean cycles; the original wall-clock deadline still applies."""
        if not self.pause_requested():
            return False
        self.save_control(phase="paused", child_pid=None)
        self.log("paused by user; original deadline unchanged")
        heartbeat = time.monotonic()
        while (self.pause_requested() and not self.stop_requested()
               and time.time() < epoch(self.control["deadline"])):
            if time.monotonic() - heartbeat >= 5:
                self.save_control(phase="paused")
                heartbeat = time.monotonic()
            time.sleep(0.1)
        self.save_control(phase="between-cycles")
        return True

    def prepare(self):
        run = read_json(self.run_path) if self.run_path.exists() else None
        control = read_json(self.control_path) if self.control_path.exists() else None
        recorded = control or run
        report_only = self.args.finalize or (recorded and timing(recorded)["remaining_seconds"] <= 0)
        intake_path = self.state / "intake.json"
        saved_intake = read_json(intake_path) if intake_path.exists() else None
        supplied = None
        if self.args.intake:
            supplied = validate_intake(read_json(Path(self.args.intake).expanduser()))
            if saved_intake is not None and supplied["answers"] != saved_intake.get("answers"):
                raise StateError("resume keeps saved intake; change focus in the active session or start a new run")
        if saved_intake is None and supplied is None and not report_only:
            raise StateError("complete the ten intake questions first; pass --intake FILE (references/intake.md)")
        if saved_intake is not None and not report_only:
            validate_intake(saved_intake)
        if control:
            old_outcome = control.get("outcome")
            if self.args.finalize:
                if not old_outcome or not control.get("summary_pending"):
                    raise StateError("--finalize requires an ended run with summary_pending: true")
            elif self.args.resume:
                if not isinstance(old_outcome, str) or not (old_outcome.startswith("halted:") or old_outcome == "stopped by user"):
                    raise StateError("--resume requires a halted or user-stopped run; completed runs use --new-run")
            elif old_outcome:
                raise StateError("previous run has ended; use --resume after recovery, --finalize for a pending report, or --new-run")
            if control.get("skill") != self.args.skill or control.get("args") != self.args.args:
                raise StateError("resume with the original --skill and --args; use --new-run to change them")
            self.control = control
        elif run:
            if self.args.resume or self.args.finalize:
                raise StateError("recovery requires the existing supervisor.json")
            timing(run)
            if terminal_outcome(run, self.args.skill, self.args.args):
                raise StateError("previous run has ended; use --new-run after reviewing it")
            self.control = dict(started_at=run["started_at"], deadline=run["deadline"])
        else:
            if self.args.resume or self.args.finalize:
                raise StateError("no supervised run exists to recover")
            started = int(time.time())
            self.control = dict(started_at=stamp(started), deadline=stamp(started + duration(self.args.duration)))
        self.control.update(skill=self.args.skill, args=self.args.args, repo=str(self.repo),
                            engine=self.engine, codex_sandbox=self.codex_sandbox, model=self.model,
                            board_control_version=1)
        self.control.setdefault("run_id", uuid.uuid4().hex)
        timing(self.control)
        for key in ("consecutive_failures", "next_limit_wait", "limit_waited_seconds"):
            value = self.control.get(key, 0)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise StateError(f"supervisor {key} must be a nonnegative finite number")
        retry = self.control.get("retry_at")
        wait_start = self.control.get("limit_wait_started_at")
        if retry is not None:
            retry_epoch = epoch(retry)
            if retry_epoch > epoch(self.control["deadline"]):
                raise StateError("retry_at cannot extend beyond the original deadline")
            if wait_start is not None and epoch(wait_start) > retry_epoch:
                raise StateError("limit_wait_started_at cannot be later than retry_at")
        elif wait_start is not None:
            raise StateError("limit_wait_started_at requires retry_at")
        if self.args.resume:
            if run and run.get("active_item"):
                raise StateError("resolve and clear active_item before --resume; a clean tree alone does not prove verification")
            self.journal(f"Explicit resume after {old_outcome}; original deadline and intake preserved.")
            for data in (self.control, run):
                if data is not None:
                    for key in ("outcome", "ended_at", "summary_pending", "last_error"):
                        data.pop(key, None)
            self.control["consecutive_failures"] = 0
            if run is not None:
                atomic_json(self.run_path, run)
        # Saved answers may have later authorization/steering metadata. Repeating the
        # original file must not erase it; only a first intake or --new-run replaces it.
        if saved_intake is None and supplied is not None:
            atomic_json(intake_path, supplied)
        atomic_json(self.control_path, self.control)

    def save_control(self, **changes):
        self.control.update(changes, heartbeat_at=stamp())
        atomic_json(self.control_path, self.control)

    def reconcile(self):
        if not self.run_path.exists():
            return None
        run = read_json(self.run_path)
        changed = any(run.get(k) != self.control[k] for k in ("started_at", "deadline"))
        if changed:
            for key in ("started_at", "deadline"):
                run[key] = self.control[key]
            self.journal("Restored the original start/deadline after a cycle changed it.")
        result = terminal_outcome(run, self.args.skill, self.args.args)
        if run.get("outcome") == "completed" and result is None:
            run["outcome"] = None
            run["next_action"] = "Discover fresh work in the highest-priority unfinished focus area."
            self.journal("Rejected premature completion: time remains. Resuming discovery with the original deadline.")
            changed = True
        if changed:
            atomic_json(self.run_path, run)
        return result

    def wait(self, seconds):
        until = min(time.time() + seconds, epoch(self.control["deadline"]))
        started = time.monotonic()
        while time.time() < until and not self.stop_requested() and not self.pause_requested():
            time.sleep(min(0.25, max(0, until - time.time())))
        return time.monotonic() - started

    def resume_limit_wait(self):
        """Honor a durable retry time and account each wall-clock second once."""
        retry = epoch(self.control["retry_at"])
        # Legacy state can preserve its retry time, but has no elapsed-wait checkpoint.
        if self.control.get("limit_wait_started_at") is None:
            self.save_control(limit_wait_started_at=stamp(min(int(time.time()), retry)))
        self.save_control(phase="account-limit")
        self.log(f"account-limit backoff: retry at {stamp(retry)}")
        self.wait(max(0, retry - time.time()))
        self.account_limit_wait()
        self.save_control(phase="account-limit" if self.control.get("retry_at") else "between-cycles")

    def account_limit_wait(self):
        if not self.control.get("retry_at"):
            return
        retry = epoch(self.control["retry_at"])
        accounted_until = min(int(time.time()), retry)
        start = epoch(self.control["limit_wait_started_at"]) if self.control.get("limit_wait_started_at") else accounted_until
        waited = self.control.get("limit_waited_seconds", 0) + max(0, accounted_until - start)
        pending = accounted_until < retry
        self.control.update(limit_waited_seconds=waited,
                            limit_wait_started_at=stamp(accounted_until) if pending else None,
                            retry_at=stamp(retry) if pending else None)

    def prompt(self, final=False):
        remaining = max(0, epoch(self.control["deadline"]) - int(time.time()))
        skill_path = Path(__file__).resolve().parent.parent / ("max/SKILL.md" if self.args.skill == "improve-max" else "SKILL.md")
        prefix = "$" if self.engine == "codex" else "/"
        return f"""{prefix}{self.args.skill} {remaining}s {shlex.quote(str(self.repo))} {self.args.args}

You are ONE CYCLE of an externally supervised improvement run. Read {skill_path}.
Use the saved ten-question intake in .improve/intake.json. Do not ask it again.
Finalization of a legacy expired run may lack intake; report the recorded evidence only.
Use those answers to drive focus, discovery, and ranking. Read .improve/run.json,
backlog.jsonl, discovery.jsonl and supervisor.json before work; missing files on the
first cycle are initialized during preflight. The supervisor's immutable start is
{self.control['started_at']} and deadline is {self.control['deadline']}.
Copy those exact timestamps into run.json; never calculate a replacement deadline.
Do not schedule wakeups, launch another daemon, or sleep to fill the duration.
The supervisor owns the local task board ({'disabled by --no-board' if self.args.no_board else 'auto-start enabled'}).
Do not start or stop a board from this child cycle. Keep backlog.jsonl current: record
each task before work, set in_progress before editing, then its verified final status.
Keep completed/rejected tasks, verification evidence, and actual commit SHAs for the board.
Read pending board requests before selecting each item with:
python3 {shlex.quote(str(Path(__file__).with_name('improve_control.py')))} --repo {shlex.quote(str(self.repo))} pending
Follow {Path(__file__).resolve().parent.parent / 'references/board-control.md'} to apply and acknowledge them.
These requests are user steering. Preserve intake exclusions unless the user explicitly changes them.
When the helper reports pause_requested, settle the current item, checkpoint a clean tree,
and return to the supervisor without new work. Pause is scoped to supervisor.json.run_id.
Do not acknowledge a request until its intent is reflected in the ledger/plan or a decline is explained.
For improve-max, update bets.jsonl phases, measurements, and pieces; do not duplicate bets in backlog.jsonl.
An empty queue requires a fresh scoped discovery pass, not a completion summary.
Resolve all finders before returning: another process cannot inherit live agents.
Persist cycle + 1, next_action, and evidence of work/discovery before returning.
Incrementing cycle or rewriting timestamps is not new evidence. Record a new scoped
scan, changed finding, verification result, or commit. Do not repeat an identical scan.
Check .improve/stop.request and the clock between items and tool calls. On stop or expiry,
settle the current item only. The supervisor allows {self.finish_grace}s of cleanup grace.
Only record a halt for a concrete blocker; a dry scan or rejected candidate is not one.
Previous cycle correction, if any: {self.control.get('last_error', 'none')}.
{"FINALIZATION ONLY: start no new work. This run ended or its deadline passed. Ignore pause for this report-only pass and report pending board requests as unimplemented. Preserve an existing halt/stop outcome and original end time. Write .improve/final-report.md from actual git and ledger evidence, append it to journal.md, set summary_pending false, and set completed only for deadline expiry. Never claim unverified changes passed." if final else "Complete a useful cycle, leave a clean tree, and return; the supervisor will relaunch you."}
"""

    def launch(self, final=False):
        command = agent_command(self.engine, self.prompt(final), self.model, self.codex_sandbox)
        report = self.state / "final-report.md"
        previous_write = report.stat().st_mtime_ns if report.exists() else None
        self.last_report_ok = False
        env = os.environ.copy()
        # Each cycle is a fresh process, not a nested interactive Claude session.
        # Keep auth/config variables; discard only inherited session identity.
        for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_THREAD_ID"):
            env.pop(key, None)
        started = time.monotonic()
        cutoff = started + (self.final_timeout if final else self.timeout)
        if not final:
            cutoff = min(cutoff, started + max(0, epoch(self.control["deadline"]) - time.time()) + self.finish_grace)
        log_path = self.state / "daemon.log"
        offset = log_path.stat().st_size if log_path.exists() else 0
        with log_path.open("ab", buffering=0) as output:
            child = subprocess.Popen(command, cwd=self.repo, env=env, stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            timed_out = False
            try:
                self.save_control(phase="finalize" if final else "working", child_pid=child.pid,
                                  cycle_started_at=stamp())
                while child.poll() is None:
                    if self.stop_requested():
                        cutoff = min(cutoff, time.monotonic() + self.finish_grace)
                    if time.monotonic() >= cutoff:
                        timed_out = True
                        signal_group(child.pid, signal.SIGTERM)
                        try:
                            child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
                        signal_group(child.pid, signal.SIGKILL)
                        break
                    time.sleep(0.1)
                rc = child.wait()
            finally:
                if child.poll() is None:
                    signal_group(child.pid, signal.SIGKILL)
                    child.wait()
                # A completed cycle cannot hand background writers to its successor.
                signal_group(child.pid, signal.SIGKILL)
                self.save_control(child_pid=None, phase="between-cycles")
        with log_path.open("rb") as output:
            output.seek(max(offset, log_path.stat().st_size - 65536))
            tail = output.read().decode(errors="replace")
        rc = 124 if timed_out else rc
        self.last_report_ok = (rc == 0 and report.exists() and report.stat().st_mtime_ns != previous_write
                               and bool(report.read_text().strip()))
        return rc, time.monotonic() - started, tail

    def finish(self, outcome, summary_pending=None):
        # An offline restart may reach finalization without re-entering the wait loop.
        self.account_limit_wait()
        if summary_pending is None:
            summary_pending = not self.last_report_ok
        self.control.update(outcome=outcome, ended_at=self.control.get("ended_at") or stamp(),
                            summary_pending=summary_pending, phase="ended", child_pid=None)
        atomic_json(self.control_path, self.control)
        if self.run_path.exists():
            run = read_json(self.run_path)
            run.update(outcome=outcome, ended_at=self.control["ended_at"], summary_pending=summary_pending)
            atomic_json(self.run_path, run)
        self.journal(f"Run ended: {outcome}. Remaining: {human(epoch(self.control['deadline']) - int(time.time()))}."
                     + (" Final narrative report is pending; no agent success is implied." if summary_pending else ""))
        self.log(f"daemon finished: {outcome}")
        return 1 if outcome.startswith("halted:") else 0

    def finalize(self):
        """A bounded report-only launch; retryable without granting more work time."""
        original = self.control.get("outcome")
        original_head = git(self.repo, "rev-parse", "HEAD")
        self.log("requesting final report without new work")
        rc, _, _ = self.launch(final=True)
        outcome = self.reconcile()
        if dirty(self.repo) or git(self.repo, "rev-parse", "HEAD") != original_head:
            return self.finish("halted: finalization modified repository work", summary_pending=True)
        return self.finish(original or outcome or "completed", summary_pending=rc != 0 or not self.last_report_ok)

    def run(self):
        failures = self.control.get("consecutive_failures", 0)
        next_wait = self.control.get("next_limit_wait", min(self.limit_wait, self.limit_cap))
        if self.args.finalize:
            return self.finalize()
        self.log(f"daemon {VERSION}: deadline={self.control['deadline']}; intake saved; one cycle per launch")
        while True:
            outcome = self.reconcile()
            if self.stop_requested():
                return self.finish("stopped by user")
            if dirty(self.repo):
                return self.finish("halted: working tree has uncommitted changes; manual recovery required")
            if outcome and outcome != "completed":
                return self.finish(outcome)
            if epoch(self.control["deadline"]) <= time.time():
                return self.finalize()
            if self.wait_while_paused():
                continue
            if failures >= self.max_failures:
                return self.finish(f"halted: {failures} consecutive failed or uncheckpointed cycles")
            if self.control.get("retry_at"):
                self.resume_limit_wait()
                continue
            before = snapshot(self.repo)
            self.log(f"cycle starting ({human(epoch(self.control['deadline']) - int(time.time()))} left)")
            rc, elapsed, output = self.launch()
            outcome = self.reconcile()
            if self.stop_requested():
                return self.finish("stopped by user")
            if dirty(self.repo):
                return self.finish("halted: cycle left uncommitted changes; manual recovery required")
            if outcome in ("completed", "target reached") and rc:
                return self.finish(f"halted: cycle claimed {outcome} but exited {rc}", summary_pending=True)
            if outcome == "completed":
                return self.finalize()
            if outcome:
                return self.finish(outcome)
            # A requested checkpoint may return without new evidence; pausing itself
            # neither increments nor clears the existing failure counter.
            if self.pause_requested() and rc == 0:
                continue
            if rc and LIMIT_PATTERN.search(output):
                waited = self.control.get("limit_waited_seconds", 0)
                if waited >= self.limit_budget:
                    return self.finish("halted: account-limit wait budget exhausted")
                now = int(time.time())
                delay = min(max(1, next_wait), self.limit_budget - waited,
                            max(0, epoch(self.control["deadline"]) - now))
                self.journal(f"Account usage limit; waiting up to {human(delay)}. Original deadline unchanged.")
                next_wait = min(max(1, next_wait) * 2, self.limit_cap)
                self.save_control(phase="account-limit", retry_at=stamp(now + math.ceil(delay)),
                                  limit_wait_started_at=stamp(now), next_limit_wait=next_wait)
                continue
            next_wait = min(self.limit_wait, self.limit_cap)
            after = snapshot(self.repo)
            failed = rc != 0 or not advanced(before, after)
            failures = failures + 1 if failed else 0
            correction = ("Last cycle failed or supplied no new evidence. Inspect a different in-scope "
                          "path/hypothesis and record what you checked; do not repeat the last scan.") if failed else None
            self.save_control(consecutive_failures=failures, next_limit_wait=next_wait, last_error=correction)
            self.log(f"cycle returned {rc} in {human(elapsed)}; checkpoint {before['cycle']}->{after['cycle']}; failures={failures}"
                     + ("; fast but checkpointed" if not failed and elapsed < self.too_fast else ""))
            self.wait(self.gap)


def daemon(args):
    from improve_board import SERVICE_FILES, board_status, start_board

    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        raise StateError(f"no such directory: {repo}")
    if Path(git(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
        raise StateError("--repo must name the git repository root")
    state, lock = repo / ".improve", repo / ".improve/daemon.lock"
    if args.status:
        info = {"running": lock_active(lock), "repo": str(repo)}
        info["board"] = board_status(repo)
        for name in ("supervisor.json", "run.json"):
            if (state / name).exists():
                info[name.removesuffix(".json")] = read_json(state / name)
        recorded = info.get("supervisor") or info.get("run")
        if recorded:
            info["clock"] = recorded_timing(info.get("run"), info.get("supervisor"))
        if args.json:
            print(json.dumps(info))
        else:
            print("running" if info["running"] else "not running")
            if info["board"]["running"]:
                print(f"board={info['board']['url']}")
            if recorded:
                print(f"deadline={recorded['deadline']} remaining={human(info['clock']['remaining_seconds'])} outcome={recorded.get('outcome')}")
                print(f"phase={recorded.get('phase', 'unknown')} child_pid={recorded.get('child_pid')} retry_at={recorded.get('retry_at')}")
                if "supervisor" in info:
                    print(f"engine={info['supervisor'].get('engine', 'claude')} model={info['supervisor'].get('model') or 'CLI default'}")
                run = info.get("run", {})
                print(f"cycle={run.get('cycle', 0)} next_action={run.get('next_action', 'not yet recorded')}")
        return 0
    if args.json:
        raise StateError("--json is available with --status")
    if args.stop:
        if lock_active(lock):
            (state / "stop.request").touch()
            print("stop requested; the cycle in flight will finish first")
        else:
            print("not running")
        return 0
    if not args.duration and not (args.resume or args.finalize):
        raise StateError("--for is required (e.g. --for 6h)")
    seconds = duration(args.duration) if args.duration else 0
    supervisor = Supervisor(args, repo)
    if dirty(repo):
        raise StateError("working tree is dirty; commit or stash your changes before starting")
    if args.dry_run:
        control_path = state / "supervisor.json"
        control = read_json(control_path) if control_path.exists() and not args.new_run else None
        engine = resolve_engine(args.engine, control)
        sandbox = resolve_sandbox(engine, args.codex_sandbox, control)
        model = resolve_model(args.model, control)
        existing = state / "supervisor.json" if (state / "supervisor.json").exists() else state / "run.json"
        deadline = stamp(int(time.time()) + seconds)
        if existing.exists() and not args.new_run:
            saved = read_json(existing)
            timing(saved)
            deadline = saved["deadline"]
        print(f"Would run {args.skill} with {engine} in {repo} until {deadline}.")
        print("Requires ten-question intake; command: " + shlex.join(agent_command(engine, "<cycle prompt>", model, sandbox)))
        print(f"Cycle timeout: {supervisor.timeout}s; final-report timeout: {supervisor.final_timeout}s.")
        return 0
    state.mkdir(exist_ok=True)
    with repo_lock(lock):
        control_path = state / "supervisor.json"
        control = read_json(control_path) if control_path.exists() and not args.new_run else None
        supervisor.engine = resolve_engine(args.engine, control)
        supervisor.codex_sandbox = resolve_sandbox(supervisor.engine, args.codex_sandbox, control)
        supervisor.model = resolve_model(args.model, control)
        if not shutil.which(supervisor.engine):
            raise StateError(f"the selected {supervisor.engine} CLI is not on PATH; no other engine will be used")
        if args.intake:
            validate_intake(read_json(Path(args.intake).expanduser()))
        from improve_control import operator_lock
        with operator_lock(repo):
            if args.new_run:
                if not args.intake:
                    raise StateError("--new-run requires --intake FILE from a new ten-question intake")
                intake = read_json(Path(args.intake).expanduser())
                old = [p for p in state.iterdir() if p.name not in {"history", "daemon.lock", *SERVICE_FILES}]
                if old:
                    history = state / "history"
                    history.mkdir(exist_ok=True)
                    archive = Path(tempfile.mkdtemp(prefix=stamp().replace(":", "-") + "-", dir=history))
                    for path in old:
                        shutil.move(str(path), archive / path.name)
                atomic_json(state / "intake.json", intake)
                args.intake = None
            supervisor.stop_path.unlink(missing_ok=True)
            supervisor.prepare()
            if args.resume:
                from improve_control import set_pause
                set_pause(repo, supervisor.control["run_id"], False)
        supervisor.log(f"using {supervisor.engine}; engine and deadline are fixed for this run")
        if not args.no_board:
            try:
                board = start_board(repo, args.board_port)
                supervisor.log(f"Kanban board: {board['url']} (stays available after the run)")
            except (OSError, ValueError) as exc:
                supervisor.log(f"Kanban board unavailable: {exc}; improvement work will continue")
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, supervisor.request_stop)
        try:
            return supervisor.run()
        except (StateError, OSError) as exc:
            # Preserve a malformed run.json for diagnosis instead of replacing it.
            supervisor.control.update(outcome=f"halted: {exc}", ended_at=stamp(), summary_pending=True)
            atomic_json(supervisor.control_path, supervisor.control)
            supervisor.journal(f"HALTED: {exc}. State preserved for manual recovery.")
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    timer = sub.add_parser("clock")
    mode = timer.add_mutually_exclusive_group()
    mode.add_argument("--remaining", action="store_true")
    mode.add_argument("--json", action="store_true")
    timer.add_argument("repo", nargs="?", default=".")
    runner = sub.add_parser("daemon")
    runner.add_argument("--repo", required=True)
    runner.add_argument("--for", "--duration", dest="duration")
    runner.add_argument("--skill", choices=("improve", "improve-max"), default="improve")
    runner.add_argument("--args", default="")
    runner.add_argument("--engine", choices=("auto", "codex", "claude"),
                        default=os.environ.get("IMPROVE_ENGINE", "auto"),
                        help="assistant for new runs (default: detect host); saved engine wins on restart")
    runner.add_argument("--codex-sandbox", choices=SANDBOX_MODES,
                        help="carry over the current Codex session's mode; default: CLI configuration")
    runner.add_argument("--model", help="model for a new run; saved selection is kept on recovery")
    runner.add_argument("--intake", help="JSON file with ten intake answers")
    runner.add_argument("--no-board", action="store_true", help="do not auto-start the local Kanban board")
    from improve_board import port_number
    runner.add_argument("--board-port", type=port_number, help="board port; 0 chooses a free port")
    recovery = runner.add_mutually_exclusive_group()
    recovery.add_argument("--new-run", action="store_true", help="archive old state after a new intake")
    recovery.add_argument("--resume", action="store_true", help="resume a recovered halt/stop with its original deadline")
    recovery.add_argument("--finalize", action="store_true", help="retry a pending final report without new improvement work")
    action = runner.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true")
    action.add_argument("--status", action="store_true")
    action.add_argument("--stop", action="store_true")
    runner.add_argument("--json", action="store_true", help="machine-readable --status")
    runner.add_argument("--version", action="version", version=f"improve-daemon {VERSION}")
    args = parser.parse_args(argv)
    try:
        return clock(args) if args.command == "clock" else daemon(args)
    except (StateError, OSError) as exc:
        print(f"improve: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
