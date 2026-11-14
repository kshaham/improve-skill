#!/usr/bin/env python3
"""Local task board and cooperative controls for improve. Python 3.9+, standard library only."""

import argparse
from collections import OrderedDict
import contextlib
import fcntl
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import socketserver
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import traceback
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from improve_runtime import StateError, atomic_json, epoch, git, lock_active, read_json, recorded_timing, repo_lock, stamp
import improve_control
from improve_control import AREAS, THEMES, operator_lock, pause_requested, queue_request, queue_submission, read_local, request_view, request_view_at, require_run, set_pause

ASSETS = Path(__file__).resolve().parent.parent / "assets/board"
SERVICE_FILES = {"board.json", "board.lock", "board.log", "operator.lock"}
SNAPSHOT_EXTRAS = ("learning", "graph")
DEFAULT_PORT = 8765
# The parse cache holds at least this many files, and grows with the number of run
# directories so that an unchanged "All runs" poll re-parses nothing (LRU beyond that).
FILE_CACHE_LIMIT = 64
LEDGER_FILES_PER_RUN = 6
FILE_CACHE_CEILING = 4096
JOURNAL_LIMIT = 1024 * 1024
LOG_LIMIT = 256 * 1024
LOG_LINES_DEFAULT = 200
LOG_LINES_MAX = 2000
CYCLE_HISTORY_LIMIT = 48
QUARANTINE_LIMIT = 100
# The supervisor names quarantine refs `refs/improve/quarantine/<UTC stamp>[-<label>]`
# (improve_resilience.quarantine_changes). Anything else under that namespace was not written
# by the supervisor and is not offered as a command to paste.
QUARANTINE_REF = re.compile(r"refs/improve/quarantine/\d{4}-\d{2}-\d{2}T[0-9-]{4,8}Z(?:-[A-Za-z0-9][A-Za-z0-9._-]{0,79})?")
# /api/report reads under the operator lock, but a browser request must never queue behind a
# stuck holder indefinitely or starve the worker's own `pending`/`ack` calls.
REPORT_LOCK_SECONDS = 2.0
# Sec-Fetch-Site values a same-origin board page (or a non-browser client) sends.
SAME_SITE_FETCH = (None, "same-origin", "none")
GRAPH_INT_PATTERN = re.compile(r"-?\d{1,9}")
HISTORY_OFFSET_MAX = 1_000_000
GZIP_MIN_BYTES = 1024
# Request types whose validation lives in improve_control; the board only offers them when
# the runtime it is paired with knows them.
OPTIONAL_REQUEST_TYPES = ("retry", "withdraw")
# Fields of the server clock that change on every poll; they are left out of the ETag so
# an unchanged ledger yields 304 and the browser advances the countdown itself.
VOLATILE_CLOCK_KEYS = ("now", "elapsed_seconds", "remaining_seconds", "hour", "report_hour", "status")
# Liveness: the supervisor rewrites heartbeat_at every 5 s even while paused. Including it in
# the ETag resent the whole snapshot (megabytes on a large ledger) on every poll of a paused
# run, so the browser reads these from /api/health on its own cadence instead.
VOLATILE_CURRENT_KEYS = ("heartbeat_at", "heartbeat_age_seconds", "cycle_age_seconds")
# Board-view fields the Knowledge tab never renders; they are dropped before sending.
UNUSED_GRAPH_VIEW_KEYS = ("counts", "runs", "dropped")
ERROR_CODES = {"unknown run": "unknown_run", "not found": "not_found",
               "that node is no longer in the graph": "node_missing"}
_file_cache = OrderedDict()
_file_cache_lock = threading.Lock()
_file_cache_capacity = FILE_CACHE_LIMIT


class BoardError(StateError):
    """A StateError with a machine-readable code for the browser."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def error_payload(exc):
    message = str(exc)
    code = getattr(exc, "code", None) or ERROR_CODES.get(message)
    if code is None and message.startswith("no graph node "):
        code = "node_missing"
    payload = {"error": message}
    if code:
        payload["code"] = code
    return payload
COLUMNS = [("ready", "Queued"), ("in_progress", "In progress"), ("done", "Done"),
           ("blocked", "Blocked"), ("proposed", "Proposed"), ("rejected", "Rejected")]
TASK_FIELDS = ("id", "title", "claim", "area", "kind", "file", "line", "paths", "owned_paths",
               "note", "evidence", "failure", "acceptance", "verification", "counter", "measurement",
               "commit", "focus_priority", "priority", "created_at", "updated_at", "started_at",
               "completed_at", "reopens", "last_verification", "bet_status", "journey",
               "target", "claimed_gain", "spike", "landed", "pieces", "kill", "branch", "user_priority", "board_request_id", "user_notes",
               "blocked_reason", "blocked_by", "blocked_at", "block_count", "supervisor_note")
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_CONTEXT_FILES = 20


def state_directory(repo):
    state = repo / ".improve"
    if state.is_symlink():
        raise StateError("the board requires .improve to be a directory inside the repository")
    return state


def finite_number(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite numbers are not valid board data")
    return number


def parse_ledger_file(path, name, warnings, jsonl):
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("file exceeds the 32 MiB viewer limit")
        text = raw.decode("utf-8")
        if not jsonl:
            value = json.loads(text, parse_float=finite_number, parse_constant=finite_number)
            if not isinstance(value, dict):
                raise ValueError("expected a JSON object")
            return value
        rows = []
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line, parse_float=finite_number, parse_constant=finite_number)
                if not isinstance(row, dict):
                    raise ValueError("expected an object")
                rows.append(row)
            except (ValueError, RecursionError):
                warnings.append(f"{name}: invalid record on line {number}; other records are shown")
        return rows
    except FileNotFoundError:
        return [] if jsonl else {}
    except (OSError, ValueError, RecursionError) as exc:
        warnings.append(f"{name}: {exc}")
        return [] if jsonl else {}


def load_file(directory, name, warnings, jsonl=False):
    """Parse a ledger file, reusing the previous parse while its inode, mtime, and size match.

    Callers treat the returned value as read-only; it is shared between polls.
    """
    path = directory / name
    empty = [] if jsonl else {}
    if path.is_symlink():
        warnings.append(f"{name}: symlink not read")
        return empty
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return empty
    except OSError as exc:
        warnings.append(f"{name}: {exc}")
        return empty
    key = (str(path), info.st_ino, info.st_mtime_ns, info.st_size)
    with _file_cache_lock:
        cached = _file_cache.get(key[0])
        if cached is not None:
            _file_cache.move_to_end(key[0])
    if cached and cached[0] == key:
        warnings.extend(cached[2])
        return cached[1]
    notices = []
    value = parse_ledger_file(path, name, notices, jsonl)
    with _file_cache_lock:
        _file_cache[key[0]] = (key, value, tuple(notices))
        _file_cache.move_to_end(key[0])
        trim_file_cache()
    warnings.extend(notices)
    return value


def trim_file_cache():
    """Evict least recently used parses beyond the capacity. Caller holds the cache lock."""
    while len(_file_cache) > _file_cache_capacity:
        _file_cache.popitem(last=False)


def size_file_cache(run_count):
    """Fit the parse cache to the ledgers one snapshot reads. A fixed 64 entries with
    oldest-first eviction meant that with a dozen archived runs the "All runs" view evicted
    its own files and re-parsed every ledger on every poll."""
    global _file_cache_capacity
    with _file_cache_lock:
        _file_cache_capacity = max(FILE_CACHE_LIMIT, min(FILE_CACHE_CEILING, LEDGER_FILES_PER_RUN * (run_count + 1)))
        trim_file_cache()


def age_seconds(value, now=None):
    """Seconds since an ISO UTC stamp, or None when the stamp is missing or malformed."""
    try:
        return max(0, int(time.time() if now is None else now) - epoch(value))
    except StateError:
        return None


def finite_or_none(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def count_field(control, key):
    value = control.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def cycle_history(control):
    """The supervisor's per-cycle records, shape-checked and capped for the browser."""
    rows = control.get("cycle_history")
    if not isinstance(rows, list):
        return []
    return [{"started_at": row.get("started_at") if isinstance(row.get("started_at"), str) else None,
             "seconds": finite_or_none(row.get("seconds")),
             "rc": finite_or_none(row.get("rc")),
             "advanced": bool(row.get("advanced")),
             "limited": bool(row.get("limited")),
             "outcome": text_value(row.get("outcome"))[:200] or None}
            for row in [row for row in rows if isinstance(row, dict)][-CYCLE_HISTORY_LIMIT:]]


def text_value(value, fallback=""):
    return value if isinstance(value, str) and value.strip() else fallback


def normalize_tasks(rows, run, run_id, warnings):
    tasks = {}
    for number, row in enumerate(rows, 1):
        task_id = text_value(row.get("id"), f"legacy-{number}")
        # Accept both rewritten ledgers and append-only updates with the same ID.
        tasks[task_id] = {**tasks.get(task_id, {}), **row, "id": task_id}
    active = run.get("active_item")
    if isinstance(active, dict) and text_value(active.get("id")):
        task_id = active["id"]
        tasks[task_id] = {**tasks.get(task_id, {}), **active, "id": task_id,
                          "status": "in_progress"}
    result = []
    aliases = {"benched": "blocked", "doing": "in_progress", "in-progress": "in_progress"}
    known = {key for key, _ in COLUMNS}
    for task_id, row in tasks.items():
        status = text_value(row.get("status"), "ready")
        status = aliases.get(status, status)
        if status not in known:
            warnings.append(f"Task {task_id}: unknown status; shown in Blocked for review")
            status = "blocked"
        interrupted = status == "in_progress" and bool(run.get("outcome"))
        if interrupted:
            status = "blocked"
        task = {key: row[key] for key in TASK_FIELDS if key in row}
        task.update(id=task_id, key=f"{run_id}:{task_id}", run_id=run_id, status=status,
                    title=text_value(row.get("title"), text_value(row.get("claim"), task_id)),
                    area=text_value(row.get("area"), "general"))
        if interrupted:
            task["note"] = (text_value(row.get("note")) + "\nRun ended before this task was settled.").strip()
        result.append(task)
    return result


def normalize_bets(rows, run, run_id, warnings):
    merged = {}
    for number, row in enumerate(rows, 1):
        key = text_value(row.get("id"), f"legacy-bet-{number}")
        merged[key] = {**merged.get(key, {}), **row, "id": key}
    phases = {"spiking": "in_progress", "placed": "in_progress", "landed": "done",
              "killed": "rejected", "proposed": "proposed"}
    converted = []
    for row in merged.values():
        phase = text_value(row.get("status"), "unknown")
        converted.append({**row, "bet_status": phase, "status": phases.get(phase, phase),
                          "title": text_value(row.get("title"), text_value(row.get("hypothesis"), row["id"])),
                          "claim": row.get("hypothesis"), "area": "performance"})
    tasks = normalize_tasks(converted, {**run, "active_item": None}, run_id, warnings)
    for task in tasks:
        task["key"] = f"{run_id}:bet:{task['id']}"
    return tasks


def run_directories(repo):
    state = state_directory(repo)
    directories = {"current": state}
    history = state / "history"
    if history.is_dir() and not history.is_symlink():
        for path in sorted(history.iterdir(), reverse=True):
            if (path.name not in ("current", "all") and path.is_dir() and
                    not path.is_symlink() and re.fullmatch(r"[\w.-]+", path.name)):
                directories[path.name] = path
    return directories


def focus_summary(focus):
    """Expose the worker-recorded area focus without trusting its shape."""
    if not isinstance(focus, dict):
        return {}
    board = focus.get("board_areas")
    areas = {}
    if isinstance(board, dict):
        for level in ("prefer", "skip"):
            names = board.get(level)
            if isinstance(names, list):
                areas.update({name: level for name in names if isinstance(name, str) and name in AREAS})
    excluded = focus.get("excluded")
    themes = board.get("themes") if isinstance(board, dict) else None
    return {"areas": areas,
            "themes": [theme for theme in THEMES if isinstance(themes, list) and theme in themes],
            "request_id": board.get("request_id") if isinstance(board, dict) else None,
            "applied_at": board.get("at") if isinstance(board, dict) else None,
            "excluded": [text_value(item)[:200] for item in excluded[:20]] if isinstance(excluded, list) else []}


def board_snapshot(repo, selected="current", include=SNAPSHOT_EXTRAS):
    """Build the polled board state. `include` names the optional sections to compute."""
    state = state_directory(repo)
    warnings = []
    now = int(time.time())
    directories = run_directories(repo)
    size_file_cache(len(directories))
    if selected != "all" and selected not in directories:
        raise StateError("unknown run")
    runs, tasks, discovery, requests = [], [], [], []
    current = {}
    for run_id, directory in directories.items():
        notices = []
        run = load_file(directory, "run.json", notices)
        control = load_file(directory, "supervisor.json", notices)
        # A worker may announce completion before the supervisor validates its exit
        # and deadline. A saved null supervisor outcome is still authoritative.
        outcome = control.get("outcome") if control else run.get("outcome")
        if control and run.get("outcome") and not outcome:
            notices.append("Worker outcome awaits supervisor confirmation; the run is not yet ended.")
        clock = None
        if run or control:
            try:
                clock = recorded_timing(run, control or None)
                if not control and outcome == "completed" and clock["remaining_seconds"] > 0:
                    outcome = None
                    notices.append("Premature completion ignored: the recorded deadline has not passed.")
            except StateError as exc:
                notices.append(f"Clock: {exc}")
        summary = {"id": run_id, "label": "Current run" if run_id == "current" else run_id,
                   "outcome": outcome,
                   "report_available": (directory / "final-report.md").is_file() and not (directory / "final-report.md").is_symlink(),
                   "summary_pending": control.get("summary_pending", run.get("summary_pending")),
                   "started_at": (control if control else run).get("started_at"),
                   "control_id": (control or run).get("run_id") or (control or run).get("started_at"),
                   "deadline": (control if control else run).get("deadline")}
        runs.append(summary)
        if run_id == "current":
            current = {**summary, "branch": run.get("branch"), "engine": control.get("engine"),
                       "control_id": (control or run).get("run_id", summary["started_at"]),
                       "phase": control.get("phase") or run.get("phase"), "cycle": run.get("cycle", 0),
                       "next_action": run.get("next_action"), "summary_pending": control.get("summary_pending", run.get("summary_pending")),
                       "retry_at": control.get("retry_at"), "daemon_running": False,
                       "supervised": bool(control), "model": control.get("model"),
                       "heartbeat_at": control.get("heartbeat_at"), "last_activity_at": run.get("last_activity_at"),
                       "consecutive_failures": control.get("consecutive_failures", 0),
                       "board_control_version": control.get("board_control_version", 0),
                       "last_error": control.get("last_error"), "clock": clock,
                       "focus": focus_summary(run.get("focus")),
                       # Supervisor bookkeeping for run health; absent fields read as null or zero.
                       "cycle_started_at": text_value(control.get("cycle_started_at")) or None,
                       "child_pid": finite_or_none(control.get("child_pid")),
                       "cycle_timeout_seconds": finite_or_none(control.get("cycle_timeout_seconds")),
                       "finish_grace_seconds": finite_or_none(control.get("finish_grace_seconds")),
                       "quarantines": count_field(control, "quarantines"),
                       "recoveries_total": count_field(control, "recoveries_total"),
                       "crashes": count_field(control, "crashes"),
                       "parks": count_field(control, "parks"),
                       "idle_cycles": count_field(control, "idle_cycles"),
                       "idle_gap": finite_or_none(control.get("idle_gap")),
                       "limit_waited_seconds": count_field(control, "limit_waited_seconds"),
                       "next_limit_wait": finite_or_none(control.get("next_limit_wait")),
                       "park_until": text_value(control.get("park_until")) or None,
                       "ended_at": text_value(control.get("ended_at")) or None,
                       "cycle_history": cycle_history(control),
                       "heartbeat_age_seconds": age_seconds(control.get("heartbeat_at"), now),
                       "cycle_age_seconds": age_seconds(control.get("cycle_started_at"), now)}
            if not (state / "daemon.lock").is_symlink():
                with contextlib.suppress(OSError):
                    current["daemon_running"] = lock_active(state / "daemon.lock")
        if selected in ("all", run_id):
            if run_id != "current":
                try:
                    requests.extend({**row, "source_run": run_id, "key": f"{run_id}:{row['id']}"}
                                    for row in request_view_at(directory))
                except (StateError, OSError) as exc:
                    notices.append(f"Requests: {exc}")
            rows = load_file(directory, "backlog.jsonl", notices, jsonl=True)
            effective_run = {**run, "outcome": summary["outcome"]}
            tasks.extend(normalize_tasks(rows, effective_run, run_id, notices))
            bets = load_file(directory, "bets.jsonl", notices, jsonl=True)
            tasks.extend(normalize_bets(bets, effective_run, run_id, notices))
            for row in load_file(directory, "discovery.jsonl", notices, jsonl=True)[-8:]:
                discovery.append({key: row[key] for key in ("at", "lane", "scope", "hypothesis", "status", "evidence", "accepted") if key in row})
        # The current run header remains visible even when viewing an archive.
        if selected in ("all", run_id) or run_id == "current":
            warnings.extend(f"{summary['label']}: {notice}" for notice in notices)
    discovery.sort(key=lambda row: text_value(row.get("at")), reverse=True)
    counts = {key: sum(task["status"] == key for task in tasks) for key, _ in COLUMNS}
    controls = {"available": False, "requests_available": True, "pause_requested": False,
                "stop_requested": (state / "stop.request").exists(), "requests": [],
                "request_types": sorted(improve_control.REQUEST_TYPES),
                "report_available": (state / "final-report.md").is_file() and not (state / "final-report.md").is_symlink()}
    try:
        controls["pause_requested"] = pause_requested(repo, current.get("control_id"))
        controls["requests"] = request_view(repo)
        if selected in ("current", "all"):
            requests.extend({**row, "source_run": "current", "key": f"current:{row['id']}"}
                            for row in controls["requests"])
        if current.get("outcome"):
            controls["reason"] = "This run has ended. Resume or start a new run from your assistant."
            controls["requests_available"] = (current["outcome"] == "stopped by user" or
                                              isinstance(current["outcome"], str) and current["outcome"].startswith("halted:"))
        elif not current.get("daemon_running"):
            controls["reason"] = "Run controls need a running daemon. Saved requests can be read by either assistant."
        elif current.get("board_control_version") != 1:
            controls["reason"] = "Restart the daemon with the updated skill to enable board controls."
        elif current.get("phase") == "finalize":
            controls["reason"] = "The final report is being written; new work is closed."
            controls["requests_available"] = False
        elif not current.get("clock"):
            controls["reason"] = "Repair the run clock before using run controls."
        else:
            controls["available"] = True
            controls["reason"] = "Pause takes effect at a clean checkpoint. The deadline keeps running."
        if current.get("clock") and current["clock"]["remaining_seconds"] <= 0:
            controls["requests_available"] = False
            controls["reason"] = "The deadline has passed. Task history and saved requests remain available."
    except (StateError, OSError) as exc:
        controls.update(reason=str(exc), requests_available=False)
        warnings.append(f"Board controls: {exc}")
    by_request = {}
    task_keys = {task["key"] for task in tasks}
    for task in tasks:
        request_id = task.get("board_request_id")
        if isinstance(request_id, str):
            by_request.setdefault((task["run_id"], request_id), []).append(task["key"])
    for row in requests:
        links = list(by_request.get((row["source_run"], row["id"]), []))
        target = row.get("task_key")
        if isinstance(target, str) and target.startswith("current:"):
            target = row["source_run"] + target[len("current"):]
            if target in task_keys and target not in links:
                links.append(target)
        row["task_keys"] = links
    return {"repo": repo.name, "workspace_id": hashlib.sha256(str(repo.resolve()).encode()).hexdigest(),
            "selected_run": selected, "runs": runs, "current": current,
            "columns": [{"id": key, "label": label} for key, label in COLUMNS],
            "tasks": tasks, "requests": requests, "counts": counts, "discovery": discovery[:24],
            "controls": controls, "warnings": warnings,
            "learning": learning_view(repo, warnings) if "learning" in include else None,
            "graph": graph_summary(repo, warnings) if "graph" in include else None,
            "updated_at": stamp(now)}


def snapshot_etag(snapshot):
    """A content tag over everything the browser renders, minus the per-poll clock."""
    stable = {key: value for key, value in snapshot.items() if key != "updated_at"}
    current = dict(stable.get("current") or {})
    for key in VOLATILE_CURRENT_KEYS:
        current.pop(key, None)
    if isinstance(current.get("clock"), dict):
        current["clock"] = {key: value for key, value in current["clock"].items() if key not in VOLATILE_CLOCK_KEYS}
    stable["current"] = current
    digest = hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    return f'"{digest[:32]}"'


def etag_matches(header, etag):
    if not header:
        return False
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate in ("*", etag):
            return True
    return False


def learning_view(repo, warnings=None):
    """Cross-run learning for the overview; a damaged store only hides the panel."""
    from improve_learning import board_summary
    try:
        return board_summary(repo)
    except (StateError, OSError, ValueError, sqlite3.Error) as exc:
        if warnings is not None:
            warnings.append(f"Learning: {exc}")
        return None
    except Exception as exc:  # The overview panel is optional; the board must keep polling.
        if warnings is not None:
            warnings.append(f"Learning: unexpected {type(exc).__name__}: {exc}")
        return None


def graph_summary(repo, warnings=None):
    """Cheap graph counters; the Knowledge view fetches the graph itself only when they change."""
    import improve_graph
    try:
        return improve_graph.summary(improve_graph.load_cached(improve_graph.graph_path(repo)))
    except (StateError, OSError, ValueError, sqlite3.Error) as exc:
        if warnings is not None:
            warnings.append(f"Knowledge graph: {exc}")
        return None
    except Exception as exc:  # A graph fault must not take the task board down with it.
        if warnings is not None:
            warnings.append(f"Knowledge graph: unexpected {type(exc).__name__}: {exc}")
        return None


def bounded_tail(path, limit):
    """The last `limit` bytes of a regular file, never following a symlink; None when absent."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StateError(f"cannot read {path.name}") from exc
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise StateError(f"{path.name} must be a regular file")
        truncated = info.st_size > limit
        if truncated:
            stream.seek(info.st_size - limit)
        raw = stream.read(limit)
    text = raw.decode("utf-8", errors="replace")
    if truncated and "\n" in text:
        # Start on a whole line rather than mid-record.
        text = text.split("\n", 1)[1]
    return {"text": text, "truncated": truncated, "size": info.st_size}


def journal_view(repo, selected):
    directory = run_directories(repo).get(selected)
    if directory is None:
        raise StateError("unknown run")
    tail = bounded_tail(directory / "journal.md", JOURNAL_LIMIT)
    if tail is None:
        raise BoardError("No journal yet.", code="journal_missing")
    return {**tail, "run_id": selected}


def log_view(repo, requested):
    """The last lines of the current run's daemon log, bounded in lines and bytes."""
    try:
        lines = int(requested) if requested is not None else LOG_LINES_DEFAULT
    except ValueError as exc:
        raise StateError("lines must be a whole number") from exc
    lines = max(1, min(lines, LOG_LINES_MAX))
    tail = bounded_tail(state_directory(repo) / "daemon.log", LOG_LIMIT)
    if tail is None:
        raise BoardError("No daemon log yet.", code="log_missing")
    rows = tail["text"].splitlines()
    return {"text": "\n".join(rows[-lines:]), "line_count": min(lines, len(rows)),
            "truncated": tail["truncated"] or len(rows) > lines, "size": tail["size"]}


def quarantine_refs(repo):
    """Quarantined worktrees saved by the supervisor as refs, newest first.

    The recovery command is meant to be pasted into a shell. Git accepts ref names such as
    `refs/improve/quarantine/$(cmd)`, so only refs named the way the supervisor names them are
    listed, and the ref is shell-quoted anyway; other refs are counted, never echoed.
    """
    output = git(repo, "for-each-ref", "--sort=-creatordate",
                 "--format=%(refname)%09%(objectname)%09%(creatordate:iso-strict)%09%(subject)",
                 "refs/improve/quarantine")
    refs, hidden = [], 0
    for line in output.splitlines():
        parts = line.split("\t", 3)
        if len(parts) < 3 or not parts[0]:
            continue
        if not QUARANTINE_REF.fullmatch(parts[0]) or not re.fullmatch(r"[0-9a-f]{7,64}", parts[1]):
            hidden += 1
            continue
        refs.append({"ref": parts[0], "commit": parts[1], "date": parts[2],
                     "subject": (parts[3] if len(parts) > 3 else "")[:200],
                     "recover": f"git checkout {shlex.quote(parts[0])} -- ."})
    return {"refs": refs[:QUARANTINE_LIMIT], "hidden": hidden}


def query_values(query, name, limit=200):
    return [value[:limit] for value in query.get(name, [])]


def query_int(value, name, low, high):
    """A whole-number query parameter clamped to [low, high]. More than nine digits is refused
    before conversion: a 30-digit offset used to overflow SQLite's integer binding and drop
    the connection instead of answering."""
    if not isinstance(value, str) or not GRAPH_INT_PATTERN.fullmatch(value.strip()):
        raise StateError(f"{name} must be a whole number")
    return max(low, min(int(value), high))


GRAPH_GET_PATHS = ("/api/graph", "/api/graph/node", "/api/graph/context", "/api/graph/history",
                   "/api/graph/questions")
QUESTION_LIMIT = 12


def graph_get(repo, path, query):
    """Read-only graph endpoints for the Knowledge view and the task dialog."""
    import improve_graph
    graph = improve_graph.load_cached(improve_graph.graph_path(repo))
    first = lambda name, fallback=None: (query_values(query, name) or [fallback])[0]
    if path == "/api/graph":
        types = [kind for kind in first("types", "").split(",") if kind in improve_graph.TYPES]
        # board_view clamps the limit to its own 10..800 range; the parse only refuses junk.
        view = improve_graph.board_view(graph, limit=query_int(first("limit", "250"), "limit", -1000, 100000),
                                        focus=first("focus"), depth=query_int(first("depth", "1"), "depth", 1, 3),
                                        query=first("q"), types=types or None,
                                        include_archived=first("archived") == "1")
        return {key: value for key, value in view.items() if key not in UNUSED_GRAPH_VIEW_KEYS}
    if path == "/api/graph/node":
        # With the repository, insight details carry `code_changed` (files edited since the
        # note was recorded); links carry their `tier`.
        return improve_graph.node_detail(graph, first("id", ""), repo=repo)
    if path == "/api/graph/history":
        types = [kind for kind in first("types", "").split(",") if kind in improve_graph.TYPES]
        return improve_graph.history(repo, query_int(first("limit", "20"), "limit", 1, 100),
                                     query_int(first("offset", "0"), "offset", 0, HISTORY_OFFSET_MAX),
                                     types or None, first("q"), first("restored") == "1")
    if path == "/api/graph/context":
        files = query_values(query, "file", 300)[:MAX_CONTEXT_FILES]
        area = first("area")
        if area is not None and area not in AREAS and not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", area):
            area = None
        exclude = first("exclude")
        try:
            # The open task is excluded inside the ranking, so it neither weights the other
            # results nor takes one of the six slots.
            result = improve_graph.context(graph, files, area, first("text"), 6, repo, exclude=exclude)
        except improve_graph.GraphError:
            result = {"seeds": [], "files": [], "insights": [], "past_tasks": [], "blockers": [], "gates": [], "co_changed": []}
        if "code" not in result:
            code = code_context(repo, files)
            if code:
                result["code"] = code
        return {**result, "total_nodes": len(graph["nodes"])}
    if path == "/api/graph/questions":
        return {"questions": suggested_questions(graph, repo), "revision": graph.get("revision", 0)}
    raise StateError("not found")


def code_context(repo, files):
    """Dependents, imports, and code notes for the task's files. The cached board graph has
    no open database (load_cached closes it), so `context()` on it never reaches the code
    layer; a short read-only session supplies it. Optional: any failure just omits it."""
    import improve_graph
    paths = list(dict.fromkeys(path for path in (improve_graph.safe_path(value, repo) for value in files) if path))
    if not paths:
        return None
    try:
        with improve_graph.reading(repo) as session:
            return improve_graph.code_context(session, paths)
    except (StateError, OSError, ValueError, sqlite3.Error):
        return None


def suggested_questions(graph, repo):
    """What the graph suggests a person look at; `node` says whether the id opens in the graph
    (a module group id, for example, does not)."""
    from improve_analysis import suggested_questions as suggest
    nodes = graph["nodes"]
    rows = []
    for row in suggest(graph, repo, QUESTION_LIMIT):
        if not isinstance(row, dict) or not isinstance(row.get("question"), str):
            continue
        rows.append({"kind": str(row.get("kind", "")), "id": str(row.get("id", "")),
                     "question": row["question"][:600], "node": row.get("id") in nodes,
                     **({"other": row["other"]} if row.get("other") in nodes else {})})
    return rows


@contextlib.contextmanager
def bounded_operator_lock(repo, seconds):
    """improve_control.operator_lock with a deadline: the same lock file and flock, polled
    without blocking, so a read endpoint gives up (BoardError code "busy") instead of waiting
    on a stuck holder."""
    state = state_directory(repo)
    fd = os.open(state / "operator.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise StateError("operator.lock must be a regular file")
    with os.fdopen(fd, "a+") as stream:
        until = time.monotonic() + seconds
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise BoardError("The run state is busy; try again in a moment.", code="busy") from None
                time.sleep(0.05)
        try:
            yield state
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def graph_post(repo, data):
    """Curate the graph from the board. Retried requests with the same request_id apply once.
    Every curated write goes through the shared write path, so it is journaled and survives a
    recovery from the last checkpoint."""
    import improve_graph
    import improve_graph_journal as journal
    action, request_id = data.get("action"), data.get("request_id")
    if not isinstance(request_id, str) or not re.fullmatch(r"[\w-]{8,80}", request_id):
        raise StateError("a request_id is required")
    with improve_graph.locked(repo) as graph:
        done = graph.setdefault("board_ops", [])
        previous = next((row for row in done if row.get("request_id") == request_id), None)
        if previous:
            return previous["result"]
        if action == "note":
            about = data.get("about") or []
            if not isinstance(about, list) or len(about) > 6 or not all(isinstance(item, str) and len(item) <= 300 for item in about):
                raise StateError("about must list at most six file:PATH or area:NAME targets")
            text, kind = data.get("text"), data.get("kind", "fact")
            if not isinstance(text, str) or len(text) > improve_graph.MAX_SUMMARY:
                raise StateError(f"knowledge must be text of at most {improve_graph.MAX_SUMMARY} characters")
            node = journal.apply_write(graph, "note", {"text": text, "kind": kind, "source": "user",
                                                        "about": [item for item in about if item.strip()]}, repo)
            node["score"] = round(improve_graph.score(node, improve_graph.run_seq(graph)), 4)
            result = {"node": improve_graph.node_view(node), "message": "Saved. The skill will see this in its next cycle."}
        elif action == "rate":
            node = journal.apply_write(graph, "rate", {"id": str(data.get("id", ""))[:400], "verdict": data.get("verdict"),
                                                        "source": "user"}, repo)
            node["score"] = round(improve_graph.score(node, improve_graph.run_seq(graph)), 4)
            result = {"node": improve_graph.node_view(node), "message": {
                "useful": "Marked useful; it will rank higher.", "wrong": "Marked wrong; the skill will stop using it.",
                "pin": "Pinned; the skill sees it every cycle.", "unpin": "Unpinned.",
                "archive": "Archived; hidden from the skill.", "restore": "Restored."}[data.get("verdict")]}
        elif action == "dream":
            from improve_dream import dream
            report = dream(graph, repo)
            inferred = report["inferred"]
            result = {"dream": report, "message": f"Dreamed: {inferred['new']} new links, {inferred['dropped']} dropped, "
                                                  f"{report['archived']} archived, {report['forgotten']} forgotten."}
        elif action == "restore":
            node = journal.apply_write(graph, "restore", {"id": str(data.get("id", ""))[:400],
                                                           "reason": "restored from the board", "source": "user"}, repo)
            result = {"node": improve_graph.node_view(node), "message": "Restored with its links. The skill sees it again."}
        elif action == "reject_link":
            fields = [data.get(name) for name in ("src", "rel", "dst")]
            if not all(isinstance(value, str) and 0 < len(value) <= 400 for value in fields):
                raise StateError("src, rel, and dst are required")
            reason = data.get("reason") if isinstance(data.get("reason"), str) else None
            removed = journal.apply_write(graph, "unlink", {"src": fields[0], "rel": fields[1], "dst": fields[2], "by": "user",
                                                            "reason": reason, "source": "user"}, repo)
            result = {**removed, "message": "Link removed. Dreaming will not infer it again."}
        elif action == "refine":
            report = improve_graph.refine(graph, repo)
            result = {"refine": report, "message": f"Refined: {report['merged']} merged, {report['faded']} faded, {report['stale_files']} stale files."}
        else:
            raise StateError("unknown graph action")
        done.append({"request_id": request_id, "result": result})
        del done[:-50]
        return result


def local_request(url, **kwargs):
    # Local board traffic must not be sent through configured HTTP proxies.
    with build_opener(ProxyHandler({})).open(Request(url, **kwargs), timeout=1) as response:
        return json.load(response)


def board_status(repo):
    path = state_directory(repo) / "board.json"
    if not path.exists() or path.is_symlink():
        return {"running": False}
    try:
        saved = read_json(path)
        port = saved.get("port")
        if type(port) is not int or not 1 <= port <= 65535:
            return {"running": False}
        url = f"http://127.0.0.1:{port}"
        if saved.get("url") != url or saved.get("repo") != str(repo):
            return {"running": False}
        health = local_request(url + "/api/health")
        running = (isinstance(health, dict) and health.get("service") == "improve-board" and health.get("instance") == saved.get("instance")
                   and health.get("repo") == str(repo))
        hosts = saved.get("allowed_hosts")
        hosts = sorted(host for host in hosts if isinstance(host, str)) if isinstance(hosts, list) else []
        return {"running": running, "url": url, "pid": saved.get("pid"), "started_at": saved.get("started_at"),
                "allowed_hosts": hosts, "phone_urls": [f"https://{host}" for host in hosts]}
    except (OSError, ValueError, URLError):
        return {"running": False}


class BoardServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind also resolves the bind address to a host name. Where the resolver
        # is slow that lookup blocks for tens of seconds (35 on a macOS CI runner), longer than
        # start_board waits. The board never uses the name, so bind without the lookup.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class BoardHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def allowed_host(self):
        port = self.server.server_port
        host = self.headers.get("Host", "")
        return host in (f"127.0.0.1:{port}", f"localhost:{port}") or host in self.server.allowed_hosts

    def board_origin(self):
        """The only Origin a same-origin browser request may carry for this Host."""
        host = self.headers.get("Host", "")
        # Extra hosts are HTTPS tunnels (for example Tailscale Serve) that a phone opens.
        return ("https://" if host in self.server.allowed_hosts else "http://") + host

    def accepts_gzip(self):
        header = self.headers.get("Accept-Encoding", "")
        return any(part.split(";", 1)[0].strip().lower() == "gzip" for part in header.split(","))

    def security_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "microphone=(self), camera=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")

    def reply(self, code, body, content_type="application/json; charset=utf-8", etag=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=True).encode()
        encoded = len(body) > GZIP_MIN_BYTES and self.accepts_gzip()
        if encoded:
            body = gzip.compress(body, compresslevel=5)
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if encoded:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        if etag:
            self.send_header("ETag", etag)
        self.security_headers()
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def reply_not_modified(self, etag):
        self.send_response(304)
        self.send_header("ETag", etag)
        self.security_headers()
        self.end_headers()

    def reply_board(self, query):
        """The polled snapshot: no operator lock (ledgers are replaced atomically), a content
        ETag for 304 replies, and optional sections only when the browser asks for them."""
        selected = query.get("run", ["current"])[0]
        include = query.get("include")
        wanted = SNAPSHOT_EXTRAS if include is None else tuple(
            part for part in include[0].split(",") if part in SNAPSHOT_EXTRAS)
        try:
            snapshot = board_snapshot(self.server.repo, selected, wanted)
        except (StateError, OSError, ValueError) as exc:
            return self.reply(400, error_payload(exc))
        except Exception as exc:  # Never drop the connection; report the fault as JSON.
            return self.internal_error("board snapshot", exc)
        self.server.snapshot_built_at = time.monotonic()
        etag = snapshot_etag(snapshot)
        if etag_matches(self.headers.get("If-None-Match"), etag):
            return self.reply_not_modified(etag)
        return self.reply(200, snapshot, etag=etag)

    def health(self):
        """Identity plus cheap liveness: the board's own snapshot age, the daemon's heartbeat,
        and the running cycle's age. The snapshot ETag leaves these out, so the browser polls
        this small payload to keep ages right while the snapshot itself answers 304."""
        payload = {"service": "improve-board", "instance": self.server.instance, "repo": str(self.server.repo),
                   "snapshot_age_seconds": None, "daemon_running": False, "control_id": None,
                   "heartbeat_at": None, "heartbeat_age_seconds": None,
                   "cycle_started_at": None, "cycle_age_seconds": None}
        built = getattr(self.server, "snapshot_built_at", None)
        if built is not None:
            payload["snapshot_age_seconds"] = round(time.monotonic() - built, 1)
        try:
            state = state_directory(self.server.repo)
            control = load_file(state, "supervisor.json", [])
            run = load_file(state, "run.json", [])
            source = control or run
            now = int(time.time())
            payload.update(
                control_id=text_value(source.get("run_id")) or text_value(source.get("started_at")) or None,
                heartbeat_at=text_value(control.get("heartbeat_at")) or None,
                heartbeat_age_seconds=age_seconds(control.get("heartbeat_at"), now),
                cycle_started_at=text_value(control.get("cycle_started_at")) or None,
                cycle_age_seconds=age_seconds(control.get("cycle_started_at"), now))
            if not (state / "daemon.lock").is_symlink():
                payload["daemon_running"] = lock_active(state / "daemon.lock")
        except (StateError, OSError, ValueError, AttributeError):
            pass  # Liveness fields are best-effort; identity must always answer.
        return payload

    def internal_error(self, what, exc):
        """Never drop the connection on an unexpected fault; report it as JSON."""
        traceback.print_exc()
        return self.reply(500, {"error": f"{what} failed: {type(exc).__name__}: {exc}", "code": "internal"})

    def do_GET(self):
        if not self.allowed_host():
            return self.reply(403, {"error": "local host required"})
        target = urlsplit(self.path)
        if target.path.startswith("/api/") and self.headers.get("Sec-Fetch-Site") not in SAME_SITE_FETCH:
            # A page on another site (or a sibling subdomain) may not read board state or
            # drive the operator lock through /api/report, even though it cannot see replies.
            return self.reply(403, {"error": "same-origin board request required", "code": "cross_site"})
        if target.path == "/api/session":
            # A custom header prevents cross-site forms/images from retrieving the
            # browser capability. No CORS response is ever sent.
            origin = self.headers.get("Origin")
            if (self.headers.get("X-Improve-Client") != "board" or
                    self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none") or
                    origin not in (None, self.board_origin())):
                return self.reply(403, {"error": "same-origin board session required"})
            return self.reply(200, {"token": self.server.ui_token})
        if target.path == "/api/report":
            try:
                selected = parse_qs(target.query, max_num_fields=8).get("run", ["current"])[0]
                with bounded_operator_lock(self.server.repo, REPORT_LOCK_SECONDS):
                    directory = run_directories(self.server.repo).get(selected)
                    if directory is None:
                        raise StateError("unknown run")
                    report = read_local(directory / "final-report.md", limit=512 * 1024)
                if report is None:
                    return self.reply(404, {"error": "No saved final report yet."})
                return self.reply(200, {"text": report, "run_id": selected})
            except BoardError as exc:
                return self.reply(503 if exc.code == "busy" else 400, error_payload(exc))
            except (StateError, OSError, ValueError) as exc:
                return self.reply(400, error_payload(exc))
            except Exception as exc:
                return self.internal_error("reading the report", exc)
        if target.path in GRAPH_GET_PATHS:
            try:
                query = parse_qs(target.query, max_num_fields=40)
                return self.reply(200, graph_get(self.server.repo, target.path, query))
            except (StateError, OSError, ValueError, sqlite3.Error) as exc:
                return self.reply(400, error_payload(exc))
            except Exception as exc:
                return self.internal_error("graph read", exc)
        if target.path == "/api/health":
            return self.reply(200, self.health())
        if target.path == "/api/board":
            return self.reply_board(parse_qs(target.query, max_num_fields=8, keep_blank_values=True))
        if target.path in ("/api/journal", "/api/log", "/api/quarantine"):
            # Operational views are read-only and bounded; they never need the operator lock.
            try:
                query = parse_qs(target.query, max_num_fields=8)
                if target.path == "/api/journal":
                    return self.reply(200, journal_view(self.server.repo, query.get("run", ["current"])[0]))
                if target.path == "/api/log":
                    return self.reply(200, log_view(self.server.repo, query.get("lines", [None])[0]))
                return self.reply(200, quarantine_refs(self.server.repo))
            except BoardError as exc:
                return self.reply(404 if exc.code in ("journal_missing", "log_missing") else 400, error_payload(exc))
            except (StateError, OSError, ValueError) as exc:
                return self.reply(400, error_payload(exc))
            except Exception as exc:
                return self.internal_error("operational view", exc)
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/board.js": ("board.js", "text/javascript; charset=utf-8"),
                  "/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
                  "/health.js": ("health.js", "text/javascript; charset=utf-8"),
                  "/controls.js": ("controls.js", "text/javascript; charset=utf-8"),
                  "/workflow.js": ("workflow.js", "text/javascript; charset=utf-8"),
                  "/drafts.js": ("drafts.js", "text/javascript; charset=utf-8"),
                  "/triage.js": ("triage.js", "text/javascript; charset=utf-8"),
                  "/focus.js": ("focus.js", "text/javascript; charset=utf-8"),
                  "/categories.js": ("categories.js", "text/javascript; charset=utf-8"),
                  "/voice-intent.js": ("voice-intent.js", "text/javascript; charset=utf-8"),
                  "/voice.js": ("voice.js", "text/javascript; charset=utf-8"),
                  "/mobile.js": ("mobile.js", "text/javascript; charset=utf-8"),
                  "/overview.js": ("overview.js", "text/javascript; charset=utf-8"),
                  "/graph.js": ("graph.js", "text/javascript; charset=utf-8"),
                  "/graph-layout.js": ("graph-layout.js", "text/javascript; charset=utf-8"),
                  "/board.css": ("board.css", "text/css; charset=utf-8")}
        if target.path in assets:
            name, mime = assets[target.path]
            try:
                return self.reply(200, (ASSETS / name).read_bytes(), mime)
            except OSError:
                return self.reply(404, {"error": f"board asset {name} is missing from this installation", "code": "asset_missing"})
        self.reply(404, {"error": "not found", "code": "not_found"})

    def do_POST(self):
        origin = self.headers.get("Origin")
        token = self.headers.get("X-Improve-Board-Token", "").encode()
        same_origin = origin == self.board_origin()
        if not self.allowed_host():
            return self.reply(403, {"error": "not authorized"})
        if self.path == "/api/stop":
            if (origin is not None and not same_origin) or not secrets.compare_digest(token, self.server.token.encode()):
                return self.reply(403, {"error": "not authorized"})
            self.reply(200, {"stopping": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if (not same_origin or not secrets.compare_digest(token, self.server.ui_token.encode()) or
                self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none")):
            return self.reply(403, {"error": "Refresh the board to reconnect its controls."})
        if self.path not in ("/api/control", "/api/requests", "/api/requests/batch", "/api/graph"):
            return self.reply(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if (not 0 < length <= 65536 or self.headers.get("Transfer-Encoding") or
                    self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json"):
                return self.reply(400, {"error": "a JSON request of at most 64 KiB is required"})
            self.connection.settimeout(5)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise StateError("incomplete request")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise StateError("a JSON object is required")
            if self.path == "/api/graph":
                # Curating knowledge does not need a live run: it shapes the next one.
                return self.reply(200, graph_post(self.server.repo, data))
            with operator_lock(self.server.repo) as state:
                require_run(self.server.repo, data.get("run_id"))
                snapshot = board_snapshot(self.server.repo, "current", include=())
                controls = snapshot["controls"]
                if self.path in ("/api/requests", "/api/requests/batch"):
                    if not controls["requests_available"]:
                        raise StateError(controls["reason"])
                    kind = data.get("type")
                    if kind in OPTIONAL_REQUEST_TYPES and kind not in improve_control.REQUEST_TYPES:
                        raise BoardError(f"This skill runtime does not accept {kind} requests yet; "
                                         "update the skill and restart the daemon.", code="unsupported_request_type")
                    if self.path.endswith("/batch"):
                        requests = queue_submission(self.server.repo, data, snapshot["tasks"], batch=True)
                        return self.reply(200, {"requests": requests, "message": f"Saved {len(requests)} priority requests. Each will be reviewed at a checkpoint."})
                    request = queue_request(self.server.repo, data, snapshot["tasks"])
                    return self.reply(200, {"request": request, "message": "Request saved. The skill will review it at its next checkpoint."})
                if not controls["available"]:
                    raise StateError(controls["reason"])
                action = data.get("action")
                if action not in ("pause", "resume", "stop"):
                    raise StateError("unknown run control")
                if controls["stop_requested"]:
                    raise StateError("A stop is already pending; it cannot be undone from the board.")
                if action != "stop" and snapshot["current"]["clock"]["remaining_seconds"] <= 0:
                    raise StateError("The deadline has passed; no more improvement cycles can start.")
                if action == "stop":
                    if (state / "stop.request").is_symlink():
                        raise StateError("stop.request must not be a symlink")
                    (state / "stop.request").touch()
                else:
                    set_pause(self.server.repo, data.get("run_id"), action == "pause")
                return self.reply(200, {"action": action, "message": {
                    "pause": "Pause requested. The current work will checkpoint first.",
                    "resume": "Resume requested. The original deadline is unchanged.",
                    "stop": "Stop requested. The skill will settle current work and end the run."
                }[action]})
        except (StateError, OSError, ValueError, TypeError, RecursionError, sqlite3.Error) as exc:
            return self.reply(400, error_payload(exc))
        except Exception as exc:
            return self.internal_error("graph write" if self.path == "/api/graph" else "request", exc)


HOST_PATTERN = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")


def trusted_host(value):
    """Validate an HTTPS tunnel hostname that forwards to this loopback board."""
    value = value.strip().lower().rstrip(".")
    if value.startswith(("http://", "https://")) or "/" in value or ":" in value:
        raise argparse.ArgumentTypeError("--allow-host takes a bare hostname such as mymac.tailnet.ts.net")
    if not HOST_PATTERN.fullmatch(value) or value in ("localhost", "127.0.0.1"):
        raise argparse.ArgumentTypeError("--allow-host must be a fully qualified hostname")
    return value


def configure_server(server, repo, allowed_hosts=()):
    """Attach the per-service identity and secrets a BoardServer needs before it handles requests."""
    server.repo, server.instance, server.token = repo, secrets.token_hex(16), secrets.token_hex(32)
    server.ui_token = secrets.token_hex(32)
    server.allowed_hosts = frozenset(allowed_hosts)
    server.snapshot_built_at = None
    return server


def preferred_port():
    """The port tried first when none is given; IMPROVE_BOARD_PORT overrides the 8765 default."""
    try:
        value = int(os.environ.get("IMPROVE_BOARD_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT
    return value if 1 <= value <= 65535 else DEFAULT_PORT


def serve(repo, port=None, allowed_hosts=()):
    state = state_directory(repo)
    state.mkdir(exist_ok=True)
    if any((state / name).is_symlink() for name in SERVICE_FILES):
        raise StateError("board service files must not be symlinks")
    with repo_lock(state / "board.lock"):
        try:
            server = BoardServer(("127.0.0.1", preferred_port() if port is None else port), BoardHandler)
        except OSError:
            if port is not None:
                raise
            server = BoardServer(("127.0.0.1", 0), BoardHandler)
        configure_server(server, repo, allowed_hosts)
        info = dict(repo=str(repo), url=f"http://127.0.0.1:{server.server_port}", port=server.server_port,
                    pid=os.getpid(), instance=server.instance, token=server.token, started_at=stamp(),
                    allowed_hosts=sorted(server.allowed_hosts),
                    phone_urls=[f"https://{host}" for host in sorted(server.allowed_hosts)])
        atomic_json(state / "board.json", info)
        def stop(*_):
            threading.Thread(target=server.shutdown, daemon=True).start()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, stop)
        print(f"Kanban board: {info['url']}", flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            server.server_close()


def start_board(repo, port=None, allowed_hosts=()):
    state = state_directory(repo)
    state.mkdir(exist_ok=True)
    try:
        allowed_hosts = sorted({trusted_host(host) for host in allowed_hosts})
    except argparse.ArgumentTypeError as exc:
        raise StateError(str(exc)) from exc
    existing = board_status(repo)
    if existing["running"]:
        if port not in (None, 0, urlsplit(existing["url"]).port):
            raise StateError(f"board already runs at {existing['url']}; stop it before changing the port")
        if allowed_hosts and allowed_hosts != existing.get("allowed_hosts", []):
            raise StateError("board already runs with different --allow-host values; stop it first")
        return existing
    if any((state / name).is_symlink() for name in SERVICE_FILES):
        raise StateError("board service files must not be symlinks")
    command = [sys.executable, str(Path(__file__).resolve()), "--repo", str(repo)]
    if port is not None:
        command += ["--port", str(port)]
    for host in allowed_hosts:
        command += ["--allow-host", host]
    with (state / "board.log").open("ab") as log:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True, close_fds=True)
    try:
        until = time.monotonic() + 8
        exited_at = None
        while time.monotonic() < until:
            info = board_status(repo)
            if info["running"]:
                if port not in (None, 0, urlsplit(info["url"]).port):
                    raise StateError(f"board already runs at {info['url']}; stop it before changing the port")
                threading.Thread(target=child.wait, daemon=True).start()
                return info
            if child.poll() is not None:
                # Another simultaneous --start may own the lock but still be binding.
                exited_at = exited_at or time.monotonic()
                if time.monotonic() - exited_at >= 0.5:
                    raise StateError("board could not start; inspect .improve/board.log (the requested port may be busy)")
            time.sleep(0.1)
        raise StateError("board startup timed out; inspect .improve/board.log")
    except BaseException:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        raise


def stop_board(repo):
    status = board_status(repo)
    if not status["running"]:
        return {"running": False}
    saved = read_json(state_directory(repo) / "board.json")
    local_request(status["url"] + "/api/stop", method="POST", data=b"",
                  headers={"X-Improve-Board-Token": saved["token"]})
    until = time.monotonic() + 3
    while time.monotonic() < until:
        if not board_status(repo)["running"]:
            return {"running": False}
        time.sleep(0.1)
    raise StateError("board is still stopping; check --status again")


def port_number(value):
    value = int(value)
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError("port must be 0 through 65535 (0 chooses a free port)")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--port", type=port_number, help="default: 8765 or a free port if occupied")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--start", action="store_true", help="start a detached board, or reuse the existing one")
    action.add_argument("--status", action="store_true")
    action.add_argument("--stop", action="store_true", help="stop the board only, not the improvement run")
    parser.add_argument("--json", action="store_true", help="machine-readable start/status/stop output")
    parser.add_argument("--allow-host", action="append", type=trusted_host, default=[], metavar="HOSTNAME",
                        help="also accept this HTTPS tunnel hostname (for phone access); repeatable")
    args = parser.parse_args()
    try:
        repo = Path(args.repo).expanduser().resolve()
        if Path(git(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
            raise StateError("--repo must be the Git repository root")
        if args.status:
            info = board_status(repo)
        elif args.stop:
            info = stop_board(repo)
        elif args.start:
            info = start_board(repo, args.port, args.allow_host)
        else:
            if args.json:
                raise StateError("--json requires --start, --status, or --stop")
            serve(repo, args.port, args.allow_host)
            return 0
        if args.json:
            print(json.dumps(info))
        elif info["running"]:
            print("\n".join([info["url"], *(f"Phone: {url}" for url in info.get("phone_urls", []))]))
        else:
            print("board is not running")
        return 0
    except (OSError, ValueError) as exc:
        print(f"improve-board: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
