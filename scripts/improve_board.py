#!/usr/bin/env python3
"""Local, read-only Kanban board for improve. Python 3.9+, standard library only."""

import argparse
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from improve_runtime import StateError, atomic_json, git, lock_active, read_json, recorded_timing, repo_lock, stamp

ASSETS = Path(__file__).resolve().parent.parent / "assets/board"
SERVICE_FILES = {"board.json", "board.lock", "board.log"}
COLUMNS = [("ready", "Queued"), ("in_progress", "In progress"), ("done", "Done"),
           ("blocked", "Blocked"), ("proposed", "Proposed"), ("rejected", "Rejected")]
TASK_FIELDS = ("id", "title", "claim", "area", "kind", "file", "line", "paths", "owned_paths",
               "note", "evidence", "failure", "acceptance", "verification", "counter", "measurement",
               "commit", "focus_priority", "priority", "created_at", "updated_at", "started_at",
               "completed_at", "reopens", "last_verification", "bet_status", "journey",
               "target", "claimed_gain", "spike", "landed", "pieces", "kill", "branch")
MAX_FILE_BYTES = 32 * 1024 * 1024


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


def load_file(directory, name, warnings, jsonl=False):
    path = directory / name
    if path.is_symlink():
        warnings.append(f"{name}: symlink not read")
        return [] if jsonl else {}
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


def board_snapshot(repo, selected="current"):
    state = state_directory(repo)
    warnings = []
    directories = {"current": state}
    history = state / "history"
    if history.is_dir() and not history.is_symlink():
        for path in sorted(history.iterdir(), reverse=True):
            if path.is_dir() and not path.is_symlink() and re.fullmatch(r"[\w.-]+", path.name):
                directories[path.name] = path
    if selected != "all" and selected not in directories:
        raise StateError("unknown run")
    runs, tasks, discovery = [], [], []
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
                   "started_at": (control if control else run).get("started_at"),
                   "deadline": (control if control else run).get("deadline")}
        runs.append(summary)
        if run_id == "current":
            current = {**summary, "branch": run.get("branch"), "engine": control.get("engine"),
                       "phase": control.get("phase") or run.get("phase"), "cycle": run.get("cycle", 0),
                       "next_action": run.get("next_action"), "summary_pending": control.get("summary_pending", run.get("summary_pending")),
                       "retry_at": control.get("retry_at"), "daemon_running": False,
                       "supervised": bool(control), "model": control.get("model"),
                       "heartbeat_at": control.get("heartbeat_at"), "last_activity_at": run.get("last_activity_at"),
                       "consecutive_failures": control.get("consecutive_failures", 0),
                       "last_error": control.get("last_error"), "clock": clock}
            if not (state / "daemon.lock").is_symlink():
                with contextlib.suppress(OSError):
                    current["daemon_running"] = lock_active(state / "daemon.lock")
        if selected in ("all", run_id):
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
    return {"repo": repo.name, "selected_run": selected, "runs": runs, "current": current,
            "columns": [{"id": key, "label": label} for key, label in COLUMNS],
            "tasks": tasks, "counts": counts, "discovery": discovery[:24],
            "warnings": warnings, "updated_at": stamp()}


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
        return {"running": running, "url": url, "pid": saved.get("pid"), "started_at": saved.get("started_at")}
    except (OSError, ValueError, URLError):
        return {"running": False}


class BoardServer(ThreadingHTTPServer):
    daemon_threads = True


class BoardHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def allowed_host(self):
        port = self.server.server_port
        allowed = (f"127.0.0.1:{port}", f"localhost:{port}")
        return self.headers.get("Host") in allowed

    def reply(self, code, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=True).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def do_GET(self):
        if not self.allowed_host():
            return self.reply(403, {"error": "local host required"})
        target = urlsplit(self.path)
        if target.path == "/api/health":
            return self.reply(200, {"service": "improve-board", "instance": self.server.instance,
                                    "repo": str(self.server.repo)})
        if target.path == "/api/board":
            try:
                selected = parse_qs(target.query, max_num_fields=8).get("run", ["current"])[0]
                return self.reply(200, board_snapshot(self.server.repo, selected))
            except (StateError, OSError, ValueError) as exc:
                return self.reply(400, {"error": str(exc)})
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/board.js": ("board.js", "text/javascript; charset=utf-8"),
                  "/board.css": ("board.css", "text/css; charset=utf-8")}
        if target.path in assets:
            name, mime = assets[target.path]
            return self.reply(200, (ASSETS / name).read_bytes(), mime)
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        origin = self.headers.get("Origin")
        trusted = (None, f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}")
        token = self.headers.get("X-Improve-Board-Token", "").encode()
        if (not self.allowed_host() or origin not in trusted
                or not secrets.compare_digest(token, self.server.token.encode())):
            return self.reply(403, {"error": "not authorized"})
        if self.path != "/api/stop":
            return self.reply(404, {"error": "not found"})
        self.reply(200, {"stopping": True})
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def serve(repo, port=None):
    state = state_directory(repo)
    state.mkdir(exist_ok=True)
    if any((state / name).is_symlink() for name in SERVICE_FILES):
        raise StateError("board service files must not be symlinks")
    with repo_lock(state / "board.lock"):
        try:
            server = BoardServer(("127.0.0.1", 8765 if port is None else port), BoardHandler)
        except OSError:
            if port is not None:
                raise
            server = BoardServer(("127.0.0.1", 0), BoardHandler)
        server.repo, server.instance, server.token = repo, secrets.token_hex(16), secrets.token_hex(32)
        info = dict(repo=str(repo), url=f"http://127.0.0.1:{server.server_port}", port=server.server_port,
                    pid=os.getpid(), instance=server.instance, token=server.token, started_at=stamp())
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


def start_board(repo, port=None):
    state = state_directory(repo)
    state.mkdir(exist_ok=True)
    existing = board_status(repo)
    if existing["running"]:
        if port not in (None, 0, urlsplit(existing["url"]).port):
            raise StateError(f"board already runs at {existing['url']}; stop it before changing the port")
        return existing
    if any((state / name).is_symlink() for name in SERVICE_FILES):
        raise StateError("board service files must not be symlinks")
    command = [sys.executable, str(Path(__file__).resolve()), "--repo", str(repo)]
    if port is not None:
        command += ["--port", str(port)]
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
            info = start_board(repo, args.port)
        else:
            if args.json:
                raise StateError("--json requires --start, --status, or --stop")
            serve(repo, args.port)
            return 0
        print(json.dumps(info) if args.json else (info["url"] if info["running"] else "board is not running"))
        return 0
    except (OSError, ValueError) as exc:
        print(f"improve-board: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
