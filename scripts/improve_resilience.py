"""Supervisor-side recovery helpers: keep a run working around failures. Standard library only.

Runs only between cycles, when no child agent can be writing the ledgers.
"""

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

from improve_runtime import StateError, atomic_json, coerce_count, git, read_json, stamp

# Lanes the supervisor rotates through when the queue is empty. Board focus and
# intake scope still filter them; this only suggests where to look next.
DISCOVERY_LANES = (
    "ui", "features", "accessibility", "performance", "security", "resilience",
    "quality", "coverage", "concurrency", "contracts", "docs", "assets", "gate-speed",
)
MAX_REASON = 500
# Outcomes an agent may legitimately write; anything else is repaired, never trusted.
TERMINAL_OUTCOMES = ("completed", "stopped by user", "target reached")
LEDGERS = ("backlog.jsonl", "discovery.jsonl", "bets.jsonl")


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


def write_rows(path, rows):
    """Atomically replace a JSONL ledger, the way the skill itself writes it."""
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def merged_tasks(state):
    tasks = {}
    for row in read_rows(state / "backlog.jsonl"):
        task_id = row.get("id")
        if isinstance(task_id, str) and task_id:
            tasks[task_id] = {**tasks.get(task_id, {}), **row}
    return tasks


def ready_count(state):
    return sum(task.get("status", "ready") == "ready" for task in merged_tasks(state).values())


# ---------------------------------------------------------------- agent-written state

def repair_run(run):
    """Normalize the agent-written fields the supervisor and clock depend on.

    Returns the problems found (and repaired in place). The ledger an agent writes is
    input, not truth: an unrecognized outcome, a float report hour, or a string cycle
    must never end a run that still has time on the clock.
    """
    problems = []
    if not isinstance(run, dict):
        raise StateError("run.json must contain an object")
    outcome = run.get("outcome")
    if outcome is not None:
        fixed = None
        if isinstance(outcome, str):
            text = outcome.strip()
            lowered = text.lower()
            if text in TERMINAL_OUTCOMES or text.startswith("halted:"):
                fixed = text
            elif lowered in TERMINAL_OUTCOMES:
                fixed = lowered
            elif re.match(r"halted\b[\s:-]*", lowered):
                reason = re.sub(r"(?i)^halted\b[\s:-]*", "", text).strip() or "unspecified"
                fixed = f"halted: {reason}"
        if fixed != outcome:
            problems.append(f"outcome {outcome!r} is not a recognized terminal outcome; "
                            + (f"normalized to {fixed!r}" if fixed else "cleared so the run continues"))
            run["outcome"] = fixed
    for key, fallback in (("last_report_hour", 0), ("cycle", None)):
        if key not in run:
            continue
        value = run[key]
        count = coerce_count(value)
        if count is None:
            if fallback is None:
                problems.append(f"{key} {value!r} is not a nonnegative integer; removed")
                run.pop(key)
            else:
                problems.append(f"{key} {value!r} is not a nonnegative integer; reset to {fallback}")
                run[key] = fallback
        elif type(value) is not int or value != count:
            problems.append(f"{key} {value!r} coerced to {count}")
            run[key] = count
    if "active_item" in run and run["active_item"] is not None and not isinstance(run["active_item"], dict):
        problems.append("active_item was not an object; cleared")
        run["active_item"] = None
    if "summary_pending" in run and not isinstance(run["summary_pending"], bool):
        run["summary_pending"] = bool(run["summary_pending"])
    return problems


def decode_problems(path):
    """Report bytes a ledger holds that are not UTF-8, so the supervisor can repair them."""
    try:
        path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        return f"{path.name} held bytes that are not UTF-8 at offset {exc.start}; they were replaced"
    return None


# ---------------------------------------------------------------- working tree

# Untracked paths that are build output or tool caches rather than unverified work. A
# repository can extend this list with run.json.artifact_paths (fnmatch patterns).
ARTIFACT_PATTERNS = (
    "build/*", "dist/*", "out/*", "target/*", "bin/*", "obj/*", "node_modules/*", "__pycache__/*", "*/__pycache__/*",
    ".pytest_cache/*", ".mypy_cache/*", ".ruff_cache/*", ".tox/*", ".venv/*", "venv/*", ".cache/*", ".gradle/*",
    ".next/*", ".turbo/*", ".parcel-cache/*", "coverage/*", ".coverage", ".coverage.*", "coverage.xml", "lcov.info",
    "DerivedData/*", "*.xcresult", "*.xcresult/*", "*.pyc", "*.pyo", "*.o", "*.a", "*.so", "*.dylib", "*.class",
    "*.log", "*.tmp", "*.swp", ".DS_Store", "*/.DS_Store", "Thumbs.db", "*.orig", "*.rej",
)


def is_artifact(path, extra=()):
    import fnmatch
    path = path.strip().strip('"')
    if path.startswith("./"):
        path = path[2:]
    for pattern in (*ARTIFACT_PATTERNS, *extra):
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path.rstrip("/"), pattern.rstrip("/*")):
            return True
    return False


def dirt(repo, artifact_paths=()):
    """Classify uncommitted changes outside .improve.

    None: clean. 'tracked': edits to files under version control. 'untracked': new files
    (unverified work the agent did not commit). 'artifacts': only build output or tool
    caches (by ARTIFACT_PATTERNS and the run's artifact_paths), which carry no work."""
    output = git(repo, "status", "--porcelain", "--", ".", ":(exclude).improve")
    if not output:
        return None
    lines = output.splitlines()
    if not all(line.startswith("??") for line in lines):
        return "tracked"
    extra = tuple(pattern for pattern in artifact_paths if isinstance(pattern, str)) if isinstance(artifact_paths, (list, tuple)) else ()
    if all(is_artifact(line[3:], extra) for line in lines):
        return "artifacts"
    return "untracked"


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
             "-c", "commit.gpgsign=false", "commit-tree", tree, "-p", head, "-m",
             f"improve: quarantined uncommitted work ({label})"],
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


def quarantine_refs(repo, limit=50):
    """Saved quarantine refs, newest first, for status and the board."""
    try:
        output = git(repo, "for-each-ref", "--sort=-refname", f"--count={limit}",
                     "--format=%(refname)%09%(objectname:short)%09%(subject)", "refs/improve/quarantine")
    except StateError:
        return []
    rows = []
    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            rows.append({"ref": parts[0], "commit": parts[1], "subject": parts[2]})
    return rows


# ---------------------------------------------------------------- processes

def process_command(pid):
    """The command line of a live process, or None when it is gone."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OverflowError, ValueError):
        return None
    try:
        output = subprocess.run(["ps", "-o", "stat=", "-o", "command=", "-p", str(pid)], capture_output=True,
                                text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    if not output:
        return None
    state, _, command = output.partition(" ")
    if state.startswith("Z"):
        return None  # A zombie holds no files and edits nothing.
    return command.strip()


def stale_child(control):
    """The pid of a previous daemon's cycle that is still running, or None.

    A daemon that died mid-cycle leaves its agent alive and editing the tree; a restart
    must settle that process before launching another one against the same repository."""
    pid = control.get("child_pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1 or pid == os.getpid():
        return None
    command = process_command(pid)
    if command is None:
        return None
    engine = control.get("engine") or ""
    # A reused pid belongs to someone else unless it runs the saved engine's executable
    # (and is not another supervisor that merely names the engine in its arguments).
    if command:
        tokens = command.split()
        runs_engine = any(token == engine or token.endswith("/" + engine) for token in tokens)
        if (engine and not runs_engine) or "improve_runtime.py" in command:
            return None
    return pid


def terminate_group(pid, grace=5.0):
    """TERM a process group, wait briefly, then KILL whatever ignored it."""
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 1.0)):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return True
        except PermissionError:
            return False
        until = time.monotonic() + wait
        while time.monotonic() < until:
            if process_command(pid) is None:
                return True
            time.sleep(0.05)
    return process_command(pid) is None


# ---------------------------------------------------------------- tasks

def block_active_item(state, reason, source="supervisor"):
    """Mark the in-flight task blocked with a reason and clear it so work can move on.

    The ledger is rewritten atomically with one canonical row per id. The task's own
    `note` (its evidence or rejection text) is preserved; the supervisor's reason goes to
    `blocked_reason` and `supervisor_note`.
    """
    run_path = state / "run.json"
    run = read_json(run_path) if run_path.exists() else {}
    active = run.get("active_item")
    task_id = active.get("id") if isinstance(active, dict) else None
    rows = read_rows(state / "backlog.jsonl")
    tasks = {}
    for row in rows:
        key = row.get("id")
        if isinstance(key, str) and key:
            tasks[key] = {**tasks.get(key, {}), **row}
    if not isinstance(task_id, str) or not task_id:
        in_progress = [key for key, task in tasks.items() if task.get("status") == "in_progress"]
        task_id = in_progress[-1] if in_progress else None
    reason = reason.strip()[:MAX_REASON]
    if task_id:
        task = dict(tasks.get(task_id, {}))
        if "title" not in task and isinstance(active, dict):
            task.update({key: active[key] for key in ("title", "area") if key in active})
        task.update(id=task_id, status="blocked", blocked_reason=reason, blocked_by=source, blocked_at=stamp(),
                    supervisor_note=reason, updated_at=stamp(),
                    block_count=int(task.get("block_count", 0) or 0) + 1)
        task.setdefault("note", reason)
        tasks[task_id] = task
        write_rows(state / "backlog.jsonl", list(tasks.values()))
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


def repair_ledgers(state, names=LEDGERS):
    """Move unparsable JSONL lines aside so one bad write cannot stop the run."""
    problems = []
    for name in names:
        path = state / name
        if not path.exists() or path.is_symlink():
            continue
        undecodable = decode_problems(path)
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
        if bad or undecodable:
            tmp = path.with_suffix(".repair")
            tmp.write_text("".join(line + "\n" for line in good))
            tmp.replace(path)
        if bad:
            problems.append(f"moved {len(bad)} invalid line(s) from {name} to {name}.damaged")
        if undecodable:
            problems.append(undecodable)
    return problems
