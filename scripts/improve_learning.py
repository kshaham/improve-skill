"""Cross-run learning: what worked, what blocked, and lessons worth keeping. Standard library only.

Two stores, both plain JSON:
  .improve/learning.json            per repository; survives --new-run archival
  $IMPROVE_LEARNING_HOME/learning.json  (default ~/.config/improve) lessons that apply to any repo
"""

import argparse
import json
import os
from pathlib import Path
import re
import sys

from improve_runtime import StateError, atomic_json, read_json, stamp
import improve_toon as toon

MAX_RUNS = 60
MAX_LESSONS = 200
MAX_BLOCKERS = 60
MAX_LESSON_CHARS = 400
GUIDANCE_LESSONS = 10
MIN_AREA_SAMPLES = 3
SCOPES = ("repo", "global")


def global_home():
    return Path(os.environ.get("IMPROVE_LEARNING_HOME", Path.home() / ".config" / "improve")).expanduser()


def store_path(repo, scope="repo"):
    if scope == "global":
        return global_home() / "learning.json"
    return Path(repo) / ".improve" / "learning.json"


def load(path, repair=True):
    empty = {"version": 1, "runs": [], "areas": {}, "blockers": [], "lessons": []}
    if not path.exists():
        return empty
    try:
        value = read_json(path)
    except (StateError, OSError, ValueError):
        # A damaged store must never stop a run; keep a copy and start fresh.
        # Read-only callers (the board) leave the file untouched.
        if repair:
            path.replace(path.with_suffix(".damaged.json"))
        return empty
    if not isinstance(value, dict):
        return empty
    for key in ("runs", "blockers", "lessons"):
        if not isinstance(value.get(key), list):
            value[key] = []
    if not isinstance(value.get("areas"), dict):
        value["areas"] = {}
    return value


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, value)


def normalized(text):
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", text.lower())).strip()


def add_lesson(repo, text, area=None, scope="repo", run_id=None):
    """Store a lesson; repeating one reinforces it instead of duplicating it."""
    if scope not in SCOPES:
        raise StateError("scope must be repo or global")
    text = re.sub(r"\s+", " ", str(text)).strip()
    if not 8 <= len(text) <= MAX_LESSON_CHARS:
        raise StateError(f"a lesson needs 8-{MAX_LESSON_CHARS} characters")
    path = store_path(repo, scope)
    value = load(path)
    key = normalized(text)
    existing = next((row for row in value["lessons"] if row.get("key") == key), None)
    if existing:
        existing.update(reinforced=int(existing.get("reinforced", 1)) + 1, last_at=stamp())
        lesson = existing
    else:
        lesson = dict(key=key, text=text, area=area, run_id=run_id, reinforced=1, at=stamp(), last_at=stamp())
        value["lessons"].append(lesson)
    value["lessons"] = sorted(value["lessons"], key=lesson_rank, reverse=True)[:MAX_LESSONS]
    save(path, value)
    return lesson


def lesson_rank(lesson):
    return (int(lesson.get("reinforced", 1)), str(lesson.get("last_at", "")))


def blocker_key(reason):
    # Collapse run-specific detail (SHAs, numbers, paths) so recurring blockers group together.
    reason = re.sub(r"[0-9a-f]{7,40}|\d+|refs/\S+|/\S+", "#", str(reason).lower())
    return normalized(reason)[:120]


def summarize_tasks(rows):
    tasks = {}
    for row in rows:
        task_id = row.get("id")
        if isinstance(task_id, str):
            tasks[task_id] = {**tasks.get(task_id, {}), **row}
    areas, blockers = {}, []
    for task in tasks.values():
        area = task.get("area") if isinstance(task.get("area"), str) else "general"
        status = task.get("status", "ready")
        bucket = areas.setdefault(area, {"done": 0, "rejected": 0, "blocked": 0, "open": 0})
        bucket[status if status in ("done", "rejected", "blocked") else "open"] += 1
        if status == "blocked":
            blockers.append(str(task.get("blocked_reason") or task.get("note") or "unspecified"))
    return areas, blockers


def record_run(repo, control, rows):
    """Fold one finished run into the repository's learning store. Idempotent per run_id."""
    path = store_path(repo)
    value = load(path)
    run_id = control.get("run_id") or control.get("started_at")
    if any(run.get("run_id") == run_id for run in value["runs"]):
        return value
    areas, blockers = summarize_tasks(rows)
    for area, counts in areas.items():
        total = value["areas"].setdefault(area, {"done": 0, "rejected": 0, "blocked": 0, "open": 0, "runs": 0})
        for key, count in counts.items():
            total[key] = total.get(key, 0) + count
        total["runs"] = total.get("runs", 0) + 1
    for reason in blockers:
        key = blocker_key(reason)
        row = next((item for item in value["blockers"] if item.get("key") == key), None)
        if row:
            row.update(count=row.get("count", 1) + 1, last_at=stamp(), example=reason[:200])
        else:
            value["blockers"].append(dict(key=key, count=1, example=reason[:200], last_at=stamp()))
    value["blockers"] = sorted(value["blockers"], key=lambda row: row.get("count", 0), reverse=True)[:MAX_BLOCKERS]
    value["runs"].append(dict(
        run_id=run_id, started_at=control.get("started_at"), ended_at=control.get("ended_at") or stamp(),
        outcome=control.get("outcome"), engine=control.get("engine"),
        recoveries=control.get("recoveries_total", 0), quarantines=control.get("quarantines", 0),
        done=sum(area["done"] for area in areas.values()),
        rejected=sum(area["rejected"] for area in areas.values()),
        blocked=sum(area["blocked"] for area in areas.values())))
    value["runs"] = value["runs"][-MAX_RUNS:]
    save(path, value)
    return value


def area_yield(counts):
    attempts = counts.get("done", 0) + counts.get("rejected", 0) + counts.get("blocked", 0)
    return (counts.get("done", 0) / attempts if attempts else 0.0), attempts


def guidance(repo, max_chars=1800):
    """Compact advice for the next cycle prompt, built from past runs."""
    local, shared = load(store_path(repo)), load(store_path(repo, "global"))
    parts = []
    ranked = sorted(((area, *area_yield(counts)) for area, counts in local["areas"].items()),
                    key=lambda item: item[1], reverse=True)
    proven = [f"{area} ({rate:.0%} of {attempts})" for area, rate, attempts in ranked if attempts >= MIN_AREA_SAMPLES]
    if proven:
        parts.append("Past verified-success rate by area (use to break ties inside the user's focus): " + ", ".join(proven[:6]))
    recurring = [row for row in local["blockers"] if row.get("count", 0) >= 2][:4]
    if recurring:
        parts.append("Recurring blockers from earlier runs; check for these early and route around them:\n"
                     + "\n".join(f"- ({row['count']}x) {row['example']}" for row in recurring))
    lessons = (sorted(local["lessons"], key=lesson_rank, reverse=True)[:GUIDANCE_LESSONS]
               + sorted(shared["lessons"], key=lesson_rank, reverse=True)[:GUIDANCE_LESSONS // 2])
    if lessons:
        parts.append("Lessons learned in earlier runs (follow unless the code has changed):\n"
                     + "\n".join(f"- {row['text']}" for row in lessons))
    text = "\n\n".join(parts)
    return text if len(text) <= max_chars else text[:max_chars].rsplit("\n", 1)[0]


def board_summary(repo):
    """Read-only view for the board's overview."""
    value = load(store_path(repo), repair=False)
    areas = []
    for area, counts in value["areas"].items():
        rate, attempts = area_yield(counts)
        areas.append(dict(area=area, rate=round(rate, 3), attempts=attempts, done=counts.get("done", 0)))
    areas.sort(key=lambda row: (row["attempts"] >= MIN_AREA_SAMPLES, row["rate"]), reverse=True)
    return dict(runs=len(value["runs"]), recent_runs=value["runs"][-5:], areas=areas[:14],
                blockers=value["blockers"][:5],
                lessons=[{k: row.get(k) for k in ("text", "area", "reinforced", "last_at")}
                         for row in sorted(value["lessons"], key=lesson_rank, reverse=True)[:8]])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--format", choices=("toon", "json"), default=os.environ.get("IMPROVE_FORMAT", "toon"),
                        help="output format for `lesson` (default toon; IMPROVE_FORMAT overrides)")
    sub = parser.add_subparsers(dest="action", required=True)
    lesson = sub.add_parser("lesson", help="record a reusable lesson")
    lesson.add_argument("--text", required=True)
    lesson.add_argument("--area")
    lesson.add_argument("--scope", choices=SCOPES, default="repo",
                        help="global only for lessons true of any project (tool quirks, process)")
    sub.add_parser("guidance", help="print the advice injected into cycle prompts")
    sub.add_parser("summary", help="print the board summary as JSON")
    args = parser.parse_args(argv)
    repo = Path(args.repo).expanduser().resolve()
    try:
        if args.action == "lesson":
            run = repo / ".improve" / "supervisor.json"
            run_id = read_json(run).get("run_id") if run.exists() else None
            saved = add_lesson(repo, args.text, args.area, args.scope, run_id)
            print(json.dumps(saved) if args.format == "json" else toon.dumps(saved))
        elif args.action == "guidance":
            print(guidance(repo) or "No learning recorded yet.")
        else:
            print(json.dumps(board_summary(repo)))
        return 0
    except (StateError, OSError) as exc:
        print(f"improve-learning: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
