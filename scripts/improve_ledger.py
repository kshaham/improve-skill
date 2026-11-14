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

from improve_resilience import ledger_lock, merged_tasks, read_rows, rewrite_tasks
from improve_runtime import StateError, atomic_json, read_json, stamp
import improve_toon as toon

STATUSES = ("ready", "in_progress", "done", "rejected", "blocked", "proposed")
PRIORITIES = ("high", "normal", "low")
# Compact columns for list views, in display order; `--fields` overrides.
QUEUE_FIELDS = ("id", "area", "user_priority", "focus_priority", "severity", "confidence", "blast", "file", "line", "title", "claim")
TASK_FIELDS = ("id", "status", "area", "user_priority", "file", "line", "title", "claim", "commit", "note", "blocked_reason", "updated_at")
DISCOVERY_FIELDS = ("at", "generation", "tier", "lane", "status", "accepted", "rejected", "paths", "hypothesis", "next_scope")
RUN_FIELDS = ("phase", "cycle", "tier", "branch", "baseline_commit", "started_at", "deadline", "last_report_hour", "next_action",
              "active_item", "dry_sweep", "benched_areas", "lanes", "counts", "outcome", "summary_pending",
              "preflight_complete", "gates", "gate_seconds", "authorization", "continuation")
# run.json keys `run-set` refuses: the supervisor owns the clock and the outcome. Every other
# key belongs to the agent (focus when it applies a board request, discovery cursors,
# improve-max's mode and target_stack, ...), so the docs never force a raw write.
RUN_PROTECTED = ("started_at", "deadline", "outcome")
# Ledger keys that identify a row; `set` must never rewrite them.
TASK_PROTECTED = ("id", "created_at")
DEFAULT_WIDTH = 100
DEFAULT_LIMIT = 50
TEXT_FIELDS = ("note", "evidence", "failure", "verification", "counter", "claim", "title", "acceptance", "blocked_reason")
# Never shortened in list views: a cut id, path, or SHA cannot be used.
KEEP_FIELDS = frozenset(("id", "file", "paths", "owned_paths", "commit", "baseline_commit", "branch", "graph_refs",
                         "active_item", "line"))
HINT_SECONDS = 3600


def state_path(repo):
    state = Path(repo) / ".improve"
    if state.is_symlink():
        raise StateError(".improve must be a directory inside the repository")
    return state


def number(value, default=0):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def critical_security(task):
    """A confirmed critical vulnerability preempts lower-risk work (SKILL.md section 2)."""
    return task.get("area") == "security" and number(task.get("severity")) >= 5


def priority_rank(task, board=None):
    """SKILL.md ranking: critical in-scope security first, then user priority, then board area
    focus (prefer before unmarked), then the focus priority, then severity x confidence x blast."""
    board = board or {}
    user = {"high": 0, "normal": 1, "low": 2}.get(task.get("user_priority"), 1)
    area = task.get("area") if isinstance(task.get("area"), str) else "general"
    preferred = 0 if area in (board.get("prefer") or []) else 1
    focus = number(task.get("focus_priority"), 999)
    product = 1.0
    for key in ("severity", "confidence", "blast"):
        product *= number(task.get(key))
    return (not critical_security(task), user, preferred, focus, -product, str(task.get("id", "")))


def board_focus(state):
    """`run.json.focus.board_areas` and intake exclusions, tolerant of a missing or odd run.json."""
    path = state / "run.json"
    try:
        run = read_json(path) if path.is_file() and not path.is_symlink() else {}
    except StateError:
        run = {}
    focus = run.get("focus") if isinstance(run.get("focus"), dict) else {}
    board = focus.get("board_areas") if isinstance(focus.get("board_areas"), dict) else {}
    lists = {key: [str(item) for item in board.get(key) or [] if isinstance(item, str)] for key in ("prefer", "skip")}
    lists["excluded"] = [str(item) for item in focus.get("excluded") or [] if isinstance(item, str)]
    return lists


def matches(task, args):
    if args.status and task.get("status", "ready") not in args.status:
        return False
    if args.area and task.get("area", "general") != args.area:
        return False
    if args.id and not str(task.get("id", "")).startswith(args.id):
        return False
    if args.file:
        from improve_graph import safe_path, task_files
        wanted = safe_path(args.file) or args.file.rstrip("/")
        if not any(path == wanted or path.startswith(wanted + "/") for path in task_files(task)):
            return False
    return True


def project(rows, fields, full, width):
    """Rows reduced to `fields` (all fields with `full`), text bounded unless `full`."""
    if full:
        return toon.uniform([dict(row) for row in rows])
    view = toon.uniform([{key: row.get(key) for key in fields if key in row or key in ("id", "status")} for row in rows], keys=list(fields))
    return toon.truncate(view, width, keep=KEEP_FIELDS)


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
        if key in TASK_PROTECTED:
            raise StateError(f"{key} identifies the row and cannot be changed with set")
        if as_json:
            try:
                changes[key] = json.loads(value)
            except ValueError as exc:
                raise StateError(f"{key}: invalid JSON value") from exc
        else:
            changes[key] = value
    return changes


def update_task(state, task_id, changes, append_refs=()):
    """Merge `changes` into the task and rewrite the ledger with one canonical row per id.
    `append_refs` are added to the task's `graph_refs` (citations accumulate over a task;
    replacing them would erase what the task relied on earlier). Damaged lines survive."""
    path = state / "backlog.jsonl"
    result = {}

    def change(tasks):
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
            task[key] = value
        if append_refs:
            existing = task.get("graph_refs") if isinstance(task.get("graph_refs"), list) else []
            task["graph_refs"] = list(dict.fromkeys([*existing, *append_refs]))[:20]
        task["updated_at"] = now
        tasks[task_id] = task
        result.update(task)

    with ledger_lock(state):
        rewrite_tasks(path, change)
    return result


def add_task(state, row):
    if not isinstance(row, dict):
        raise StateError("a task must be a JSON object")
    task_id = row.get("id")
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,80}", task_id):
        raise StateError("a task needs an id of letters, digits, '.', '_', ':' or '-'")
    row.setdefault("status", "ready")
    row.setdefault("created_at", stamp())
    row["updated_at"] = stamp()
    validate_update(row)
    path = state / "backlog.jsonl"

    def check(tasks):
        if task_id in tasks:
            raise StateError(f"task {task_id!r} already exists; use `set` to change it")

    with ledger_lock(state):
        rewrite_tasks(path, check, append=[row])
    return row


def context_hint(repo, task):
    """Advisory (Graphify's always-on nudge): starting a task whose files no recent `context`
    query covered. Never blocks anything."""
    import improve_graph_journal as journal
    from improve_graph import task_files
    files = task_files(task, repo)
    if not files:
        return None
    seen = journal.files_read_since(repo, HINT_SECONDS)
    covered = [path for path in files if path in seen or any(path.startswith(item.rstrip("/") + "/") for item in seen)]
    if covered:
        return None
    script = Path(__file__).with_name("improve_graph.py")
    return (f"no graph context was read for these files in the last hour; before editing run: python3 {script} "
            f"--repo {repo} context --files {' '.join(files[:6])}")


def run_view(state, full=False):
    """The run's state for a decision. Always includes what the instructions branch on
    (preflight_complete, gates and their cost, authorization, continuation); `--full` is the
    whole run.json."""
    path = state / "run.json"
    if not path.is_file() or path.is_symlink():
        return {}
    run = read_json(path)
    if full:
        return run
    view = {key: run[key] for key in RUN_FIELDS if key in run}
    if isinstance(run.get("scanned_paths"), list):
        view["scanned_paths_count"] = len(run["scanned_paths"])
    if isinstance(run.get("focus"), dict):
        focus = run["focus"]
        view["focus"] = {key: focus[key] for key in ("priorities", "excluded", "board_areas") if key in focus}
    return view


def set_run(state, changes):
    """Validated, atomic run.json update for every key except the supervisor's clock and outcome."""
    for key in changes:
        if key in RUN_PROTECTED:
            raise StateError(f"{key} belongs to the supervisor and cannot be set")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
            raise StateError(f"run.json keys are lower_snake_case, got {key!r}")
    if "preflight_complete" in changes and not isinstance(changes["preflight_complete"], bool):
        value = str(changes["preflight_complete"]).lower()
        if value not in ("true", "false"):
            raise StateError("preflight_complete must be true or false")
        changes["preflight_complete"] = value == "true"
    for key in ("cycle", "last_report_hour"):
        if key in changes:
            try:
                changes[key] = int(changes[key])
            except (TypeError, ValueError) as exc:
                raise StateError(f"{key} must be an integer") from exc
            if changes[key] < 0:
                raise StateError(f"{key} must not be negative")
    path = state / "run.json"
    if path.is_symlink():
        raise StateError("run.json must not be a symlink")
    with ledger_lock(state):
        run = read_json(path) if path.is_file() else {}
        run.update(changes)
        atomic_json(path, run)
    return {key: run[key] for key in changes}


def add_discovery(state, row):
    """Append one discovery checkpoint (validated JSON object) to discovery.jsonl."""
    if not isinstance(row, dict) or not row:
        raise StateError("a discovery row must be a non-empty JSON object")
    row.setdefault("at", stamp())
    path = state / "discovery.jsonl"
    if path.is_symlink():
        raise StateError("discovery.jsonl must not be a symlink")
    line = json.dumps(row, ensure_ascii=False)
    with ledger_lock(state):
        with open(path, "a") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return row


def parse_run_pairs(pairs):
    """KEY=VALUE pairs for run-set; `id`-style protection does not apply to run.json."""
    changes = {}
    for pair in pairs or ():
        key, sep, value = pair.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise StateError(f"expected KEY=VALUE, got {pair!r}")
        changes[key] = value
    return changes


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
    toon.output_options(parser, width=DEFAULT_WIDTH)
    shared = argparse.ArgumentParser(add_help=False)
    toon.output_options(shared, suppress=True)
    sub = parser.add_subparsers(dest="action", required=True)

    def command(name, **kwargs):
        return sub.add_parser(name, parents=[shared], **kwargs)

    for name, text in (("queue", "ready tasks in rank order"), ("tasks", "tasks matching the filters")):
        listing = command(name, help=text)
        listing.add_argument("--status", action="append", choices=STATUSES)
        listing.add_argument("--area")
        listing.add_argument("--file", help="a file or directory a task touches")
        listing.add_argument("--id", help="id prefix")
        listing.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
        listing.add_argument("--fields", help="comma-separated columns")
        listing.add_argument("--all-areas", action="store_true", help="include areas the board marked skip")
    show = command("show", help="complete rows for these ids")
    show.add_argument("ids", nargs="+")
    command("counts", help="task counts by area and status")
    disc = command("discovery", help="latest discovery passes")
    disc.add_argument("--limit", type=int, default=10)
    command("run", help="the run's current state (gates, authorization, preflight); --full for all of run.json")
    run_setter = command("run-set", help="set run.json keys the agent owns (atomic, validated)")
    run_setter.add_argument("pairs", nargs="*", help="KEY=VALUE text values")
    run_setter.add_argument("--json-field", action="append", help="KEY=JSON for numbers, lists, objects, booleans")
    discovery_adder = command("discovery-add", help="append one discovery checkpoint")
    discovery_adder.add_argument("--json", required=True, help="the checkpoint as a JSON object")
    bets = command("bets", help="improve-max bets")
    bets.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    setter = command("set", help="change a task and rewrite the ledger canonically")
    setter.add_argument("id")
    setter.add_argument("--status", choices=STATUSES)
    setter.add_argument("--commit")
    setter.add_argument("--note")
    setter.add_argument("--evidence")
    setter.add_argument("--verification")
    setter.add_argument("--counter")
    setter.add_argument("--blocked-reason", dest="blocked_reason")
    setter.add_argument("--user-priority", dest="user_priority", choices=PRIORITIES)
    setter.add_argument("--graph-ref", action="append", dest="graph_refs",
                        help="insight id the task relied on; repeatable; added to the ones already recorded")
    setter.add_argument("--clear-graph-refs", action="store_true", help="replace the recorded graph_refs instead of adding")
    setter.add_argument("--field", action="append", help="key=value text field; repeatable")
    setter.add_argument("--json-field", action="append", help="key=JSON for numbers, lists, or objects; repeatable")
    adder = command("add", help="append a new task")
    adder.add_argument("--json", required=True, help="the task row as a JSON object")
    args, extra = parser.parse_known_args(argv)
    if extra:
        # argparse stops taking positionals after an option; `run-set --json-field k=v a=b` is
        # still one list of pairs, in whatever order the agent wrote it.
        if args.action == "run-set" and all("=" in token and not token.startswith("-") for token in extra):
            args.pairs = list(args.pairs or []) + extra
        else:
            parser.error(f"unrecognized arguments: {' '.join(extra)}")
    toon.resolve_format(args)
    if args.width is None:
        args.width = DEFAULT_WIDTH
    repo = Path(args.repo).expanduser().resolve()
    try:
        state = state_path(repo)
        tasks = merged_tasks(state) if args.action not in ("run", "discovery", "run-set", "discovery-add") else {}
        skipped = 0
        if args.action in ("queue", "tasks"):
            board = board_focus(state)
            if args.action == "queue":
                args.status = args.status or ["ready"]
            chosen = [task for task in tasks.values() if matches(task, args)]
            if args.action == "queue" and not args.all_areas:
                # Never start new work in a skip area or an area excluded at intake; leave it
                # queued (a critical security finding still preempts).
                hidden = set(board["skip"]) | set(board["excluded"])
                kept = [task for task in chosen if task.get("area") not in hidden or critical_security(task)]
                skipped, chosen = len(chosen) - len(kept), kept
            chosen.sort(key=lambda task: priority_rank(task, board))
            total = len(chosen)
            chosen = chosen[:max(0, args.limit)] if args.limit else chosen
            fields = tuple(part.strip() for part in args.fields.split(",") if part.strip()) if args.fields else (
                QUEUE_FIELDS if args.action == "queue" else TASK_FIELDS)
            result = {"total": total, "shown": len(chosen), "tasks": project(chosen, fields, args.full, args.width)}
            if skipped:
                result["skipped_areas"] = skipped  # Queued in skip or excluded areas; --all-areas shows them.
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
            result = run_view(state, full=args.full)
        elif args.action == "run-set":
            changes = parse_run_pairs(args.pairs)
            changes.update(parse_fields(args.json_field, as_json=True))
            if not changes:
                raise StateError("run-set needs at least one KEY=VALUE")
            result = set_run(state, changes)
        elif args.action == "discovery-add":
            try:
                row = json.loads(args.json)
            except ValueError as exc:
                raise StateError("--json must be a JSON object") from exc
            result = add_discovery(state, row)
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
            refs = list(dict.fromkeys(args.graph_refs or []))
            if args.clear_graph_refs:
                changes["graph_refs"] = refs
                refs = []
            changes.update(parse_fields(args.field))
            changes.update(parse_fields(args.json_field, as_json=True))
            if not changes and not refs:
                raise StateError("set needs at least one change")
            validate_update(changes)
            result = update_task(state, args.id, changes, append_refs=refs)
            if changes.get("status") == "in_progress":
                hint = context_hint(repo, result)
                if hint:
                    result = {**result, "hint": hint}
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
