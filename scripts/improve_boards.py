"""Which improve boards are running on this machine, and which codebase the user wants to see.

Every repository keeps its own board, knowledge graph, and wiki under its own `.improve/`;
nothing here moves knowledge between repositories. A running board records itself in a small
per-user registry (beside the global learning store) so that the board of a finished or older
run can point to the board of the codebase being improved now. The registry is a convenience:
every function here fails soft, and a board works without it. Standard library only.
"""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import stat
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from improve_control import read_local
from improve_learning import global_home
from improve_runtime import StateError, atomic_json, recorded_timing, stamp

REGISTRY = "boards.json"
MAX_BOARDS = 50
PROBE_SECONDS = 1


def registry_path():
    return global_home() / REGISTRY


@contextlib.contextmanager
def registry_lock():
    """One writer at a time; boards start and stop independently."""
    home = global_home()
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home / "boards.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise StateError("boards.lock must be a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def load():
    """Registered boards as {repository path: {port, instance, started_at}}; damage reads as empty."""
    try:
        raw = read_local(registry_path())
        value = json.loads(raw) if raw else {}
    except (StateError, OSError, ValueError, RecursionError):
        return {}
    boards = value.get("boards") if isinstance(value, dict) else None
    if not isinstance(boards, dict):
        return {}
    return {path: entry for path, entry in boards.items()
            if isinstance(path, str) and isinstance(entry, dict)
            and type(entry.get("port")) is int and 1 <= entry["port"] <= 65535
            and isinstance(entry.get("instance"), str)}


def save(boards):
    path = registry_path()
    if path.is_symlink():
        raise StateError("boards.json must not be a symlink")
    # Newest first, bounded: a machine that has run many boards does not grow the file forever.
    kept = sorted(boards.items(), key=lambda item: str(item[1].get("started_at") or ""), reverse=True)[:MAX_BOARDS]
    atomic_json(path, {"boards": dict(kept)})


def record(repo, port, instance):
    """Remember that `repo`'s board is serving on `port`. Never raises. A board calls this when
    it starts and again whenever it looks at the registry, so an entry lost to damage or to
    another board's cleanup comes back; an unchanged entry is not rewritten."""
    try:
        entry = load().get(str(repo))
        if entry and entry.get("port") == int(port) and entry.get("instance") == str(instance):
            return
        with registry_lock():
            boards = load()
            boards[str(repo)] = {"port": int(port), "instance": str(instance), "started_at": stamp()}
            save(boards)
    except (StateError, OSError, ValueError):
        pass


def forget(repo, instance=None):
    """Drop `repo`'s entry; with `instance`, only if that board is still the one recorded."""
    try:
        with registry_lock():
            boards = load()
            entry = boards.get(str(repo))
            if entry is None or (instance is not None and entry.get("instance") != instance):
                return
            del boards[str(repo)]
            save(boards)
    except (StateError, OSError, ValueError):
        pass


def url_of(entry):
    return f"http://127.0.0.1:{entry['port']}"


def running(path, entry):
    """True when the recorded board answers as that repository's board, False when it is gone
    (nothing listens there, or something else does), and None when it cannot be told: a slow
    answer is not a stopped board."""
    try:
        # Local board traffic must not be sent through configured HTTP proxies.
        with build_opener(ProxyHandler({})).open(Request(url_of(entry) + "/api/health"), timeout=PROBE_SECONDS) as response:
            health = json.load(response)
    except URLError as exc:
        return False if isinstance(exc.reason, ConnectionRefusedError) else None
    except ConnectionRefusedError:
        return False
    except (OSError, ValueError):
        return None
    return (isinstance(health, dict) and health.get("service") == "improve-board"
            and health.get("instance") == entry.get("instance") and health.get("repo") == path)


def state_file(repo, name):
    try:
        raw = read_local(Path(repo) / ".improve" / name)
        value = json.loads(raw) if raw else {}
    except (StateError, OSError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def run_rank(repo):
    """(live, started_at) for a repository's latest run. A run still inside its deadline
    outranks a finished one; among equals the later start wins. No run ranks last."""
    run, control = state_file(repo, "run.json"), state_file(repo, "supervisor.json")
    source = control or run
    started = source.get("started_at") if isinstance(source.get("started_at"), str) else ""
    if not started:
        return False, ""
    try:
        remaining = recorded_timing(run, control or None)["remaining_seconds"]
    except (StateError, ValueError, TypeError, KeyError):
        return False, started
    outcome = source.get("outcome")
    # As the board itself reads it: without a supervisor, "completed" before the deadline is
    # premature and the run is still live.
    if not control and outcome == "completed" and remaining > 0:
        outcome = None
    return outcome is None and remaining > 0, started


def view(repo, probe=running, usual_address=True):
    """What `repo`'s board shows about the others: every other running board, the one whose
    codebase outranks this one (if any), and whether an open page should follow it. Only the
    board at the usual address sends its page on, and never while its own run is live; a board
    at any other address was opened for its own codebase, so it only offers the link."""
    repo = str(repo)
    mine = run_rank(repo)
    others, stale = [], []
    for path, entry in sorted(load().items()):
        if path == repo:
            continue
        answer = probe(path, entry)
        if answer is False:
            stale.append((path, entry["instance"]))
        if not answer:
            continue
        live, started = run_rank(path)
        others.append({"repo": Path(path).name, "url": url_of(entry), "live": live, "started_at": started})
    for path, instance in stale:
        forget(path, instance)
    others.sort(key=lambda row: (row["live"], row["started_at"]), reverse=True)
    best = others[0] if others and (others[0]["live"], others[0]["started_at"]) > mine else None
    return {"others": [{"repo": row["repo"], "url": row["url"], "live": row["live"]} for row in others],
            "active": {"repo": best["repo"], "url": best["url"]} if best else None,
            "follow": bool(best) and not mine[0] and bool(usual_address)}
