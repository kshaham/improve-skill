"""Durable board requests and cooperative run controls. Standard library only."""

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import uuid

from improve_runtime import StateError, atomic_json, stamp

MAX_BYTES = 8 * 1024 * 1024
AREAS = ("general", "features", "ui", "assets", "performance", "quality", "security",
         "coverage", "concurrency", "resilience", "gate-speed", "docs", "accessibility", "contracts")
REQUEST_TYPES = {"task", "guidance", "priority", "decision", "note"}


def state_path(repo):
    state = Path(repo) / ".improve"
    if state.is_symlink() or not state.is_dir():
        raise StateError("a local .improve directory is required")
    return state


def read_local(path, default=None, limit=MAX_BYTES):
    """Read a bounded regular file without following a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return default
    except OSError as exc:
        raise StateError(f"cannot read local {path.name}") from exc
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise StateError(f"{path.name} must be a regular file")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise StateError(f"{path.name} exceeds its size limit")
    try:
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise StateError(f"{path.name} is not UTF-8") from exc


def local_json(path, default=None):
    raw = read_local(path)
    if raw is None:
        return {} if default is None else default
    try:
        value = json.loads(raw, parse_constant=lambda _: None)
    except (ValueError, RecursionError) as exc:
        raise StateError(f"invalid {path.name}; saved requests were preserved") from exc
    if not isinstance(value, dict):
        raise StateError(f"{path.name} must contain an object")
    return value


@contextlib.contextmanager
def operator_lock(repo):
    state = state_path(repo)
    fd = os.open(state / "operator.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise StateError("operator.lock must be a regular file")
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield state
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def operator_state(repo):
    return request_state(state_path(repo))


def request_state(directory):
    value = local_json(directory / "operator.json", {"requests": []})
    rows = value.get("requests", [])
    if (not isinstance(rows, list) or len(rows) > 1000 or
            any(not isinstance(row, dict) or not isinstance(row.get("id"), str) or
                not isinstance(row.get("type"), str) or row["type"] not in REQUEST_TYPES for row in rows)):
        raise StateError("invalid operator.json requests; saved requests were preserved")
    value["requests"] = rows
    return value


def run_identity(repo):
    state = state_path(repo)
    control = local_json(state / "supervisor.json")
    run = control or local_json(state / "run.json")
    return run.get("run_id", run.get("started_at"))


def require_run(repo, expected):
    if expected != run_identity(repo):
        raise StateError("The current run changed. Refresh the board before sending this action.")


def pause_requested(repo, run_id):
    value = local_json(state_path(repo) / "pause.json")
    if type(value.get("paused", False)) is not bool:
        raise StateError("invalid pause request")
    return value.get("paused", False) and value.get("run_id") == run_id


def set_pause(repo, run_id, paused):
    # Caller holds operator_lock, shared with new-run archival.
    require_run(repo, run_id)
    atomic_json(state_path(repo) / "pause.json", dict(paused=paused, run_id=run_id, updated_at=stamp()))


def request_view(repo):
    return request_view_at(state_path(repo))


def request_view_at(directory):
    """Read requests from a trusted current or archived run directory."""
    value = request_state(directory)
    receipts = local_json(directory / "board-receipts.json")
    result = []
    fields = ("id", "type", "created_at", "title", "text", "area", "priority",
              "task_key", "task_title", "decision", "run_id", "expected_status")
    for row in value["requests"]:
        receipt = receipts.get(row["id"], {})
        if not isinstance(receipt, dict):
            raise StateError("invalid board receipt; saved responses were preserved")
        status = receipt.get("status")
        if receipt and (status not in ("applied", "declined") or not isinstance(receipt.get("note"), str)):
            raise StateError("invalid board receipt; saved responses were preserved")
        result.append({**{key: row[key] for key in fields if key in row},
                       "status": status if status in ("applied", "declined") else "pending",
                       "response": receipt.get("note", ""), "responded_at": receipt.get("at")})
    return result


def text_field(data, field, limit, required=True):
    value = data.get(field, "")
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise StateError(f"{field} must contain {'1' if required else '0'}–{limit} characters")
    return value.strip()


def prepare_request(data, tasks, value):
    """Validate against an in-memory ledger before committing a whole submission."""
    try:
        request_id = str(uuid.UUID(data.get("id", "")))
    except (ValueError, TypeError, AttributeError) as exc:
        raise StateError("a valid request id is required") from exc
    kind = data.get("type")
    if kind not in REQUEST_TYPES:
        raise StateError("unknown request type")
    request = dict(id=request_id, type=kind, run_id=data.get("run_id"),
                   text=text_field(data, "text", 8000, required=kind in ("guidance", "note")))
    if kind == "task":
        request["title"] = text_field(data, "title", 200)
        request["area"] = data.get("area", "general")
        if request["area"] not in AREAS:
            raise StateError("unknown task area")
    if kind in ("task", "priority"):
        request["priority"] = data.get("priority", "normal")
        if request["priority"] not in ("high", "normal", "low"):
            raise StateError("unknown priority")
    if kind in ("priority", "decision", "note"):
        key = text_field(data, "task_key", 500)
        # Idempotent retries still work after a worker settles the target.
        request["task_key"] = key
        if kind == "decision":
            request["decision"] = data.get("decision")
            if request["decision"] not in ("approve", "reject"):
                raise StateError("decision must be approve or reject")
    previous = next((row for row in value["requests"] if row["id"] == request_id), None)
    if previous:
        if any(previous.get(key) != item for key, item in request.items()):
            raise StateError("request id was already used for a different request")
        return previous
    if len(value["requests"]) >= 1000:
        raise StateError("This run has reached 1,000 board requests. Start a new run to archive them.")
    if kind in ("priority", "decision", "note"):
        task = next((item for item in tasks if item["key"] == request["task_key"]), None)
        if not task or task.get("run_id") != "current":
            raise StateError("Only tasks in the current run can receive changes.")
        if kind != "note" and task["status"] in ("done", "rejected"):
            raise StateError("This task is already finished. Add a new task for follow-up work.")
        if kind == "decision" and task["status"] != "proposed":
            raise StateError("This task is no longer awaiting a proposal decision.")
        request.update(task_title=task["title"], expected_status=task["status"])
    request["created_at"] = stamp()
    value["requests"].append(request)
    return request


def queue_submission(repo, data, tasks, batch=False):
    """Save a submission atomically. Caller holds the lock shared with archival."""
    require_run(repo, data.get("run_id"))
    value = operator_state(repo)
    if batch:
        items = data.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= 50 or any(not isinstance(item, dict) for item in items):
            raise StateError("Select 1–50 tasks for a priority change.")
        payloads = [dict(id=item.get("id"), task_key=item.get("task_key"), type="priority",
                         run_id=data.get("run_id"), priority=data.get("priority"), text=data.get("text", "")) for item in items]
    else:
        payloads = [data]
    result, seen, targets = [], set(), set()
    for payload in payloads:
        request = prepare_request(payload, tasks, value)
        if request["id"] in seen or batch and request["task_key"] in targets:
            raise StateError("Each selected task and request id must be unique.")
        seen.add(request["id"])
        if batch:
            targets.add(request["task_key"])
        result.append(request)
    if len(json.dumps(value).encode()) > MAX_BYTES:
        raise StateError("The board request ledger is full. Saved requests were preserved.")
    atomic_json(state_path(repo) / "operator.json", value)
    return result


def queue_request(repo, data, tasks):
    return queue_submission(repo, data, tasks)[0]


def acknowledge(repo, request_id, status, note):
    if status not in ("applied", "declined") or not isinstance(note, str) or not 1 <= len(note.strip()) <= 8000:
        raise StateError("acknowledgment requires applied/declined and a note of 1–8,000 characters")
    with operator_lock(repo) as state:
        row = next((row for row in operator_state(repo)["requests"] if row["id"] == request_id), None)
        if row is None:
            raise StateError("unknown request; it may belong to an archived run")
        receipts = local_json(state / "board-receipts.json")
        previous = receipts.get(request_id)
        if previous:
            if isinstance(previous, dict) and previous.get("status") == status and previous.get("note") == note.strip():
                return previous
            raise StateError("request was already acknowledged; preserve its recorded response")
        receipt = dict(status=status, note=note.strip(), at=stamp())
        receipts[request_id] = receipt
        if len(json.dumps(receipts).encode()) > MAX_BYTES:
            raise StateError("The receipt ledger is full. Saved responses were preserved.")
        atomic_json(state / "board-receipts.json", receipts)
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("pending", help="read pending requests and cooperative control flags")
    ack = sub.add_parser("ack", help="acknowledge an applied or declined user request")
    ack.add_argument("--id", required=True)
    ack.add_argument("--status", choices=("applied", "declined"), required=True)
    ack.add_argument("--note", required=True)
    args = parser.parse_args(argv)
    repo = Path(args.repo).expanduser().resolve()
    try:
        if args.action == "ack":
            result = acknowledge(repo, args.id, args.status, args.note)
        else:
            # New-run archival uses this same lock. Never combine an old run's
            # identity with the replacement run's requests or control flags.
            with operator_lock(repo) as state:
                identity = run_identity(repo)
                result = dict(run_id=identity, pause_requested=pause_requested(repo, identity),
                              stop_requested=(state / "stop.request").exists(),
                              requests=[row for row in request_view(repo) if row["status"] == "pending"])
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (StateError, OSError) as exc:
        print(f"improve-control: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
