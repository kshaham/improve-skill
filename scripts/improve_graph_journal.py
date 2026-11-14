"""Side files that keep curated knowledge safe and the graph responsive. Standard library only.

Three append-only JSONL files live beside `graph.db` in `.improve/`:

  graph-writes.jsonl   every curated write (agent and user notes, links, ratings, rejected
                       links, restores, consolidations), numbered by `meta.journal_seq`.
                       A checkpoint (`graph.backup.db`) truncates it; after a recovery from
                       the checkpoint, the writes newer than it are replayed, so curated
                       knowledge is not lost the way ledger-derived data never is.
  graph-pending.jsonl  writes an agent made while another process held the graph. They are
                       queued instead of failing and applied by the next writer session
                       (Graphify's pending-changes drain), each exactly once.
  graph-reads.jsonl    which knowledge `context`, `search`, and `node` returned. The daemon's
                       sync folds it in, so knowledge that is read and followed stays alive,
                       and the ledger can tell whether a task's files were looked up.
"""

import contextlib
import fcntl
import json
import os
from pathlib import Path
import time
import uuid

WRITES = "graph-writes.jsonl"
PENDING = "graph-pending.jsonl"
READS = "graph-reads.jsonl"
READS_HISTORY = "graph-reads.history.jsonl"
RECOVERING = "graph.recovering"
MAX_LINE = 16 * 1024
MAX_READS_BYTES = 2 * 1024 * 1024
READS_KEEP_SECONDS = 2 * 3600
MAX_APPLIED = 400
WRITE_OPS = ("note", "link", "rate", "unlink", "restore", "consolidate")


class JournalError(ValueError):
    """A side file could not be written; the caller reports it like any graph error."""


@contextlib.contextmanager
def side_lock(path):
    """A short exclusive lock for one side file (appends and the drain's rename take it), so
    no append can land in a file after the drain has renamed it away and read it."""
    lock = Path(str(path) + ".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def take_for_drain(path):
    """Rename a side file aside for draining (under its lock) and return every file waiting to
    be drained, oldest first: this one and any left by a drain that did not finish."""
    path = Path(path)
    if path.is_file() and not path.is_symlink() and path.stat().st_size > 0:
        with side_lock(path):
            if path.is_file() and path.stat().st_size > 0:
                os.replace(path, path.with_name(f"{path.name}.{time.time_ns():020d}.draining"))
    return sorted(candidate for candidate in path.parent.glob(f"{path.name}.*.draining")
                  if candidate.is_file() and not candidate.is_symlink())


def append_locked(path, rows):
    with side_lock(path):
        append_lines(path, rows)


def append_lines(path, rows):
    """Append whole lines with one write each, so concurrent appenders never interleave."""
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        for row in rows:
            # ASCII escapes: a lone surrogate from undecodable argv bytes must not make the write
            # (and so the journal entry) vanish.
            line = (json.dumps(row, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
            if len(line) > MAX_LINE:
                raise JournalError(f"{path.name}: entry over {MAX_LINE} bytes")
            os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def read_lines(path):
    """Rows of a JSONL side file; damaged lines are skipped (the file is advisory)."""
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        return []
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        with contextlib.suppress(ValueError):
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    return rows


def rewrite(path, rows):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w") as stream:
        for row in rows:
            stream.write(json.dumps(row, separators=(",", ":"), ensure_ascii=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def state_dir(db_path):
    return Path(db_path).parent


# ---------------------------------------------------------------- write journal

def record(graph, op, **args):
    """Queue a curated write for the journal; written after the session commits."""
    if getattr(graph, "replaying", False):
        return
    entries = getattr(graph, "journal", None)
    if entries is not None:
        entries.append({"op": op, "args": {key: value for key, value in args.items() if value is not None},
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})


def number(graph):
    """Give queued entries sequence numbers (stored in meta in the same transaction)."""
    for entry in getattr(graph, "journal", None) or []:
        graph["journal_seq"] = int(graph.get("journal_seq", 0) or 0) + 1
        entry["seq"] = graph["journal_seq"]


def flush(graph, db_path):
    entries = getattr(graph, "journal", None) or []
    if entries:
        append_lines(state_dir(db_path) / WRITES, entries)
        graph.journal = []


def truncate_after_checkpoint(db_path, seq):
    """The checkpoint now holds everything up to `seq`; keep only what is newer."""
    path = state_dir(db_path) / WRITES
    if not path.exists():
        return
    newer = [row for row in read_lines(path) if int(row.get("seq", 0) or 0) > int(seq or 0)]
    rewrite(path, newer)


def replay(graph, db_path, repo):
    """After a recovery, re-apply the curated writes the checkpoint does not contain."""
    since = int(graph.get("journal_seq", 0) or 0)
    rows = sorted((row for row in read_lines(state_dir(db_path) / WRITES) if int(row.get("seq", 0) or 0) > since),
                  key=lambda row: int(row["seq"]))
    applied = 0
    graph.replaying = True
    try:
        for row in rows:
            if apply_quietly(graph, row, repo):
                applied += 1
            graph["journal_seq"] = max(int(graph.get("journal_seq", 0) or 0), int(row["seq"]))
    finally:
        graph.replaying = False
    return applied


# ---------------------------------------------------------------- pending writes

def queue_pending(repo, op, args):
    entry = {"id": uuid.uuid4().hex, "op": op, "args": args, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    append_locked(Path(repo) / ".improve" / PENDING, [entry])
    return entry


def drain_pending(graph, db_path, repo):
    """Apply queued writes once each. The queue is renamed aside first, so writes queued while
    this session runs go to a fresh file and are applied by the next writer; the drained
    files are deleted only after this session commits (`finish_drains`). Applied ids are kept
    in meta, so a crash between commit and delete applies nothing twice."""
    files = take_for_drain(state_dir(db_path) / PENDING)
    if not files:
        return 0
    done = list(graph.get("pending_applied") or [])
    seen = set(done)
    applied = 0
    for path in files:
        for row in read_lines(path):
            key = str(row.get("id") or "")
            if key in seen:
                continue
            if apply_quietly(graph, row, repo, journal=True):
                applied += 1
            seen.add(key)
            done.append(key)
    graph["pending_applied"] = done[-MAX_APPLIED:]
    graph.drained = list(getattr(graph, "drained", None) or []) + files
    return applied


def finish_drains(graph):
    """After commit: drained side files are done. Reads keep a short history for the ledger."""
    for path in getattr(graph, "drained", None) or []:
        if path.name.startswith(READS):
            with contextlib.suppress(OSError, JournalError):
                keep_read_history(path)
        with contextlib.suppress(OSError):
            path.unlink()
    graph.drained = []


clear_drained = finish_drains  # Older name, kept for callers.


def apply_quietly(graph, row, repo, journal=False):
    """Apply one recorded write; a write that no longer applies (its node is gone, its proposal
    expired) is skipped, never allowed to stop recovery or a drain."""
    from improve_graph import GraphError
    try:
        apply_write(graph, row.get("op"), row.get("args") or {}, repo, journal=journal)
        return True
    except (GraphError, KeyError, TypeError, ValueError):
        return False


def apply_write(graph, op, args, repo, journal=True):
    """The one implementation of every curated write, shared by the CLI, the board, journal
    replay, and the pending queue. Returns what the write produced."""
    import improve_graph as graph_module
    from improve_dream import consolidate, reject_link

    if op not in WRITE_OPS:
        raise graph_module.GraphError(f"unknown write {op!r}")
    if graph.get("nodes") is not None and getattr(graph, "partial", False) and op in ("restore", "consolidate", "unlink"):
        load_everything(graph)
    source = args.get("source", "agent")
    if op == "note":
        result = graph_module.add_insight(graph, args["text"], args.get("kind", "fact"), source,
                                          args.get("about") or [], args.get("run_id"), repo)
    elif op == "link":
        graph_module.fetch(graph, graph_module.candidate_ids(graph, args["src"], repo)
                           + graph_module.candidate_ids(graph, args["dst"], repo))
        src = graph_module.target_id(graph, args["src"], repo)
        dst = graph_module.target_id(graph, args["dst"], repo)
        result = graph_module.link(graph, src, args["rel"], dst, graph_module.run_seq(graph))
        if result is None:
            raise graph_module.GraphError("a node cannot link to itself")
    elif op == "rate":
        graph_module.fetch(graph, [graph_module.resolve(graph, args["id"])])
        result = graph_module.rate(graph, args["id"], args["verdict"], source)
    elif op == "unlink":
        result = reject_link(graph, args["src"], args["rel"], args["dst"], by=args.get("by", source),
                             reason=args.get("reason"))
    elif op == "restore":
        result = graph_module.restore_node(graph, args["id"], args.get("reason", "needed again"), source=source)
    else:
        result = consolidate(graph, args["proposal"], args["text"], args.get("kind", "technique"),
                             args.get("consume") or [], source)
    if journal:
        record(graph, op, **args)
    return result


def load_everything(graph):
    store = getattr(graph, "store", None)
    if store is not None and getattr(graph, "partial", False):
        store.fetch(graph, [row[0] for row in store.con.execute("SELECT id FROM node")])
        graph.partial = False


# ---------------------------------------------------------------- reads

def record_reads(repo, ids, files=(), command="context"):
    """Best effort: a read-only query notes what it returned. Never raises."""
    ids = [str(node_id) for node_id in dict.fromkeys(ids) if str(node_id).startswith("insight:")][:40]
    files = [str(path) for path in dict.fromkeys(files)][:40]
    if not ids and not files:
        return
    path = Path(repo) / ".improve" / READS
    with contextlib.suppress(OSError, JournalError):
        if path.exists() and path.stat().st_size > MAX_READS_BYTES:
            return
        append_locked(path, [{"at": int(time.time()), "cmd": command, "ids": ids, "files": files}])


def drain_reads(graph, repo, touch):
    """Refresh knowledge that queries returned since the last sync. The reads file is renamed
    aside first (appends during the sync start a fresh file), every row is applied exactly
    once, and after commit the recent rows move to a short history the ledger's hint reads."""
    files = take_for_drain(Path(repo) / ".improve" / READS)
    refreshed = 0
    for path in files:
        for row in read_lines(path):
            for node_id in row.get("ids") or []:
                node = graph["nodes"].get(node_id)
                if node is not None and node.get("type") == "insight":
                    touch(graph, node)
                    node["retrieved"] = int(node.get("retrieved", 0)) + 1
                    refreshed += 1
    graph.pop("reads_applied_at", None)
    graph.drained = list(getattr(graph, "drained", None) or []) + files
    return refreshed


def keep_read_history(drained):
    """Fold a drained reads file into `graph-reads.history.jsonl`, keeping READS_KEEP_SECONDS."""
    history = drained.with_name(READS_HISTORY)
    cutoff = int(time.time()) - READS_KEEP_SECONDS
    rows = [row for row in read_lines(history) + read_lines(drained) if int(row.get("at", 0) or 0) >= cutoff]
    rewrite(history, rows[-5000:])


def files_read_since(repo, seconds=3600):
    """Files that `context` queries covered recently (for the ledger's advisory hint): rows
    not yet drained, rows being drained, and the recent history."""
    state = Path(repo) / ".improve"
    cutoff = int(time.time()) - seconds
    found = set()
    sources = [state / READS, state / READS_HISTORY, *sorted(state.glob(f"{READS}.*.draining"))]
    for source in sources:
        for row in read_lines(source):
            if int(row.get("at", 0) or 0) >= cutoff:
                found.update(str(path) for path in row.get("files") or [])
    return found
