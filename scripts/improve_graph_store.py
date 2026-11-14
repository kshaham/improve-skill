"""SQLite persistence for the knowledge graph. Standard library only.

One database per repository, `.improve/graph.db`, in WAL mode so readers never block the
daemon and never see a half-written graph:

  node(id, type, body)            body is the node's canonical JSON
  edge(key, src, rel, dst, body)  indexed both ways for neighborhood loads
  meta(key, value)                revision, runs, aliases, refine/dream state, ...
  history(...)                    every removed node with its links and the reason, so a
                                  forgotten memory can be brought back exactly
  node_fts (FTS5, when built in)  pre-tokenized words, used only to find search candidates

A session loads the whole graph (daemon, board) or only the part it needs (agent CLI
calls), and writes back only the rows that changed. Refinement and dreaming checkpoint
`graph.backup.db`; a damaged database is set aside and restored from that checkpoint.
"""

import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
import zlib

DB_NAME = "graph.db"
BACKUP_NAME = "graph.backup.db"
LEGACY_NAME = "graph.json"
MAX_HISTORY = 20000
BUSY_TIMEOUT_SECONDS = 60
CHUNK = 500
STRUCTURE = ("nodes", "edges")
# "no such table" is not here: a missing graph table is caught by `Store.open`, and anything else
# missing (a newer or older optional table, a typo) is a bug to fix, not a store to discard.
DAMAGE_WORDS = ("not a database", "malformed", "disk image", "corrupt")
# Bumped whenever `indexer` output changes, so an index built by older code is rebuilt once.
INDEX_VERSION = 2
DAMAGED_KEEP = 3


def canonical(value):
    """The one serialization used for storage and change detection."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class StoreDamaged(Exception):
    """The database file cannot be read as a knowledge graph."""


class GraphConflict(ValueError):
    """A partial session tried to write a node it never loaded; nothing was written."""


def pack(value):
    """History records are written once and read rarely: store them compressed."""
    return sqlite3.Binary(zlib.compress(canonical(value).encode(), 6))


def unpack(value, empty):
    if value is None or value == "":
        return empty
    if isinstance(value, (bytes, memoryview)):
        return json.loads(zlib.decompress(bytes(value)))
    return json.loads(value)


def is_damage(exc):
    """True only for a database that cannot be read as a knowledge graph. A constraint
    violation, a programming error, or a busy database is a bug or a wait, never a reason
    to discard the live store for its checkpoint."""
    message = str(exc).lower()
    if any(word in message for word in DAMAGE_WORDS):
        return True
    if isinstance(exc, (sqlite3.OperationalError, sqlite3.IntegrityError, sqlite3.ProgrammingError,
                        sqlite3.DataError, sqlite3.InterfaceError)):
        return False
    return isinstance(exc, sqlite3.DatabaseError)


class GraphState(dict):
    """The graph as a plain dict (nodes, edges, meta keys) plus the session it came from.
    Attributes are not dict items, so JSON and equality see only the graph itself."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.store = None
        self.partial = False
        self.removed = {}
        self.journal = []       # curated writes of this session, journaled after commit
        self.replaying = False  # True while recovery re-applies journaled writes
        self.drained = None     # the pending-writes file this session applied
        self.newer = False      # read a graph written by a newer format


def paths(state):
    state = Path(state)
    return state / DB_NAME, state / BACKUP_NAME, state / LEGACY_NAME


def chunks(values, size=CHUNK):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


class Store:
    def __init__(self, path, indexer=None):
        self.path = Path(path)
        self.indexer = indexer
        self.con = None
        self.fts = False
        self.original = {"nodes": {}, "edges": {}, "meta": {}}
        self._forgotten = None
        self.last_write = {"nodes": 0, "edges": 0, "meta": 0, "deleted_nodes": 0, "deleted_edges": 0}

    # ------------------------------------------------------------ lifecycle

    def open(self, create=True):
        """Connect; False when the database does not exist and may not be created."""
        if not self.path.exists() and not create:
            return False
        if self.path.is_symlink():
            raise StoreDamaged(f"{self.path.name} must not be a symlink")
        existed = self.path.exists() and self.path.stat().st_size > 0
        self.con = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None,
                                   check_same_thread=False)
        try:
            self.con.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_SECONDS * 1000}")
            if existed:
                # Never adopt someone else's database: tables but no graph means damage, not a new graph.
                found = {row[0] for row in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if found and "meta" not in found:
                    raise StoreDamaged(f"{self.path.name} is a different database, not a knowledge graph")
            if create:
                # auto_vacuum only takes effect if set before WAL mode and the first table.
                self.con.execute("PRAGMA auto_vacuum=INCREMENTAL")
                if self.con.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                    self.con.execute("PRAGMA journal_mode=WAL")
                self.con.execute("PRAGMA synchronous=NORMAL")
                self.schema()
            tables = {row[0] for row in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"node", "edge", "meta", "history"} <= tables:
                raise StoreDamaged(f"{self.path.name} is not a knowledge graph")
            self.fts = "node_fts" in tables and self.fts_usable()
        except sqlite3.DatabaseError as exc:
            self.close()
            if is_damage(exc):
                raise StoreDamaged(str(exc)) from exc
            raise
        except StoreDamaged:
            self.close()
            raise
        return True

    def close(self):
        if self.con is not None:
            with contextlib.suppress(sqlite3.Error):
                self.con.close()
            self.con = None

    def schema(self):
        self.con.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS node(id TEXT PRIMARY KEY, type TEXT NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS node_type ON node(type);
            CREATE TABLE IF NOT EXISTS edge(key TEXT PRIMARY KEY, src TEXT NOT NULL, rel TEXT NOT NULL,
                                            dst TEXT NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS edge_src ON edge(src);
            CREATE INDEX IF NOT EXISTS edge_dst ON edge(dst);
            CREATE TABLE IF NOT EXISTS history(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, node_id TEXT NOT NULL, type TEXT, label TEXT,
                op TEXT, reason TEXT, at TEXT, revision INTEGER, tick INTEGER, body BLOB NOT NULL,
                edges BLOB NOT NULL, extra TEXT, restored_at TEXT);
            CREATE INDEX IF NOT EXISTS history_node ON history(node_id);
        """)
        with contextlib.suppress(sqlite3.OperationalError):  # Python builds without FTS5 search by scan.
            self.con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING "
                             "fts5(id UNINDEXED, words, tokenize=\"unicode61 tokenchars '_'\")")

    def fts_usable(self):
        try:
            self.con.execute("SELECT id FROM node_fts LIMIT 0").fetchall()
            return True
        except sqlite3.OperationalError:
            return False

    @contextlib.contextmanager
    def snapshot(self):
        """A consistent read across several statements."""
        if self.con.in_transaction:
            yield
            return
        self.con.execute("BEGIN")
        try:
            yield
        finally:
            if self.con.in_transaction:
                self.con.execute("COMMIT")

    def begin_write(self):
        self.con.execute("BEGIN IMMEDIATE")

    def commit(self):
        if self.con.in_transaction:
            self.con.execute("COMMIT")

    def rollback(self):
        if self.con is not None and self.con.in_transaction:
            with contextlib.suppress(sqlite3.Error):
                self.con.execute("ROLLBACK")

    # ------------------------------------------------------------ reads

    def read_meta(self):
        meta = {}
        for key, value in self.con.execute("SELECT key, value FROM meta"):
            self.original["meta"][key] = value
            meta[key] = json.loads(value)
        return meta

    def revision(self):
        row = self.con.execute("SELECT value FROM meta WHERE key='revision'").fetchone()
        return json.loads(row[0]) if row else 0

    def load_all(self, base):
        """Every node and edge, into `base` (a GraphState with meta defaults)."""
        with self.snapshot():
            base.update(self.read_meta())
            nodes, edges = {}, {}
            for node_id, body in self.con.execute("SELECT id, body FROM node"):
                self.original["nodes"][node_id] = body
                nodes[node_id] = json.loads(body)
            for key, body in self.con.execute("SELECT key, body FROM edge"):
                self.original["edges"][key] = body
                edges[key] = json.loads(body)
        base["nodes"], base["edges"] = nodes, edges
        base.partial = False
        return base

    def load_meta_only(self, base):
        base.update(self.read_meta())
        base["nodes"], base["edges"] = {}, {}
        base.partial = True
        return base

    def fetch(self, graph, node_ids):
        """Load these nodes (if they exist) and every edge between loaded nodes they touch."""
        wanted = [node_id for node_id in dict.fromkeys(node_ids) if node_id not in graph["nodes"]]
        fresh = []
        for part in chunks(wanted):
            marks = ",".join("?" * len(part))
            for node_id, body in self.con.execute(f"SELECT id, body FROM node WHERE id IN ({marks})", part):
                self.original["nodes"][node_id] = body
                graph["nodes"][node_id] = json.loads(body)
                fresh.append(node_id)
        loaded = graph["nodes"]
        for part in chunks(fresh):
            marks = ",".join("?" * len(part))
            for key, src, dst, body in self.con.execute(
                    f"SELECT key, src, dst, body FROM edge WHERE src IN ({marks}) UNION "
                    f"SELECT key, src, dst, body FROM edge WHERE dst IN ({marks})", part + part):
                if key not in graph["edges"] and src in loaded and dst in loaded:
                    self.original["edges"][key] = body
                    graph["edges"][key] = json.loads(body)
        return fresh

    def ids_of_type(self, node_type):
        return [row[0] for row in self.con.execute("SELECT id FROM node WHERE type=?", (node_type,))]

    def neighbor_ids(self, node_ids, types=None, rels=None):
        """Ids linked to these nodes, optionally only of some node types or through some relations."""
        found = set()
        node_filter = f" AND n.type IN ({','.join('?' * len(types))})" if types else ""
        rel_filter = f" AND e.rel IN ({','.join('?' * len(rels))})" if rels else ""
        extra = list(rels or ()) + list(types or ())
        for part in chunks(set(node_ids)):
            marks = ",".join("?" * len(part))
            query = (f"SELECT e.dst FROM edge e JOIN node n ON n.id = e.dst WHERE e.src IN ({marks}){rel_filter}{node_filter} "
                     f"UNION SELECT e.src FROM edge e JOIN node n ON n.id = e.src WHERE e.dst IN ({marks}){rel_filter}{node_filter}")
            found.update(row[0] for row in self.con.execute(query, part + extra + part + extra))
        return found

    def file_candidates(self, paths):
        """File ids a path can mean: itself, files below it (a directory), and files beside it (a
        new file). Range scans on the primary key, so no file list is ever loaded."""
        found = set()
        for path in paths:
            found.update(self.existing(["file:" + path]))
            for prefix in {path.rstrip("/") + "/", path.rsplit("/", 1)[0] + "/" if "/" in path else None} - {None}:
                low = "file:" + prefix
                high = low[:-1] + chr(ord("/") + 1)
                found.update(row[0] for row in self.con.execute(
                    "SELECT id FROM node WHERE id >= ? AND id < ?", (low, high)))
        return found

    def existing(self, node_ids):
        found = set()
        for part in chunks(set(node_ids)):
            marks = ",".join("?" * len(part))
            found.update(row[0] for row in self.con.execute(f"SELECT id FROM node WHERE id IN ({marks})", part))
        return found

    def search_candidates(self, words):
        """Ids of nodes sharing at least one indexed word, or None when no index can answer."""
        if not self.fts or not self.fts_ready() or not words:
            return None
        query = " OR ".join(f'"{word}"' for word in sorted(words))
        return {row[0] for row in self.con.execute("SELECT id FROM node_fts WHERE node_fts MATCH ?", (query,))}

    def fts_ready(self):
        row = self.con.execute("SELECT value FROM meta WHERE key='fts_ready'").fetchone()
        if not (row and json.loads(row[0])):
            return False
        version = self.con.execute("SELECT value FROM meta WHERE key='fts_version'").fetchone()
        return bool(version and json.loads(version[0]) == INDEX_VERSION)

    # ------------------------------------------------------------ writes

    def save(self, graph, revision_hook):
        """Write only what changed since load. Returns True when anything was written."""
        con, original = self.con, self.original
        nodes, edges = graph["nodes"], graph["edges"]
        changed_nodes = {}
        for node_id, node in nodes.items():
            body = canonical(node)
            if original["nodes"].get(node_id) != body:
                changed_nodes[node_id] = body
        deleted_nodes = [node_id for node_id in original["nodes"] if node_id not in nodes]
        changed_edges = {}
        for key, edge in edges.items():
            body = canonical(edge)
            if original["edges"].get(key) != body:
                changed_edges[key] = (edge, body)
        deleted_edges = [key for key in original["edges"] if key not in edges]
        if graph.partial:
            created = [node_id for node_id in changed_nodes if node_id not in original["nodes"]]
            clash = self.existing(created)
            if clash:
                raise GraphConflict(f"partial session would overwrite unloaded nodes: {sorted(clash)[:3]}")
        meta = {key: value for key, value in graph.items() if key not in STRUCTURE and not key.startswith("_")}
        changed_meta = {}
        for key, value in meta.items():
            body = canonical(value)
            if original["meta"].get(key) != body:
                changed_meta[key] = body
        self.last_write = {"nodes": len(changed_nodes), "edges": len(changed_edges), "meta": len(changed_meta),
                           "deleted_nodes": len(deleted_nodes), "deleted_edges": len(deleted_edges)}
        if not (changed_nodes or deleted_nodes or changed_edges or deleted_edges or changed_meta):
            return False
        revision_hook(graph)
        for key in ("revision", "updated_at"):
            changed_meta[key] = canonical(graph[key])
        self.last_write["meta"] = len(changed_meta)
        self.record_history(graph, deleted_nodes)
        for part in chunks(deleted_nodes):
            marks = ",".join("?" * len(part))
            con.execute(f"DELETE FROM node WHERE id IN ({marks})", part)
            con.execute(f"DELETE FROM edge WHERE src IN ({marks}) OR dst IN ({marks})", part + part)
            if self.fts:
                con.execute(f"DELETE FROM node_fts WHERE id IN ({marks})", part)
        for part in chunks(deleted_edges):
            con.execute(f"DELETE FROM edge WHERE key IN ({','.join('?' * len(part))})", part)
        con.executemany("INSERT INTO node(id, type, body) VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET "
                        "type=excluded.type, body=excluded.body",
                        [(node_id, nodes[node_id]["type"], body) for node_id, body in changed_nodes.items()])
        con.executemany("INSERT INTO edge(key, src, rel, dst, body) VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE "
                        "SET src=excluded.src, rel=excluded.rel, dst=excluded.dst, body=excluded.body",
                        [(key, edge["src"], edge["rel"], edge["dst"], body) for key, (edge, body) in changed_edges.items()])
        self.index(changed_nodes, nodes, original["nodes"])
        con.executemany("INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        list(changed_meta.items()))
        return True

    def index(self, changed, nodes, before=None):
        """Keep search words current; a change that leaves a node's words alone costs nothing."""
        if not changed:
            return
        if not self.fts:
            # This build cannot maintain the index; mark it stale so a capable one rebuilds it.
            self.con.execute("INSERT INTO meta(key, value) VALUES ('fts_ready', 'false') "
                             "ON CONFLICT(key) DO UPDATE SET value='false'")
            return
        if not self.fts_ready():
            self.rebuild_index()
            return
        before = before or {}
        words = {node_id: self.indexer(nodes[node_id]) for node_id in changed}
        stale = [node_id for node_id in changed
                 if node_id not in before or self.indexer(json.loads(before[node_id])) != words[node_id]]
        for part in chunks(stale):
            self.con.execute(f"DELETE FROM node_fts WHERE id IN ({','.join('?' * len(part))})", part)
        self.con.executemany("INSERT INTO node_fts(id, words) VALUES (?,?)", [(node_id, words[node_id]) for node_id in stale])

    def rebuild_index(self):
        self.con.execute("DELETE FROM node_fts")
        rows = ((node_id, self.indexer(json.loads(body))) for node_id, body in self.con.execute(
            "SELECT id, body FROM node").fetchall())
        self.con.executemany("INSERT INTO node_fts(id, words) VALUES (?,?)", rows)
        self.con.execute("INSERT INTO meta(key, value) VALUES ('fts_ready', 'true') "
                         "ON CONFLICT(key) DO UPDATE SET value='true'")
        self.con.execute("INSERT INTO meta(key, value) VALUES ('fts_version', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(INDEX_VERSION),))

    # ------------------------------------------------------------ history

    def record_history(self, graph, deleted):
        if not deleted:
            return
        removed = getattr(graph, "removed", {}) or {}
        revision, tick, at = graph.get("revision", 0), graph.get("tick", 0), graph.get("updated_at")
        rows = []
        for node_id in deleted:
            body = self.original["nodes"][node_id]
            node = json.loads(body)
            links = []
            for part in (node_id,):
                for (edge_body,) in self.con.execute("SELECT body FROM edge WHERE src=? UNION ALL "
                                                     "SELECT body FROM edge WHERE dst=?", (part, part)):
                    links.append(json.loads(edge_body))
            info = removed.get(node_id, {})
            rows.append((node_id, node.get("type"), str(node.get("label", ""))[:200], info.get("op", "removed"),
                         info.get("reason"), at, revision, tick, pack(node), pack(links),
                         canonical(info.get("extra") or {})))
        self.con.executemany("INSERT INTO history(node_id, type, label, op, reason, at, revision, tick, body, edges, "
                             "extra) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.con.execute("DELETE FROM history WHERE seq <= (SELECT max(seq) FROM history) - ?", (MAX_HISTORY,))
        if self._forgotten is not None:
            self._forgotten.update(deleted)

    def forgotten_ids(self):
        """Ids with a removal that has not been restored (cached for the session)."""
        if self._forgotten is None:
            self._forgotten = {row[0] for row in self.con.execute(
                "SELECT DISTINCT node_id FROM history WHERE restored_at IS NULL")}
        return self._forgotten

    def latest_forgotten(self, node_id):
        row = self.con.execute("SELECT seq, node_id, type, label, op, reason, at, revision, tick, body, edges, extra "
                               "FROM history WHERE node_id=? AND restored_at IS NULL ORDER BY seq DESC LIMIT 1",
                               (node_id,)).fetchone()
        return self.history_row(row) if row else None

    def forgotten_insights(self):
        """The latest unrestored removal of each insight, for recall by text."""
        rows = self.con.execute(
            "SELECT h.seq, h.node_id, h.body FROM history h JOIN (SELECT node_id, max(seq) AS seq FROM history "
            "WHERE type='insight' AND restored_at IS NULL GROUP BY node_id) latest ON latest.seq=h.seq").fetchall()
        return [(seq, node_id, unpack(body, {})) for seq, node_id, body in rows]

    def history(self, limit=50, offset=0, types=None, query=None, include_restored=False, node_id=None):
        clauses, params = [], []
        if not include_restored:
            clauses.append("restored_at IS NULL")
        if types:
            clauses.append(f"type IN ({','.join('?' * len(types))})")
            params.extend(types)
        if query:
            # `%`, `_`, and `\` in the words are literal, not wildcards.
            pattern = "%" + re.sub(r"([%_\\])", r"\\\1", query.lower()) + "%"
            clauses.append("(lower(label) LIKE ? ESCAPE '\\' OR lower(reason) LIKE ? ESCAPE '\\' "
                           "OR lower(node_id) LIKE ? ESCAPE '\\')")
            params.extend([pattern] * 3)
        if node_id:
            clauses.append("node_id = ?")
            params.append(node_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        total = self.con.execute(f"SELECT count(*) FROM history {where}", params).fetchone()[0]
        rows = self.con.execute(
            "SELECT seq, node_id, type, label, op, reason, at, revision, tick, body, edges, extra, restored_at "
            f"FROM history {where} ORDER BY seq DESC LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
        return total, [self.history_row(row) for row in rows]

    @staticmethod
    def history_row(row):
        names = ("seq", "node_id", "type", "label", "op", "reason", "at", "revision", "tick", "body", "edges",
                 "extra", "restored_at")
        record = dict(zip(names, row))
        for key, empty in (("body", {}), ("edges", []), ("extra", {})):
            record[key] = unpack(record.get(key), empty)
        return record

    def mark_restored(self, seq, at):
        node_id = self.con.execute("SELECT node_id FROM history WHERE seq=?", (seq,)).fetchone()
        # Older removals of the same node are superseded by this restore as well.
        if node_id:
            self.con.execute("UPDATE history SET restored_at=? WHERE node_id=? AND restored_at IS NULL", (at, node_id[0]))
            if self._forgotten is not None:
                self._forgotten.discard(node_id[0])

    def history_count(self):
        return self.con.execute("SELECT count(*) FROM history").fetchone()[0]

    # ------------------------------------------------------------ maintenance

    def integrity(self):
        """Remove rows that can no longer be valid; returns how many."""
        removed = self.con.execute("DELETE FROM edge WHERE src NOT IN (SELECT id FROM node) "
                                   "OR dst NOT IN (SELECT id FROM node)").rowcount
        if self.fts and self.fts_ready():
            removed += self.con.execute("DELETE FROM node_fts WHERE id NOT IN (SELECT id FROM node)").rowcount
        return removed

    def checkpoint(self):
        """Return freed pages to the disk and fold the WAL back in (outside a transaction).
        A database created before incremental vacuum is converted once by a full VACUUM."""
        with contextlib.suppress(sqlite3.Error):
            if self.con.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
                self.con.execute("PRAGMA auto_vacuum=INCREMENTAL")
                self.con.execute("VACUUM")
            self.con.execute("PRAGMA incremental_vacuum").fetchall()
            self.con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()

    def backup(self, destination):
        destination = Path(destination)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.unlink(missing_ok=True)
        target = sqlite3.connect(str(temporary))
        try:
            self.con.backup(target)
        finally:
            target.close()
        os.replace(temporary, destination)

    def size_bytes(self):
        total = 0
        for suffix in ("", "-wal"):
            with contextlib.suppress(OSError):
                total += os.path.getsize(str(self.path) + suffix)
        return total


def set_aside(db_path):
    """Move a damaged database (and its WAL files) out of the way; returns the new path.
    The newest is `graph.damaged.db`; earlier incidents are kept as `graph.damaged.<stamp>.db`
    (the newest few), so a second incident does not erase the evidence of the first."""
    db_path = Path(db_path)
    damaged = db_path.with_name("graph.damaged.db")
    if damaged.exists():
        moment = time.time_ns()
        older = db_path.with_name(f"graph.damaged.{time.strftime('%Y%m%dT%H%M%S', time.gmtime(moment / 1e9))}"
                                  f"-{moment % 1_000_000_000:09d}.db")
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(damaged) + suffix)
            if source.exists():
                os.replace(source, Path(str(older) + suffix))
        kept = sorted(db_path.parent.glob("graph.damaged.*.db"))
        for stale in kept[:max(0, len(kept) - DAMAGED_KEEP)]:
            for suffix in ("", "-wal", "-shm"):
                Path(str(stale) + suffix).unlink(missing_ok=True)
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(db_path) + suffix)
        if source.exists():
            os.replace(source, Path(str(damaged) + suffix))
    return damaged


def restore_backup(db_path, backup_path):
    """Copy a readable backup into place; False when there is none.

    The copy is verified, not the original: the checkpoint is a WAL-mode database, and a
    read-only open of one fails on older SQLite builds when no `-shm` file exists, which used
    to turn a good checkpoint into a rebuild from nothing. Checking the copy also proves the
    exact file that is installed."""
    backup_path = Path(backup_path)
    if not backup_path.is_file() or backup_path.is_symlink():
        return False
    temporary = Path(db_path).with_name("graph.restoring.db")
    for suffix in ("", "-wal", "-shm"):
        Path(str(temporary) + suffix).unlink(missing_ok=True)
    shutil.copyfile(backup_path, temporary)
    try:
        probe = sqlite3.connect(str(temporary))
        try:
            ok = probe.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            tables = {row[0] for row in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            probe.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        finally:
            probe.close()
    except sqlite3.DatabaseError:
        ok, tables = False, set()
    if not ok or not {"node", "edge", "meta", "history"} <= tables:
        for suffix in ("", "-wal", "-shm"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)
        return False
    # A write-ahead log left by the old database would be replayed onto the restored one.
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
        Path(str(temporary) + suffix).unlink(missing_ok=True)
    os.replace(temporary, db_path)
    return True
