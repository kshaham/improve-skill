"""Knowledge graph of improvement sessions: what was tried, where, and what was learned.

Standard library only. One store per repository, `.improve/graph.json`, which survives
`--new-run` archival like learning.json. The supervisor ingests the live run after every
cycle and refines the graph when a run ends; agents query it before work and add what
they discover. See references/knowledge-graph.md for the query recipes.

Node ids are typed and stable:  run:<id>  task:<run>:<id>  area:<name>  file:<path>
gate:<name>  blocker:<key>  insight:<hash>.
"""

import argparse
import contextlib
from collections import deque
from functools import lru_cache
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from improve_runtime import StateError, atomic_json, read_json, stamp

VERSION = 1
TYPES = ("run", "task", "area", "file", "gate", "blocker", "insight")
INSIGHT_KINDS = ("lesson", "pitfall", "technique", "convention", "decision", "fact")
# Relations an agent or user may add. Structural relations are written only by ingest/refine.
USER_RELATIONS = ("about", "relates_to", "causes", "fixes", "avoids", "depends_on", "supersedes", "used_in")
STRUCTURAL = ("in_run", "in_area", "touches", "verified_by", "blocked_by", "co_changed")
RELATIONS = USER_RELATIONS + STRUCTURAL
MAX_LABEL = 200
MAX_SUMMARY = 600
MAX_FILES_PER_TASK = 20
MAX_TASKS = 1500
MAX_FILES = 1500
MAX_INSIGHTS = 400
MAX_CO_CHANGE = 600
MAX_REFINE_LOG = 30
MERGE_SIMILARITY = 0.75
DECAY = 0.9
FADE_SCORE = 0.2
FADE_AFTER_RUNS = 8
FINISHED = ("done", "rejected", "blocked")
STOP_WORDS = frozenset("a an and are as at be by for from has in is it its of on or that the this to was were when with".split())

_cache = {}


class GraphError(StateError):
    """Raised for invalid graph input; the message is safe to show a user."""


def graph_path(repo):
    return Path(repo) / ".improve" / "graph.json"


def empty_graph():
    return {"version": VERSION, "revision": 0, "nodes": {}, "edges": {}, "runs": [],
            "aliases": {}, "refine": {"history": []}}


def load(path, repair=True):
    """Read the graph. A damaged file never blocks work: writers set it aside, readers ignore it."""
    if not path.exists():
        return empty_graph()
    try:
        value = read_json(path)
        if not isinstance(value.get("nodes"), dict) or not isinstance(value.get("edges"), dict):
            raise StateError("graph.json lacks nodes/edges objects")
    except (StateError, OSError, ValueError, RecursionError):
        if repair:
            path.replace(path.with_suffix(".damaged.json"))
        return empty_graph()
    graph = {**empty_graph(), **value}
    for key, kind in (("runs", list), ("aliases", dict), ("refine", dict)):
        if not isinstance(graph.get(key), kind):
            graph[key] = empty_graph()[key]
    graph["nodes"] = {key: node for key, node in graph["nodes"].items()
                      if isinstance(node, dict) and node.get("type") in TYPES}
    graph["edges"] = {key: edge for key, edge in graph["edges"].items()
                      if isinstance(edge, dict) and edge.get("src") in graph["nodes"]
                      and edge.get("dst") in graph["nodes"] and edge.get("rel") in RELATIONS}
    return graph


def load_cached(path):
    """Read-only load keyed on mtime/size so a polling board does not re-parse an unchanged file."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return empty_graph()
    key = (str(path), info.st_mtime_ns, info.st_size)
    cached = _cache.get(str(path))
    if cached and cached[0] == key:
        return cached[1]
    graph = load(path, repair=False)
    _cache[str(path)] = (key, graph)
    return graph


@contextlib.contextmanager
def locked(repo):
    """Exclusive read-modify-write of the graph shared by the daemon, agents, and the board."""
    state = Path(repo) / ".improve"
    if state.is_symlink():
        raise GraphError(".improve must be a directory inside the repository")
    state.mkdir(exist_ok=True)
    if graph_path(repo).is_symlink():
        raise GraphError("graph.json must not be a symlink")
    fd = os.open(state / "graph.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise GraphError("graph.lock must be a regular file")
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            graph = load(graph_path(repo))
            before = json.dumps(graph, sort_keys=True)
            yield graph
            if json.dumps(graph, sort_keys=True) != before:
                graph["revision"] = int(graph.get("revision", 0)) + 1
                graph["updated_at"] = stamp()
                atomic_json(graph_path(repo), graph)
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


# ---------------------------------------------------------------- text helpers

def clean(text, limit):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def normalized(text):
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", str(text).lower())).strip()


@lru_cache(maxsize=8192)
def tokens(text):
    return frozenset({word for word in re.findall(r"[a-z0-9_]+", str(text).lower().replace("/", " ").replace(".", " "))
            if len(word) > 1 and word not in STOP_WORDS})


def similarity(left, right):
    a, b = tokens(left), tokens(right)
    return len(a & b) / len(a | b) if a and b else 0.0


def short_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def blocker_key(reason):
    # Matches improve_learning: collapse SHAs, numbers, and paths so repeats group together.
    reason = re.sub(r"[0-9a-f]{7,40}|\d+|refs/\S+|/\S+", "#", str(reason).lower())
    return normalized(reason)[:120]


def safe_path(value, repo=None):
    """A repository-relative file path, or None for anything that cannot be one."""
    if not isinstance(value, str):
        return None
    value = value.strip().split(":", 1)[0] if re.search(r":\d+$", value.strip()) else value.strip()
    if not value or len(value) > 300 or "\x00" in value or "\n" in value:
        return None
    if value.startswith("/"):
        if repo is None:
            return None
        try:
            value = str(Path(value).resolve().relative_to(Path(repo).resolve()))
        except ValueError:
            return None
    value = value.removeprefix("./").rstrip("/")
    if not value or value.startswith(".improve") or ".." in Path(value).parts:
        return None
    return value


def task_files(task, repo=None):
    found = []
    for key in ("paths", "owned_paths"):
        values = task.get(key)
        if isinstance(values, str):
            values = [values]
        if isinstance(values, list):
            found.extend(values)
    found.append(task.get("file"))
    files = []
    for value in found:
        path = safe_path(value, repo)
        if path and path not in files:
            files.append(path)
    return files[:MAX_FILES_PER_TASK]


# ---------------------------------------------------------------- graph primitives

def resolve(graph, node_id):
    seen = set()
    while node_id in graph["aliases"] and node_id not in seen:
        seen.add(node_id)
        node_id = graph["aliases"][node_id]
    return node_id


def run_seq(graph):
    return len(graph["runs"])


def upsert(graph, node_id, node_type, label, seq=None, **fields):
    node = graph["nodes"].get(node_id)
    now = stamp()
    if node is None:
        node = {"id": node_id, "type": node_type, "label": clean(label, MAX_LABEL) or node_id,
                "created_at": now, "first_seq": seq if seq is not None else run_seq(graph)}
        graph["nodes"][node_id] = node
    elif label:
        node["label"] = clean(label, MAX_LABEL)
    for key, value in fields.items():
        if value is not None:
            node[key] = value
    if seq is not None:
        node["last_seq"] = max(int(node.get("last_seq", seq)), seq)
        runs = node.setdefault("seqs", [])
        if seq not in runs:
            runs.append(seq)
            del runs[:-20]
    return node


def link(graph, src, rel, dst, seq=None, **fields):
    if rel not in RELATIONS:
        raise GraphError(f"unknown relation {rel!r}; use one of {', '.join(USER_RELATIONS)}")
    src, dst = resolve(graph, src), resolve(graph, dst)
    if src == dst or src not in graph["nodes"] or dst not in graph["nodes"]:
        return None
    if rel == "co_changed" and dst < src:
        src, dst = dst, src
    key = f"{src}|{rel}|{dst}"
    edge = graph["edges"].setdefault(key, {"src": src, "rel": rel, "dst": dst, "weight": 1, "created_at": stamp()})
    for name, value in fields.items():
        if value is not None:
            edge[name] = value
    if seq is not None:
        edge["last_seq"] = seq
    return edge


def remove_node(graph, node_id):
    graph["nodes"].pop(node_id, None)
    for key in [key for key, edge in graph["edges"].items() if node_id in (edge["src"], edge["dst"])]:
        del graph["edges"][key]


def edges_of(graph, node_id):
    return [edge for edge in graph["edges"].values() if node_id in (edge["src"], edge["dst"])]


def adjacency(graph):
    result = {}
    for edge in graph["edges"].values():
        result.setdefault(edge["src"], []).append((edge, edge["dst"]))
        result.setdefault(edge["dst"], []).append((edge, edge["src"]))
    return result


def target_id(graph, value, repo=None):
    """Turn `file:x`, `area:ui`, a bare path, or an existing id into a node id, creating file/area nodes."""
    value = str(value).strip()
    resolved = resolve(graph, value)
    if resolved in graph["nodes"]:
        return resolved
    prefix, _, rest = value.partition(":")
    if prefix == "area" and re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", rest):
        upsert(graph, value, "area", rest)
        return value
    path = safe_path(rest if prefix == "file" else value, repo)
    if path and (prefix == "file" or "/" in value or "." in value):
        node_id = "file:" + path
        upsert(graph, node_id, "file", path)
        return node_id
    raise GraphError(f"unknown graph node {value!r}; use file:PATH, area:NAME, or an existing node id")


# ---------------------------------------------------------------- ingest

def ingest_run(graph, state, run_id, control=None, repo=None):
    """Fold one run directory into the graph. Upsert-only, so re-ingesting a live run is safe."""
    from improve_resilience import merged_tasks

    control = control or {}
    run = {}
    if (state / "run.json").is_file() and not (state / "run.json").is_symlink():
        with contextlib.suppress(StateError):
            run = read_json(state / "run.json")
    entry = next((row for row in graph["runs"] if row.get("run_id") == run_id), None)
    if entry is None:
        entry = {"run_id": run_id, "seq": run_seq(graph) + 1, "first_ingested_at": stamp()}
        graph["runs"].append(entry)
    seq = entry["seq"]
    tasks = merged_tasks(state)
    counts = {status: sum(task.get("status") == status for task in tasks.values()) for status in FINISHED}
    outcome = control.get("outcome") if control else run.get("outcome")
    entry.update(ingested_at=stamp(), outcome=outcome, tasks=len(tasks), **counts)
    run_node = "run:" + run_id
    upsert(graph, run_node, "run", run_id, seq, outcome=outcome, engine=control.get("engine"),
           started_at=control.get("started_at") or run.get("started_at"),
           ended_at=control.get("ended_at") or run.get("ended_at"), stats={"tasks": len(tasks), **counts})

    gates = run.get("gates") if isinstance(run.get("gates"), dict) else {}
    seconds = run.get("gate_seconds") if isinstance(run.get("gate_seconds"), dict) else {}
    gate_ids = {}
    for name, command in list(gates.items())[:40]:
        if not re.fullmatch(r"[\w.:-]{1,60}", str(name)) or name == "...":
            continue
        cost = seconds.get(name)
        upsert(graph, "gate:" + name, "gate", name, seq,
               summary=clean(command if isinstance(command, str) else json.dumps(command), MAX_SUMMARY),
               seconds=cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None)
        gate_ids[name] = "gate:" + name

    for task_id, task in list(tasks.items())[-MAX_TASKS:]:
        status = task.get("status") if isinstance(task.get("status"), str) else "ready"
        area = task.get("area") if isinstance(task.get("area"), str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", task["area"]) else "general"
        node_id = f"task:{run_id}:{task_id}"
        reason = task.get("blocked_reason") if status == "blocked" else None
        detail = task.get("note") if status in ("rejected", "blocked") else task.get("evidence")
        upsert(graph, node_id, "task", task.get("title") or task.get("claim") or task_id, seq,
               status=status, area=area, run_id=run_id, task_id=str(task_id),
               summary=clean(task.get("claim") or "", MAX_SUMMARY) or None,
               outcome_note=clean(reason or detail or "", MAX_SUMMARY) or None,
               commit=task.get("commit") if isinstance(task.get("commit"), str) else None)
        link(graph, node_id, "in_run", run_node, seq)
        upsert(graph, "area:" + area, "area", area, seq)
        link(graph, node_id, "in_area", "area:" + area, seq)
        for path in task_files(task, repo):
            upsert(graph, "file:" + path, "file", path, seq)
            link(graph, node_id, "touches", "file:" + path, seq)
        verification = json.dumps(task.get("verification") or task.get("last_verification") or "")
        for name, gate in gate_ids.items():
            if len(name) > 2 and name in verification:
                link(graph, node_id, "verified_by", gate, seq)
        if reason or status == "blocked":
            reason = clean(reason or task.get("note") or "unspecified", 300)
            key = blocker_key(reason) or "unspecified"
            blocker = "blocker:" + short_hash(key)
            upsert(graph, blocker, "blocker", reason, seq, key=key)
            link(graph, node_id, "blocked_by", blocker, seq)
        refs = task.get("graph_refs")
        for ref in refs[:20] if isinstance(refs, list) else []:
            ref = resolve(graph, str(ref))
            if ref in graph["nodes"] and graph["nodes"][ref]["type"] == "insight":
                link(graph, ref, "used_in", node_id, seq)
                graph["nodes"][ref]["last_seq"] = max(int(graph["nodes"][ref].get("last_seq", 0)), seq)
    return entry


def import_lessons(graph, repo):
    """Mirror learning.json lessons as insight nodes so they are connected to areas and files."""
    from improve_learning import load as load_learning, store_path

    lessons = load_learning(store_path(repo), repair=False)["lessons"]
    for lesson in lessons[:MAX_INSIGHTS]:
        text = lesson.get("text")
        if not isinstance(text, str) or len(text) < 8:
            continue
        node = add_insight(graph, text, "lesson", source="learning", reinforce=False)
        node["reinforced"] = max(int(node.get("reinforced", 1)), int(lesson.get("reinforced", 1) or 1))
        area = lesson.get("area")
        if isinstance(area, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", area):
            upsert(graph, "area:" + area, "area", area)
            link(graph, node["id"], "about", "area:" + area)


def run_identity(directory):
    for name in ("supervisor.json", "run.json"):
        path = directory / name
        if path.is_file() and not path.is_symlink():
            with contextlib.suppress(StateError):
                value = read_json(path)
                ident = value.get("run_id") or value.get("started_at")
                if isinstance(ident, str) and ident:
                    return ident, value if name == "supervisor.json" else {}
    return None, {}


def sync(repo, include_history=None, refine_after=False):
    """Ingest archived runs (first use only, unless asked) and the current run, then optionally refine."""
    repo = Path(repo)
    state = repo / ".improve"
    with locked(repo) as graph:
        known = {row.get("run_id") for row in graph["runs"]}
        history = state / "history"
        backfill = include_history if include_history is not None else not graph["runs"]
        ingested = []
        if backfill and history.is_dir() and not history.is_symlink():
            for directory in sorted(history.iterdir()):
                if directory.is_dir() and not directory.is_symlink():
                    run_id, control = run_identity(directory)
                    if run_id and (include_history or run_id not in known):
                        ingest_run(graph, directory, run_id, control, repo)
                        ingested.append(run_id)
        run_id, control = run_identity(state)
        if run_id:
            ingest_run(graph, state, run_id, control, repo)
            ingested.append(run_id)
        import_lessons(graph, repo)
        report = refine(graph, repo) if refine_after else None
        return {"ingested": ingested, "refine": report, "nodes": len(graph["nodes"]), "edges": len(graph["edges"])}


# ---------------------------------------------------------------- agent/user writes

def add_insight(graph, text, kind="fact", source="agent", about=(), run_id=None, repo=None, reinforce=True):
    """Record knowledge. A near-duplicate reinforces the existing node instead of adding noise."""
    text = clean(text, MAX_SUMMARY)
    if len(text) < 8:
        raise GraphError("knowledge needs at least 8 characters")
    if kind not in INSIGHT_KINDS:
        raise GraphError(f"kind must be one of {', '.join(INSIGHT_KINDS)}")
    node_id = resolve(graph, "insight:" + short_hash(normalized(text)))
    node = graph["nodes"].get(node_id)
    if node is None:
        node = next((item for item in graph["nodes"].values() if item["type"] == "insight"
                     and item.get("kind") == kind and similarity(item.get("summary", ""), text) >= MERGE_SIMILARITY), None)
    seq = run_seq(graph)
    if node is None:
        node = upsert(graph, node_id, "insight", text[:120], seq, summary=text, kind=kind, source=source,
                      reinforced=1, useful=0, wrong=0, run_id=run_id)
    else:
        if reinforce:
            node["reinforced"] = int(node.get("reinforced", 1)) + 1
        node["last_seq"] = max(int(node.get("last_seq", 0)), seq)
        if source == "user":
            node["source"] = "user"
        if reinforce:
            node.pop("archived", None)
    for value in about:
        link(graph, node["id"], "about", target_id(graph, value, repo), seq)
    return node


def rate(graph, node_id, verdict, source="agent"):
    node = graph["nodes"].get(resolve(graph, node_id))
    if node is None:
        raise GraphError(f"no graph node {node_id!r}")
    if verdict == "useful":
        node["useful"] = int(node.get("useful", 0)) + 1
        node["last_seq"] = run_seq(graph)
        node.pop("archived", None)
    elif verdict == "wrong":
        node["wrong"] = int(node.get("wrong", 0)) + 1
        if source == "user":
            node["archived"] = "marked wrong by the user"
    elif verdict in ("pin", "unpin"):
        node["pinned"] = verdict == "pin"
        if node["pinned"]:
            node.pop("archived", None)
    elif verdict in ("archive", "restore"):
        if verdict == "archive":
            node["archived"] = f"archived by {source}"
            node["pinned"] = False
        else:
            node.pop("archived", None)
            node["restored_seq"] = run_seq(graph)
    else:
        raise GraphError("verdict must be useful, wrong, pin, unpin, archive, or restore")
    return node


# ---------------------------------------------------------------- refine

def tracked_files(repo):
    if repo is None:
        return None
    try:
        output = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True, timeout=20, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return set(output.decode(errors="replace").split("\0")) - {""}


def outcome_stats(task_ids, graph, base=None):
    stats = {"done": 0, "rejected": 0, "blocked": 0, "open": 0, **(base or {})}
    for task_id in task_ids:
        status = graph["nodes"][task_id].get("status")
        stats[status if status in FINISHED else "open"] += 1
    attempts = stats["done"] + stats["rejected"] + stats["blocked"]
    stats["attempts"] = attempts
    # Laplace-smoothed so a single failure does not brand a file as risky.
    stats["success"] = round((stats["done"] + 1) / (attempts + 2), 3)
    return stats


def merge_into(graph, keep, drop):
    keep_node, drop_node = graph["nodes"][keep], graph["nodes"][drop]
    for field in ("reinforced", "useful", "wrong"):
        keep_node[field] = int(keep_node.get(field, 0)) + int(drop_node.get(field, 0))
    keep_node["last_seq"] = max(int(keep_node.get("last_seq", 0)), int(drop_node.get("last_seq", 0)))
    keep_node["pinned"] = bool(keep_node.get("pinned") or drop_node.get("pinned"))
    if drop_node.get("source") == "user":
        keep_node["source"] = "user"
    keep_node.setdefault("merged_from", []).append(clean(drop_node.get("summary"), 160))
    del keep_node["merged_from"][:-10]
    for edge in edges_of(graph, drop):
        src = keep if edge["src"] == drop else edge["src"]
        dst = keep if edge["dst"] == drop else edge["dst"]
        link(graph, src, edge["rel"], dst, edge.get("last_seq"))
    remove_node(graph, drop)
    graph["aliases"][drop] = keep


def refine(graph, repo=None):
    """Periodic maintenance. Deterministic from saved facts, so running it often is harmless."""
    current = run_seq(graph)
    report = {"at": stamp(), "seq": current, "merged": 0, "faded": 0, "pruned_tasks": 0,
              "pruned_files": 0, "stale_files": 0, "co_changed": 0}
    nodes = graph["nodes"]

    # 1. Merge near-duplicate insights into the better-supported one.
    insights = sorted((node for node in nodes.values() if node["type"] == "insight"),
                      key=lambda node: (-int(node.get("reinforced", 1)) - 2 * int(node.get("useful", 0)), node["id"]))
    kept = []
    for node in insights:
        twin = next((other for other in kept if other.get("kind") == node.get("kind")
                     and similarity(other.get("summary", ""), node.get("summary", "")) >= MERGE_SIMILARITY), None)
        if twin:
            merge_into(graph, twin["id"], node["id"])
            report["merged"] += 1
        else:
            kept.append(node)

    # 2. Prune the oldest finished tasks, folding their outcomes into file/area baselines.
    tasks = sorted((node for node in nodes.values() if node["type"] == "task"),
                   key=lambda node: (node.get("status") not in FINISHED, int(node.get("last_seq", 0)), node["id"]))
    for node in tasks[:max(0, len(tasks) - MAX_TASKS)]:
        status = node.get("status") if node.get("status") in FINISHED else "open"
        for edge in edges_of(graph, node["id"]):
            other = nodes.get(edge["dst"])
            if other and other["type"] in ("file", "area"):
                base = other.setdefault("base", {})
                base[status] = base.get(status, 0) + 1
        remove_node(graph, node["id"])
        report["pruned_tasks"] += 1

    # 3. Recompute derived statistics and co-change edges from the remaining facts.
    touched, in_area, used = {}, {}, {}
    for edge in graph["edges"].values():
        if edge["rel"] == "touches":
            touched.setdefault(edge["dst"], []).append(edge["src"])
        elif edge["rel"] == "in_area":
            in_area.setdefault(edge["dst"], []).append(edge["src"])
        elif edge["rel"] == "used_in":
            used.setdefault(edge["src"], []).append(edge["dst"])
    for key in [key for key, edge in graph["edges"].items() if edge["rel"] == "co_changed"]:
        del graph["edges"][key]
    pairs = {}
    by_task = {}
    for file_id, task_ids in touched.items():
        for task_id in task_ids:
            by_task.setdefault(task_id, []).append(file_id)
    for task_id, files in by_task.items():
        if nodes[task_id].get("status") == "done" and 2 <= len(files) <= 8:
            for index, left in enumerate(sorted(files)):
                for right in sorted(files)[index + 1:]:
                    pairs[(left, right)] = pairs.get((left, right), 0) + 1
    for (left, right), weight in sorted(pairs.items(), key=lambda item: -item[1])[:MAX_CO_CHANGE]:
        link(graph, left, "co_changed", right, weight=weight)
    report["co_changed"] = min(len(pairs), MAX_CO_CHANGE)

    files = tracked_files(repo)
    for node in nodes.values():
        if node["type"] == "file":
            node["stats"] = outcome_stats(touched.get(node["id"], []), graph, node.get("base"))
            areas = {}
            for task_id in touched.get(node["id"], []):
                area = nodes[task_id].get("area", "general")
                areas[area] = areas.get(area, 0) + 1
            if areas:
                area = max(sorted(areas), key=areas.get)
                node["area"] = area
            stale = files is not None and node["label"] not in files and not any(
                path.startswith(node["label"] + "/") for path in files)
            node["stale"] = stale or None
            report["stale_files"] += bool(stale)
        elif node["type"] == "area":
            node["stats"] = outcome_stats(in_area.get(node["id"], []), graph, node.get("base"))
        elif node["type"] in ("gate", "blocker"):
            rel = "verified_by" if node["type"] == "gate" else "blocked_by"
            node["uses"] = sum(1 for edge in graph["edges"].values() if edge["rel"] == rel and edge["dst"] == node["id"])
        elif node["type"] == "insight":
            cited = used.get(node["id"], [])
            node["cited_done"] = sum(nodes[task].get("status") == "done" for task in cited if task in nodes)
            node["cited_failed"] = sum(nodes[task].get("status") in ("rejected", "blocked") for task in cited if task in nodes)

    # 4. Score everything; fade insights that stopped earning their place.
    for node in nodes.values():
        node["score"] = round(score(node, current), 4)
        if node["type"] == "insight" and not node.get("pinned") and not node.get("archived"):
            idle = current - max(int(node.get("last_seq", 0)), int(node.get("restored_seq", -1)))
            wrong = int(node.get("wrong", 0))
            if wrong >= 2 and wrong > int(node.get("useful", 0)) + int(node.get("cited_done", 0)):
                node["archived"] = "contradicted more often than confirmed"
            elif node["score"] < FADE_SCORE and idle >= FADE_AFTER_RUNS:
                node["archived"] = f"not used in {idle} runs"
            report["faded"] += bool(node.get("archived"))

    # 5. Bound size: drop the weakest unconnected files and archived insights.
    for kind, limit, counter in (("file", MAX_FILES, "pruned_files"), ("insight", MAX_INSIGHTS, None)):
        group = sorted((node for node in nodes.values() if node["type"] == kind and not node.get("pinned")),
                       key=lambda node: (not node.get("archived"), node["score"], node["id"]))
        for node in group[:max(0, len(group) - limit)]:
            remove_node(graph, node["id"])
            if counter:
                report[counter] += 1
    for alias, target in list(graph["aliases"].items()):
        if resolve(graph, target) not in nodes:
            del graph["aliases"][alias]

    report.update(nodes=len(nodes), edges=len(graph["edges"]))
    history = graph["refine"].setdefault("history", [])
    history.append(report)
    del history[:-MAX_REFINE_LOG]
    graph["refine"].update(last_at=report["at"], last_seq=current)
    return report


def score(node, current):
    """Relevance in [0, ~2]: evidence-weighted confidence decayed by runs since last seen."""
    idle = max(0, current - int(node.get("last_seq", current)))
    recency = DECAY ** idle
    kind = node["type"]
    if kind == "insight":
        support = int(node.get("reinforced", 1)) + 2 * int(node.get("useful", 0)) + 2 * int(node.get("cited_done", 0))
        doubt = 3 * int(node.get("wrong", 0)) + int(node.get("cited_failed", 0))
        confidence = (support + 1) / (support + doubt + 2)
        value = confidence * recency * (1.6 if node.get("source") == "user" else 1.0)
        value *= 0.2 if node.get("archived") else 1.0
    elif kind in ("file", "area"):
        stats = node.get("stats", {})
        value = min(1.0, 0.25 + 0.15 * stats.get("attempts", 0)) * recency
    elif kind == "task":
        value = (0.6 if node.get("status") in FINISHED else 0.4) * recency
    elif kind in ("gate", "blocker"):
        value = min(1.0, 0.3 + 0.1 * int(node.get("uses", 0))) * recency
    else:
        value = 0.5 * recency
    return value * (2.0 if node.get("pinned") else 1.0)


# ---------------------------------------------------------------- queries

def neighbors(graph, node_id, adj=None):
    adj = adj or adjacency(graph)
    return [(edge, graph["nodes"][other]) for edge, other in adj.get(node_id, []) if other in graph["nodes"]]


def node_view(node, fields=("id", "type", "label", "kind", "status", "summary", "score", "stats", "pinned",
                            "archived", "stale", "source", "reinforced", "useful", "wrong", "cited_done",
                            "cited_failed", "area", "seconds", "uses", "outcome_note", "commit", "run_id",
                            "task_id", "outcome", "merged_from", "last_seq", "first_seq", "created_at")):
    return {key: node[key] for key in fields if node.get(key) not in (None, [], {})}


def search(graph, text, limit=15, types=None, include_archived=False):
    words = tokens(text)
    if not words:
        return []
    ranked = []
    for node in graph["nodes"].values():
        if types and node["type"] not in types or (node.get("archived") and not include_archived):
            continue
        haystack = tokens(" ".join(str(node.get(key, "")) for key in ("label", "summary", "outcome_note", "kind", "area")))
        hits = len(words & haystack)
        if hits:
            exact = 1.5 if str(text).lower() in str(node.get("label", "")).lower() else 1.0
            ranked.append((hits / len(words) * exact * (0.5 + float(node.get("score", 0.5))), node))
    ranked.sort(key=lambda item: (-item[0], item[1]["id"]))
    return [{**node_view(node), "match": round(value, 3)} for value, node in ranked[:limit]]


def context(graph, files=(), area=None, text=None, limit=8, repo=None):
    """Everything worth knowing before working on these files/area: the primary pre-task query."""
    nodes, adj = graph["nodes"], adjacency(graph)
    paths = [path for path in (safe_path(value, repo) for value in files) if path]
    seeds = []
    for path in paths:
        if "file:" + path in nodes:
            seeds.append("file:" + path)
        else:  # A directory or a new file: use known files beneath or beside it.
            prefix = path.rstrip("/") + "/"
            parent = path.rsplit("/", 1)[0] + "/" if "/" in path else None
            seeds.extend(key for key, node in nodes.items() if node["type"] == "file"
                         and (node["label"].startswith(prefix) or parent and node["label"].startswith(parent)))
    seeds = list(dict.fromkeys(seeds))[:40]
    if area and "area:" + area in nodes:
        seeds.append("area:" + area)
    insights, tasks, blockers, gates, related = {}, {}, {}, {}, {}

    def weigh(bucket, node, amount):
        bucket[node["id"]] = bucket.get(node["id"], 0) + amount * (0.5 + float(node.get("score", 0.5)))

    for seed in seeds:
        direct = 1.0 if seed.startswith("file:") else 0.5
        for edge, other in neighbors(graph, seed, adj):
            if other["type"] == "insight" and not other.get("archived"):
                weigh(insights, other, 2 * direct)
            elif other["type"] == "task":
                weigh(tasks, other, direct)
                for edge2, far in neighbors(graph, other["id"], adj):
                    if far["type"] == "blocker":
                        weigh(blockers, far, direct)
                    elif far["type"] == "gate":
                        weigh(gates, far, direct)
                    elif far["type"] == "insight" and not far.get("archived"):
                        weigh(insights, far, direct)
            elif other["type"] == "file" and edge["rel"] == "co_changed" and other["id"] not in seeds:
                weigh(related, other, int(edge.get("weight", 1)))
    if text:
        for hit in search(graph, text, limit, types=("insight",)):
            weigh(insights, nodes[hit["id"]], hit["match"])
    if not seeds and not text:
        raise GraphError("context needs --files, --area, or --text")

    def top(bucket, count=limit):
        return [node_view(nodes[key]) for key in sorted(bucket, key=lambda key: (-bucket[key], key))[:count]]

    file_stats = [{"file": nodes[seed]["label"], **nodes[seed].get("stats", {}), "stale": nodes[seed].get("stale")}
                  for seed in seeds if seed.startswith("file:") and nodes[seed].get("stats")]
    return {"seeds": seeds, "files": file_stats[:limit], "insights": top(insights),
            "past_tasks": top(tasks), "blockers": top(blockers, 4), "gates": top(gates, 4),
            "co_changed": top(related, 6)}


def hotspots(graph, limit=10):
    files = [node for node in graph["nodes"].values()
             if node["type"] == "file" and node.get("stats", {}).get("attempts", 0) >= 2 and not node.get("stale")]
    files.sort(key=lambda node: (node["stats"]["success"], -node["stats"]["attempts"], node["id"]))
    return [{"file": node["label"], "id": node["id"], **node["stats"]} for node in files[:limit]]


def shortest_path(graph, start, goal, max_depth=6):
    start, goal = resolve(graph, start), resolve(graph, goal)
    if start not in graph["nodes"] or goal not in graph["nodes"]:
        raise GraphError("both path endpoints must be existing node ids")
    adj, previous, queue = adjacency(graph), {start: None}, deque([(start, 0)])
    while queue:
        current, depth = queue.popleft()
        if current == goal:
            break
        if depth >= max_depth:
            continue
        for edge, other in adj.get(current, []):
            if other not in previous:
                previous[other] = (current, edge["rel"])
                queue.append((other, depth + 1))
    if goal not in previous:
        return []
    steps, current = [], goal
    while previous[current]:
        parent, rel = previous[current]
        steps.append({"from": parent, "rel": rel, "to": current, "label": graph["nodes"][current]["label"]})
        current = parent
    return list(reversed(steps))


def node_detail(graph, node_id, limit=200):
    node_id = resolve(graph, node_id)
    node = graph["nodes"].get(node_id)
    if node is None:
        raise GraphError(f"no graph node {node_id!r}")
    links = []
    for edge, other in neighbors(graph, node_id)[:limit]:
        links.append({"rel": edge["rel"], "direction": "out" if edge["src"] == node_id else "in",
                      "weight": edge.get("weight", 1), **node_view(other, ("id", "type", "label", "status", "score", "archived", "kind"))})
    links.sort(key=lambda row: (row["rel"], -float(row.get("score", 0)), row["id"]))
    return {"node": node_view(node), "links": links}


def brief(graph, script, repo_arg, max_chars=1800):
    """Compact, ranked knowledge for a cycle prompt. Ids let the agent cite what it used."""
    nodes = graph["nodes"]
    if not nodes:
        return ""
    parts = [f"Knowledge graph: {len(nodes)} nodes from {len(graph['runs'])} recorded runs. "
             f"Before each task query it: python3 {script} --repo {repo_arg} context --files PATH... --area AREA"]
    live = [node for node in nodes.values() if node["type"] == "insight" and not node.get("archived")
            and node.get("source") != "learning"]
    live.sort(key=lambda node: (not node.get("pinned"), node.get("source") != "user", -float(node.get("score", 0)), node["id"]))
    if live:
        lines = []
        for node in live[:8]:
            flags = [flag for flag, on in (("pinned", node.get("pinned")), ("from the user", node.get("source") == "user"),
                                           (f"x{node.get('reinforced')}", int(node.get("reinforced", 1)) > 1)) if on]
            about = [other["label"] for _, other in neighbors(graph, node["id"]) if other["type"] in ("file", "area", "gate")][:3]
            lines.append(f"- [{node['id']}] {node.get('kind', 'fact')}: {node.get('summary', node['label'])}"
                         + (f" (about {', '.join(about)})" if about else "") + (f" [{', '.join(flags)}]" if flags else ""))
        parts.append("Top knowledge (cite ids you rely on in the task's graph_refs):\n" + "\n".join(lines))
    risky = hotspots(graph, 5)
    if risky:
        parts.append("Files where past attempts often failed; read their context first:\n" + "\n".join(
            f"- {row['file']}: {row['done']}/{row['attempts']} verified" for row in risky if row["success"] < 0.6))
    gates = sorted((node for node in nodes.values() if node["type"] == "gate" and node.get("seconds")),
                   key=lambda node: node["seconds"])[:6]
    if gates:
        parts.append("Known gates and cost: " + ", ".join(f"{node['label']} {node['seconds']}s" for node in gates))
    text = "\n\n".join(part for part in parts if part.strip() and not part.endswith(":\n"))
    return text if len(text) <= max_chars else text[:max_chars].rsplit("\n", 1)[0]


def board_view(graph, limit=250, focus=None, depth=1, query=None, types=None, include_archived=False):
    """A bounded subgraph for the board: the strongest nodes, a node's neighborhood, or search hits."""
    nodes = graph["nodes"]
    limit = max(10, min(int(limit), 800))
    chosen = []
    if focus:
        focus = resolve(graph, focus)
        if focus not in nodes:
            raise GraphError("that node is no longer in the graph")
        adj, seen, frontier = adjacency(graph), {focus}, [focus]
        for _ in range(max(1, min(int(depth), 3))):
            following = []
            for current in frontier:
                for _, other in sorted(adj.get(current, []), key=lambda item: -float(nodes[item[1]].get("score", 0))):
                    if other not in seen and len(seen) < limit:
                        seen.add(other)
                        following.append(other)
            frontier = following
        chosen = list(seen)
    else:
        pool = [node for node in nodes.values() if (include_archived or not node.get("archived"))
                and (not types or node["type"] in types)]
        if query:
            hits = {row["id"] for row in search(graph, query, limit, types, include_archived)}
            adj = adjacency(graph)
            near = {other for hit in hits for _, other in adj.get(hit, [])}
            pool = [node for node in pool if node["id"] in hits or node["id"] in near]
        pool.sort(key=lambda node: (not node.get("pinned"), -float(node.get("score", 0)), node["id"]))
        chosen = [node["id"] for node in pool[:limit]]
    selected = set(chosen)
    edges = [{"src": edge["src"], "rel": edge["rel"], "dst": edge["dst"], "weight": edge.get("weight", 1)}
             for edge in graph["edges"].values() if edge["src"] in selected and edge["dst"] in selected]
    edges.extend(projected_edges(graph, selected))
    counts = {kind: sum(node["type"] == kind for node in nodes.values()) for kind in TYPES}
    return {"revision": graph.get("revision", 0), "updated_at": graph.get("updated_at"),
            "nodes": [node_view(nodes[key]) for key in chosen], "edges": edges[:limit * 4],
            "total_nodes": len(nodes), "total_edges": len(graph["edges"]), "counts": counts,
            "runs": graph["runs"][-12:], "refine": graph["refine"].get("history", [])[-8:],
            "hotspots": hotspots(graph, 8), "focus": focus, "truncated": len(chosen) < len(nodes)}


def projected_edges(graph, selected, fanout=8):
    """Connect shown nodes that share a hidden task, so hiding tasks keeps the picture connected."""
    direct = {(edge["src"], edge["dst"]) for edge in graph["edges"].values()}
    shared = {}
    for task_id, links in adjacency(graph).items():
        if task_id in selected or graph["nodes"].get(task_id, {}).get("type") != "task":
            continue
        shown = sorted({other for _, other in links if other in selected})[:fanout]
        for index, left in enumerate(shown):
            for right in shown[index + 1:]:
                if (left, right) not in direct and (right, left) not in direct:
                    shared[(left, right)] = shared.get((left, right), 0) + 1
    return [{"src": left, "rel": "via_task", "dst": right, "weight": weight}
            for (left, right), weight in sorted(shared.items(), key=lambda item: -item[1])]


def summary(graph):
    nodes = graph["nodes"]
    return {"revision": graph.get("revision", 0), "nodes": len(nodes), "edges": len(graph["edges"]),
            "runs": len(graph["runs"]), "insights": sum(node["type"] == "insight" and not node.get("archived") for node in nodes.values()),
            "last_refine": graph["refine"].get("last_at")}


# ---------------------------------------------------------------- CLI

def print_context(result):
    lines = []
    for row in result["files"]:
        lines.append(f"{row['file']}: {row['done']}/{row['attempts']} past attempts verified"
                     + (" (file no longer tracked)" if row.get("stale") else ""))
    for title, key, render in (
            ("Knowledge", "insights", lambda n: f"[{n['id']}] {n.get('kind', '')}: {n.get('summary', n['label'])}"),
            ("Past tasks here", "past_tasks", lambda n: f"[{n.get('status')}] {n['label']}" + (f" - {n['outcome_note']}" if n.get("outcome_note") else "")),
            ("Blockers seen here", "blockers", lambda n: n["label"]),
            ("Gates used here", "gates", lambda n: n["label"] + (f" ({n['seconds']}s)" if n.get("seconds") else "")),
            ("Files usually changed together", "co_changed", lambda n: n["label"])):
        if result[key]:
            lines.append(title + ":")
            lines.extend("- " + render(node) for node in result[key])
    return "\n".join(lines) or "The graph has nothing on these yet. Record what you learn with `note`."


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    sync_parser = sub.add_parser("sync", help="ingest the current run (and archived runs on first use)")
    sync_parser.add_argument("--history", action="store_true", help="re-ingest every archived run")
    sync_parser.add_argument("--refine", action="store_true", help="refine after ingesting")
    sub.add_parser("refine", help="merge duplicates, rescore, fade stale knowledge, bound size")
    brief_parser = sub.add_parser("brief", help="ranked knowledge for the next cycle")
    brief_parser.add_argument("--max-chars", type=int, default=1800)
    ctx = sub.add_parser("context", help="what is known about files/an area before working on them")
    ctx.add_argument("--files", nargs="*", default=[])
    ctx.add_argument("--area")
    ctx.add_argument("--text")
    ctx.add_argument("--limit", type=int, default=8)
    ctx.add_argument("--json", action="store_true")
    find = sub.add_parser("search", help="rank nodes by words")
    find.add_argument("text")
    find.add_argument("--type", action="append", choices=TYPES)
    find.add_argument("--limit", type=int, default=15)
    show = sub.add_parser("node", help="a node and its links")
    show.add_argument("id")
    path = sub.add_parser("path", help="how two nodes are connected")
    path.add_argument("start")
    path.add_argument("goal")
    hot = sub.add_parser("hotspots", help="files where verified success is lowest")
    hot.add_argument("--limit", type=int, default=10)
    note = sub.add_parser("note", help="record knowledge; repeats reinforce it")
    note.add_argument("--text", required=True)
    note.add_argument("--kind", choices=INSIGHT_KINDS, default="fact")
    note.add_argument("--about", nargs="*", default=[], help="file:PATH, area:NAME, gate:NAME, or a node id")
    connect = sub.add_parser("link", help="relate two nodes")
    connect.add_argument("src")
    connect.add_argument("rel", choices=USER_RELATIONS)
    connect.add_argument("dst")
    verdict = sub.add_parser("rate", help="say whether knowledge helped")
    verdict.add_argument("id")
    verdict.add_argument("verdict", choices=("useful", "wrong", "pin", "unpin", "archive", "restore"))
    sub.add_parser("stats", help="graph size and last refinement")
    args = parser.parse_args(argv)
    repo = Path(args.repo).expanduser().resolve()
    try:
        if args.action == "sync":
            result = sync(repo, include_history=True if args.history else None, refine_after=args.refine)
        elif args.action in ("refine", "note", "link", "rate"):
            with locked(repo) as graph:
                if args.action == "refine":
                    result = refine(graph, repo)
                elif args.action == "note":
                    run_id, _ = run_identity(repo / ".improve")
                    result = node_view(add_insight(graph, args.text, args.kind, "agent", args.about, run_id, repo))
                elif args.action == "link":
                    src, dst = target_id(graph, args.src, repo), target_id(graph, args.dst, repo)
                    result = link(graph, src, args.rel, dst, run_seq(graph))
                    if result is None:
                        raise GraphError("a node cannot link to itself")
                else:
                    result = node_view(rate(graph, args.id, args.verdict))
        else:
            graph = load(graph_path(repo), repair=False)
            if args.action == "brief":
                result = brief(graph, Path(__file__).resolve(), repo, args.max_chars) or "The knowledge graph is empty."
            elif args.action == "context":
                result = context(graph, args.files, args.area, args.text, args.limit, repo)
                if not args.json:
                    result = print_context(result)
            elif args.action == "search":
                result = search(graph, args.text, args.limit, args.type)
            elif args.action == "node":
                result = node_detail(graph, args.id)
            elif args.action == "path":
                result = shortest_path(graph, args.start, args.goal) or "No connection within six steps."
            elif args.action == "hotspots":
                result = hotspots(graph, args.limit)
            else:
                result = summary(graph)
        print(result if isinstance(result, str) else json.dumps(result, indent=2))
        return 0
    except (StateError, OSError, ValueError) as exc:
        print(f"improve-graph: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
