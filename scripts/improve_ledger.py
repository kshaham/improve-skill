"""Token-efficient views of the run ledgers for the agent, and canonical writes to them.

Reading `.improve/backlog.jsonl` raw repeats every key on every row and carries every
evidence string into the model's context. These commands print only what a decision needs,
as TOON tables (one header, one row per task), with long text bounded unless `--full` is
given. Writes rewrite the ledger atomically with one row per id, the way SKILL.md asks
the agent to keep it. Standard library only.

    improve_ledger.py --repo R queue                 ready work in rank order
    improve_ledger.py --repo R tasks --status blocked --area ui
    improve_ledger.py --repo R show q-0007
    improve_ledger.py --repo R counts
    improve_ledger.py --repo R discovery --limit 5
    improve_ledger.py --repo R run
    improve_ledger.py --repo R set q-0007 --status done --commit a1b2c3d
    improve_ledger.py --repo R add --json '{"id":"q-0008","area":"quality",...}'
"""

import argparse
import json
import os
from pathlib import Path
import re
import sys

from improve_resilience import LEDGERS, merged_tasks, read_rows, write_rows
from improve_runtime import StateError, atomic_json, read_json, stamp
import improve_toon as toon

STATUSES = ("ready", "in_progress", "done", "rejected", "blocked", "proposed")
PRIORITIES = ("high", "normal", "low")
# Compact columns for list views, in display order; `--fields` overrides.
QUEUE_FIELDS = ("id", "area", "user_priority", "focus_priority", "severity", "confidence", "blast", "file", "line", "title", "claim")
TASK_FIELDS = ("id", "status", "area", "user_priority", "file", "line", "title", "claim", "commit", "note", "blocked_reason", "updated_at")
DISCOVERY_FIELDS = ("at", "generation", "tier", "lane", "status", "accepted", "rejected", "paths", "hypothesis", "next_scope")
RUN_FIELDS = ("phase", "cycle", "tier", "branch", "baseline_commit", "started_at", "deadline", "last_report_hour", "next_action",
              "active_item", "dry_sweep", "benched_areas", "lanes", "counts", "outcome", "summary_pending")
DEFAULT_WIDTH = 100
DEFAULT_LIMIT = 50
TEXT_FIELDS = ("note", "evidence", "failure", "verification", "counter", "claim", "title", "acceptance", "blocked_reason")


def state_path(repo):
    state = Path(repo) / ".improve"
    if state.is_symlink():
        raise StateError(".improve must be a directory inside the repository")
    return state


def priority_rank(task):
    """SKILL.md ranking: user priority, focus priority, then severity x confidence x blast."""
    user = {"high": 0, "normal": 1, "low": 2}.get(task.get("user_priority"), 1)
    focus = task.get("focus_priority")
    focus = focus if isinstance(focus, (int, float)) and not isinstance(focus, bool) else 999
    product = 1.0
    for key in ("severity", "confidence", "blast"):
        value = task.get(key)
        product *= value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
    return (user, focus, -product, str(task.get("id", "")))


def matches(task, args):
    if args.status and task.get("status", "ready") not in args.status:
        return False
    if args.area and task.get("area", "general") != args.area:
        return False
    if args.id and not str(task.get("id", "")).startswith(args.id):
        return False
    if args.file:
        paths = [task.get("file")] + list(task.get("paths") or []) + list(task.get("owned_paths") or [])
        if not any(isinstance(path, str) and (path == args.file or path.startswith(args.file.rstrip("/") + "/")) for path in paths):
            return False
    return True


def project(rows, fields, full, width):
    """Rows reduced to `fields` (all fields with `full`), text bounded unless `full`."""
    if full:
        return toon.uniform([dict(row) for row in rows])
    view = toon.uniform([{key: row.get(key) for key in fields if key in row or key in ("id", "status")} for row in rows], keys=list(fields))
    return toon.truncate(view, width)


def render(value, fmt, delimiter):
    if fmt == "json":
        return json.dumps(value, indent=2, ensure_ascii=False)
    return toon.dumps(value, delimiter=delimiter)


def validate_update(changes):
    if "status" in changes and changes["status"] not in STATUSES:
        raise StateError(f"status must be one of {', '.join(STATUSES)}")
    if "user_priority" in changes and changes["user_priority"] not in PRIORITIES:
        raise StateError(f"user_priority must be one of {', '.join(PRIORITIES)}")
    if "commit" in changes and changes["commit"] is not None and not re.fullmatch(r"[0-9a-fA-F]{7,40}", str(changes["commit"])):
        raise StateError("commit must be a 7-40 character hex SHA")
    for key in ("severity", "blast"):
        if key in changes and not (isinstance(changes[key], int) and 1 <= changes[key] <= 5):
            raise StateError(f"{key} must be an integer from 1 to 5")
    if "confidence" in changes and not (isinstance(changes["confidence"], (int, float)) and 0 <= changes["confidence"] <= 1):
        raise StateError("confidence must be between 0 and 1")


def parse_fields(pairs, as_json=False):
    changes = {}
    for pair in pairs or ():
        key, sep, value = pair.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise StateError(f"expected key=value, got {pair!r}")
        if as_json:
            try:
                changes[key] = json.loads(value)
            except ValueError as exc:
                raise StateError(f"{key}: invalid JSON value") from exc
        else:
            changes[key] = value
    return changes


def update_task(state, task_id, changes):
    """Merge `changes` into the task and rewrite the ledger with one canonical row per id."""
    path = state / "backlog.jsonl"
    rows = read_rows(path)
    tasks = {}
    order = []
    for row in rows:
        key = row.get("id")
        if not isinstance(key, str) or not key:
            continue
        if key not in tasks:
            order.append(key)
        tasks[key] = {**tasks.get(key, {}), **row}
    if task_id not in tasks:
        raise StateError(f"no task {task_id!r} in backlog.jsonl")
    task = dict(tasks[task_id])
    now = stamp()
    status = changes.get("status")
    if status == "in_progress" and not task.get("started_at"):
        changes.setdefault("started_at", now)
    if status in ("done", "rejected") and not changes.get("completed_at"):
        changes.setdefault("completed_at", now)
    for key, value in changes.items():
        if value is None and key in task:
            task[key] = None
        else:
            task[key] = value
    task["updated_at"] = now
    tasks[task_id] = task
    write_rows(path, [tasks[key] for key in order])
    return task


def add_task(state, row):
    if not isinstance(row, dict):
        raise StateError("a task must be a JSON object")
    task_id = row.get("id")
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,80}", task_id):
        raise StateError("a task needs an id of letters, digits, '.', '_', ':' or '-'")
    tasks = merged_tasks(state)
    if task_id in tasks:
        raise StateError(f"task {task_id!r} already exists; use `set` to change it")
    row.setdefault("status", "ready")
    row.setdefault("created_at", stamp())
    row["updated_at"] = stamp()
    validate_update(row)
    path = state / "backlog.jsonl"
    rows = read_rows(path)
    write_rows(path, rows + [row])
    return row


def run_view(state):
    path = state / "run.json"
    if not path.is_file() or path.is_symlink():
        return {}
    run = read_json(path)
    view = {key: run[key] for key in RUN_FIELDS if key in run}
    if isinstance(run.get("scanned_paths"), list):
        view["scanned_paths_count"] = len(run["scanned_paths"])
    if isinstance(run.get("focus"), dict):
        focus = run["focus"]
        view["focus"] = {key: focus[key] for key in ("priorities", "excluded", "board_areas") if key in focus}
    return view


def counts_view(tasks):
    by_area = {}
    for task in tasks.values():
        area = task.get("area") if isinstance(task.get("area"), str) else "general"
        status = task.get("status", "ready")
        bucket = by_area.setdefault(area, {status: 0 for status in STATUSES})
        bucket[status if status in STATUSES else "blocked"] += 1
    rows = [{"area": area, **counts, "total": sum(counts.values())} for area, counts in sorted(by_area.items())]
    totals = {"area": "all", **{status: sum(row[status] for row in rows) for status in STATUSES}}
    totals["total"] = sum(totals[status] for status in STATUSES)
    return {"areas": rows, "totals": totals}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--format", choices=("toon", "json"), default=os.environ.get("IMPROVE_FORMAT", "toon"),
                        help="output format (default toon; IMPROVE_FORMAT overrides)")
    parser.add_argument("--delimiter", choices=("comma", "tab", "pipe"), default="comma")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH, help="bound text cells in list views (0 for none)")
    parser.add_argument("--full", action="store_true", help="every field, untruncated")
    sub = parser.add_subparsers(dest="action", required=True)
    for name, text in (("queue", "ready tasks in rank order"), ("tasks", "tasks matching the filters")):
        command = sub.add_parser(name, help=text)
        command.add_argument("--status", action="append", choices=STATUSES)
        command.add_argument("--area")
        command.add_argument("--file", help="a file or directory a task touches")
        command.add_argument("--id", help="id prefix")
        command.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
        command.add_argument("--fields", help="comma-separated columns")
    show = sub.add_parser("show", help="complete rows for these ids")
    show.add_argument("ids", nargs="+")
    sub.add_parser("counts", help="task counts by area and status")
    disc = sub.add_parser("discovery", help="latest discovery passes")
    disc.add_argument("--limit", type=int, default=10)
    sub.add_parser("run", help="the run's current state without the bulky fields")
    bets = sub.add_parser("bets", help="improve-max bets")
    bets.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    setter = sub.add_parser("set", help="change a task and rewrite the ledger canonically")
    setter.add_argument("id")
    setter.add_argument("--status", choices=STATUSES)
    setter.add_argument("--commit")
    setter.add_argument("--note")
    setter.add_argument("--evidence")
    setter.add_argument("--verification")
    setter.add_argument("--counter")
    setter.add_argument("--blocked-reason", dest="blocked_reason")
    setter.add_argument("--user-priority", dest="user_priority", choices=PRIORITIES)
    setter.add_argument("--graph-ref", action="append", dest="graph_refs", help="insight id the task relied on; repeatable")
    setter.add_argument("--field", action="append", help="key=value text field; repeatable")
    setter.add_argument("--json-field", action="append", help="key=JSON for numbers, lists, or objects; repeatable")
    adder = sub.add_parser("add", help="append a new task")
    adder.add_argument("--json", required=True, help="the task row as a JSON object")
    args = parser.parse_args(argv)
    repo = Path(args.repo).expanduser().resolve()
    try:
        state = state_path(repo)
        tasks = merged_tasks(state) if args.action not in ("run", "discovery") else {}
        if args.action in ("queue", "tasks"):
            if args.action == "queue":
                args.status = args.status or ["ready"]
            chosen = [task for task in tasks.values() if matches(task, args)]
            chosen.sort(key=priority_rank)
            total = len(chosen)
            chosen = chosen[:max(0, args.limit)] if args.limit else chosen
            fields = tuple(part.strip() for part in args.fields.split(",") if part.strip()) if args.fields else (
                QUEUE_FIELDS if args.action == "queue" else TASK_FIELDS)
            result = {"total": total, "shown": len(chosen), "tasks": project(chosen, fields, args.full, args.width)}
        elif args.action == "show":
            missing = [task_id for task_id in args.ids if task_id not in tasks]
            if missing:
                raise StateError(f"no task {missing[0]!r} in backlog.jsonl")
            rows = [tasks[task_id] for task_id in args.ids]
            result = rows[0] if len(rows) == 1 else toon.uniform([dict(row) for row in rows])
        elif args.action == "counts":
            result = counts_view(tasks)
        elif args.action == "discovery":
            rows = read_rows(state / "discovery.jsonl")[-max(0, args.limit):] if args.limit else read_rows(state / "discovery.jsonl")
            result = {"total": len(read_rows(state / "discovery.jsonl")), "passes": project(rows, DISCOVERY_FIELDS, args.full, args.width)}
        elif args.action == "run":
            result = run_view(state)
        elif args.action == "bets":
            rows = []
            for row in read_rows(state / "bets.jsonl"):
                rows.append(row)
            merged = {}
            for row in rows:
                key = row.get("id")
                if isinstance(key, str):
                    merged[key] = {**merged.get(key, {}), **row}
            chosen = list(merged.values())[:max(0, args.limit)] if args.limit else list(merged.values())
            result = {"total": len(merged), "bets": project(chosen, ("id", "status", "kind", "journey", "target", "claimed_gain", "hypothesis"),
                                                             args.full, args.width)}
        elif args.action == "set":
            changes = {}
            for key in ("status", "commit", "note", "evidence", "verification", "counter", "blocked_reason", "user_priority"):
                if getattr(args, key) is not None:
                    changes[key] = getattr(args, key)
            if args.graph_refs:
                changes["graph_refs"] = list(dict.fromkeys(args.graph_refs))
            changes.update(parse_fields(args.field))
            changes.update(parse_fields(args.json_field, as_json=True))
            if not changes:
                raise StateError("set needs at least one change")
            validate_update(changes)
            result = update_task(state, args.id, changes)
        else:
            try:
                row = json.loads(args.json)
            except ValueError as exc:
                raise StateError("--json must be a JSON object") from exc
            result = add_task(state, row)
        print(render(result, args.format, args.delimiter))
        return 0
    except (StateError, OSError, ValueError) as exc:
        print(f"improve-ledger: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
