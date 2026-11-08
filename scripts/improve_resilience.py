"""Supervisor-side recovery helpers: keep a run working around failures. Standard library only.

Runs only between cycles, when no child agent can be writing the ledgers.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile

from improve_runtime import StateError, atomic_json, git, read_json, stamp

# Lanes the supervisor rotates through when the queue is empty. Board focus and
# intake scope still filter them; this only suggests where to look next.
DISCOVERY_LANES = (
    "ui", "features", "accessibility", "performance", "security", "resilience",
    "quality", "coverage", "concurrency", "contracts", "docs", "assets", "gate-speed",
)
MAX_REASON = 500


def read_rows(path):
    """Read a JSONL ledger, skipping damaged lines rather than failing recovery."""
    if not path.exists() or path.is_symlink():
        return []
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def merged_tasks(state):
    tasks = {}
    for row in read_rows(state / "backlog.jsonl"):
        task_id = row.get("id")
        if isinstance(task_id, str) and task_id:
            tasks[task_id] = {**tasks.get(task_id, {}), **row}
    return tasks


def ready_count(state):
    return sum(task.get("status", "ready") == "ready" for task in merged_tasks(state).values())


def quarantine_changes(repo, label):
    """Save every uncommitted change to a private ref, then restore a clean tree.

    Nothing is lost: `git checkout <ref> -- .` or `git stash apply`-style recovery
    brings the work back. `.improve/` is never touched.
    """
    head = git(repo, "rev-parse", "HEAD")
    fd, index = tempfile.mkstemp(prefix="improve-index-", dir=repo / ".improve")
    os.close(fd)
    env = {**os.environ, "GIT_INDEX_FILE": index}
    try:
        subprocess.run(["git", "-C", str(repo), "read-tree", "HEAD"], env=env, check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "add", "-A", "--", ".", ":(exclude).improve"],
                       env=env, check=True, capture_output=True)
        tree = subprocess.run(["git", "-C", str(repo), "write-tree"], env=env, check=True,
                              capture_output=True, text=True).stdout.strip()
        commit = subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=improve-supervisor", "-c", "user.email=improve@localhost",
             "commit-tree", tree, "-p", head, "-m", f"improve: quarantined uncommitted work ({label})"],
            check=True, capture_output=True, text=True).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise StateError(f"could not save uncommitted work: {exc.stderr.strip()[:200]}") from exc
    finally:
        Path(index).unlink(missing_ok=True)
    ref = f"refs/improve/quarantine/{stamp().replace(':', '-')}-{label}"
    git(repo, "update-ref", ref, commit)
    # Only restore once the work is safely referenced.
    git(repo, "reset", "-q", "--hard", "HEAD")
    git(repo, "clean", "-fdq", "-e", ".improve")
    return ref


def block_active_item(state, reason, source="supervisor"):
    """Mark the in-flight task blocked with a reason and clear it so work can move on."""
    run_path = state / "run.json"
    run = read_json(run_path) if run_path.exists() else {}
    active = run.get("active_item")
    task_id = active.get("id") if isinstance(active, dict) else None
    tasks = merged_tasks(state)
    if not isinstance(task_id, str) or not task_id:
        in_progress = [key for key, task in tasks.items() if task.get("status") == "in_progress"]
        task_id = in_progress[-1] if in_progress else None
    reason = reason.strip()[:MAX_REASON]
    if task_id:
        task = tasks.get(task_id, {})
        row = dict(id=task_id, status="blocked", blocked_reason=reason, blocked_by=source,
                   blocked_at=stamp(), note=reason, block_count=int(task.get("block_count", 0) or 0) + 1)
        if "title" not in task and isinstance(active, dict):
            row.update({key: active[key] for key in ("title", "area") if key in active})
        with (state / "backlog.jsonl").open("a") as stream:
            stream.write(json.dumps(row) + "\n")
    if run:
        run.pop("active_item", None)
        history = run.get("supervisor_blocks") if isinstance(run.get("supervisor_blocks"), list) else []
        history.append(dict(task=task_id, reason=reason, at=stamp()))
        run["supervisor_blocks"] = history[-20:]
        atomic_json(run_path, run)
    return task_id


def clear_worker_halt(state, outcome):
    """Turn a worker-declared halt back into a running state with a recorded blocker."""
    run_path = state / "run.json"
    if not run_path.exists():
        return
    run = read_json(run_path)
    if run.get("outcome") == outcome:
        run["outcome"] = None
        run.pop("ended_at", None)
        run["next_action"] = "Previous item was blocked; choose different eligible work or a new discovery lane."
        atomic_json(run_path, run)


def explored_lanes(state):
    """Return lanes ordered by when they were last explored (never-explored first)."""
    last_seen = {}
    for order, row in enumerate(read_rows(state / "discovery.jsonl")):
        lane = row.get("lane") or row.get("area")
        if isinstance(lane, str):
            last_seen[lane] = order
    return last_seen


def suggest_lane(state, focus=None, blocked_areas=()):
    """Pick the next discovery lane: preferred areas first, then least recently explored."""
    focus = focus if isinstance(focus, dict) else {}
    board = focus.get("board_areas") if isinstance(focus.get("board_areas"), dict) else {}
    prefer = [lane for lane in board.get("prefer", []) if lane in DISCOVERY_LANES]
    skip = set(board.get("skip", [])) | set(blocked_areas)
    seen = explored_lanes(state)
    candidates = [lane for lane in DISCOVERY_LANES if lane not in skip]
    if not candidates:
        return None
    ranked = sorted(candidates, key=lambda lane: (lane not in prefer, seen.get(lane, -1)))
    return ranked[0]


def blocked_summary(state, limit=6):
    """Short list of recently blocked tasks so the next cycle routes around them."""
    blocked = [(task_id, task) for task_id, task in merged_tasks(state).items() if task.get("status") == "blocked"]
    lines = []
    for task_id, task in blocked[-limit:]:
        reason = str(task.get("blocked_reason") or task.get("note") or "no reason recorded")[:160]
        lines.append(f"- {task_id} ({task.get('area', 'general')}): {reason}")
    return "\n".join(lines)


def recovery_brief(state, focus=None):
    """Text appended to each cycle prompt: what is blocked and where to look next."""
    parts = []
    blocked = blocked_summary(state)
    if blocked:
        parts.append("Blocked tasks (do not retry unless their blocker changed; pick other work):\n" + blocked)
    if ready_count(state) == 0:
        lane = suggest_lane(state, focus)
        if lane:
            parts.append(f"The ready queue is empty. Run a fresh scoped discovery pass in the '{lane}' lane "
                         "(least recently explored eligible lane), then queue what you verify.")
    return "\n\n".join(parts)


def repair_ledgers(state, names=("backlog.jsonl", "discovery.jsonl", "bets.jsonl")):
    """Move unparsable JSONL lines aside so one bad write cannot stop the run."""
    problems = []
    for name in names:
        path = state / name
        if not path.exists() or path.is_symlink():
            continue
        good, bad = [], []
        for line in path.read_text(errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                ok = isinstance(json.loads(line), dict)
            except ValueError:
                ok = False
            (good if ok else bad).append(line)
        if bad:
            with (state / (name + ".damaged")).open("a") as stream:
                stream.write("\n".join(bad) + "\n")
            tmp = path.with_suffix(".repair")
            tmp.write_text("".join(line + "\n" for line in good))
            tmp.replace(path)
            problems.append(f"moved {len(bad)} invalid line(s) from {name} to {name}.damaged")
    return problems
