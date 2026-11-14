"""Knowledge graph of improvement sessions: what was tried, where, and what was learned.

Standard library only. One SQLite store per repository, `.improve/graph.db` (see
improve_graph_store.py), which survives `--new-run` archival like learning.json. The
supervisor ingests the live run after every cycle, refines and dreams on a schedule
(improve_dream.py), and keeps every removed node in a history so it can be restored.
Agents query it before work and add what they discover. See
references/knowledge-graph.md for the query recipes.

Node ids are typed and stable:  run:<id>  task:<run>:<id>  area:<name>  file:<path>
gate:<name>  blocker:<key>  insight:<hash>.
"""

import contextlib
from collections import deque
import copy
import fcntl
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import sqlite3
import stat
import subprocess
import sys

from improve_graph_store import GraphState, Store, StoreDamaged, is_damage, restore_backup, set_aside
from improve_runtime import StateError, epoch, read_json, stamp
import improve_graph_journal as journal

VERSION = 1
TYPES = ("run", "task", "area", "file", "gate", "blocker", "insight")
INSIGHT_KINDS = ("lesson", "pitfall", "technique", "convention", "decision", "fact")
# Relations an agent or user may add. Structural relations are written only by ingest/refine.
USER_RELATIONS = ("about", "relates_to", "causes", "fixes", "avoids", "depends_on", "supersedes", "used_in")
STRUCTURAL = ("in_run", "in_area", "touches", "verified_by", "blocked_by", "co_changed")
# Links that dreaming infers from evidence; each carries confidence, evidence, and its rule.
DREAM_RELATIONS = ("applies_to", "co_cited", "similar_to", "affects", "contradicts")
RELATIONS = USER_RELATIONS + STRUCTURAL + DREAM_RELATIONS
SEARCH_FIELDS = ("label", "summary", "outcome_note", "kind", "area")
PROTECT_TICKS = 6
MAX_LABEL = 200
MAX_SUMMARY = 600
MAX_FILES_PER_TASK = 20
MAX_TASKS = 1500
MAX_FILES = 1500
MAX_INSIGHTS = 400
MAX_CO_CHANGE = 600
MAX_CO_BASE = 2400
MAX_REFINE_LOG = 30
MAX_BLOCKERS = 300
MAX_ALIASES = 2000
RENAME_HISTORY = 300
# Ledger-derived links of a task; ingest replaces them so the graph follows ledger edits.
TASK_RELATIONS = ("in_run", "in_area", "touches", "verified_by", "blocked_by")
MERGE_SIMILARITY = 0.75
DECAY = 0.9
FADE_SCORE = 0.2
FADE_AFTER_RUNS = 8
FINISHED = ("done", "rejected", "blocked")
# Knowledge the user or the learning store wrote is steering: automatic maintenance never
# archives, caps, or forgets it. Agents may rate it; only a user may archive it.
PROTECTED_SOURCES = ("user", "learning")
# Whose wording wins when two sources record the same knowledge.
SOURCE_RANK = {"user": 2, "learning": 1}
FINGERPRINT_MAX_BYTES = 4 * 1024 * 1024
MAX_FINGERPRINTS = 8
LOCK_WAIT_SECONDS = 30
# How much a past task's outcome says about the knowledge it cited, in `context`.
OUTCOME_WEIGHT = {"done": 1.0, "open": 0.6, "ready": 0.6, "in_progress": 0.6, "blocked": 0.6, "rejected": 0.35}
HUB_RELATIONS = ("in_run", "in_area")
HUB_MIN_DEGREE = 50
TIER_ORDER = ("inferred", "recorded", "observed")
BRIEF_NOTES = 8
BRIEF_NOTE_CHARS = 220
# A SHA-like run of hex letters contains a digit; "defaced" or "accede" is a word, not an id.
HEX_RUN = r"(?=[0-9a-f]*\d)[0-9a-f]{7,40}"
STOP_WORDS = frozenset("a an and are as at be by for from has in is it its of on or that the this to was were when with".split())

_cache = {}


class GraphError(StateError):
    """Raised for invalid graph input; the message is safe to show a user."""


class GraphAmbiguous(GraphError):
    """A name matches several things; the caller prints the candidates (CLI exit 4)."""

    def __init__(self, message, candidates):
        super().__init__(message)
        self.candidates = candidates


class GraphNotFound(GraphError):
    """Nothing by that name (CLI exit 5)."""


class GraphBusy(GraphError):
    """Another writer holds the graph; the caller may retry later (CLI exit 3)."""


def lock_wait_seconds():
    value = os.environ.get("IMPROVE_GRAPH_LOCK_WAIT", str(LOCK_WAIT_SECONDS))
    return float(value) if re.fullmatch(r"\d+(\.\d+)?", value) else float(LOCK_WAIT_SECONDS)


def protected(node, tick=None):
    """True when automatic maintenance must leave this node alone."""
    if node.get("pinned") or node.get("source") in PROTECTED_SOURCES:
        return True
    return tick is not None and int(node.get("protected_until_tick", -1)) > tick


def graph_path(repo):
    return Path(repo) / ".improve" / "graph.db"


def backup_path(path):
    return Path(path).with_name("graph.backup.db")


def damaged_path(path):
    return Path(path).with_name("graph.damaged.db")


def legacy_path(path):
    return Path(path).with_name("graph.json")


def empty_graph():
    return {"version": VERSION, "revision": 0, "nodes": {}, "edges": {}, "runs": [], "aliases": {}, "lessons": {},
            "refine": {"history": []}, "tick": 0, "co_base": {},
            "dream": {"history": [], "proposals": [], "rejected": {}, "dropped": [], "gaps": []}}


def fill_defaults(graph, partial=False):
    """Repair the top-level shape in place; a full load also drops rows that cannot be valid."""
    for key, default in empty_graph().items():
        if key in ("nodes", "edges"):
            continue
        if not isinstance(graph.get(key), type(default)) or isinstance(graph.get(key), bool):
            graph[key] = copy.deepcopy(default)
    for key, default in empty_graph()["dream"].items():
        if not isinstance(graph["dream"].get(key), type(default)):
            graph["dream"][key] = copy.deepcopy(default)
    if not isinstance(graph.get("nodes"), dict):
        graph["nodes"] = {}
    if not isinstance(graph.get("edges"), dict):
        graph["edges"] = {}
    if partial:
        return graph
    graph["nodes"] = {key: node for key, node in graph["nodes"].items()
                      if isinstance(node, dict) and node.get("type") in TYPES and node.get("id") == key}
    graph["edges"] = {key: edge for key, edge in graph["edges"].items()
                      if isinstance(edge, dict) and edge.get("src") in graph["nodes"]
                      and edge.get("dst") in graph["nodes"] and edge.get("rel") in RELATIONS}
    return graph


def search_words(node):
    """Words a node can be found by; the same tokens `search` ranks with, so an index of
    them returns exactly the nodes a full scan would."""
    return " ".join(sorted(node_tokens(node)))


def node_tokens(node):
    return tokens(" ".join(str(node.get(key, "")) for key in SEARCH_FIELDS))


def parse_legacy(path):
    value = read_json(path)
    if not isinstance(value.get("nodes"), dict) or not isinstance(value.get("edges"), dict):
        raise StateError(f"{path.name} lacks nodes/edges objects")
    return value


def legacy_graph(path):
    try:
        return fill_defaults(GraphState(parse_legacy(path)))
    except (StateError, OSError, ValueError, RecursionError):
        return fill_defaults(GraphState(empty_graph()))


def recover(path):
    """Set a damaged database aside and fall back to the last refined checkpoint. A marker file
    stays until the journal replay that follows has committed, so a failure in between is
    retried by the next writer instead of silently losing the newer curated writes."""
    set_aside(path)
    source = "backup" if restore_backup(path, backup_path(path)) else "rebuild"
    mark_recovering(path, source)
    return source


def recovering_marker(path):
    return Path(path).with_name(journal.RECOVERING)


def mark_recovering(path, detail):
    marker = recovering_marker(path)
    temporary = marker.with_name(marker.name + ".tmp")
    temporary.write_text(json.dumps({"at": stamp(), "detail": detail}))
    os.replace(temporary, marker)


def load(path, repair=True):
    """Read the whole graph. A damaged store never blocks work: with `repair` it is set
    aside and restored from the last checkpoint (ledgers re-fill anything newer); without
    it, readers see an empty graph and change nothing."""
    path = Path(path)
    if path.suffix == ".json":
        return legacy_graph(path) if path.exists() else fill_defaults(GraphState(empty_graph()))
    if not path.exists():
        legacy = legacy_path(path)
        return legacy_graph(legacy) if legacy.exists() else fill_defaults(GraphState(empty_graph()))
    store = Store(path, search_words)
    try:
        if not store.open(create=False):
            return fill_defaults(GraphState(empty_graph()))
        return fill_defaults(store.load_all(GraphState()))
    except (StoreDamaged, sqlite3.DatabaseError) as exc:
        if isinstance(exc, sqlite3.DatabaseError) and not is_damage(exc):
            raise
        if repair and path.parent.name == ".improve":
            store.close()
            with locked(path.parent.parent):
                pass
            return load(path, repair=False)
        return fill_defaults(GraphState(empty_graph()))
    finally:
        store.close()


def load_cached(path):
    """Whole-graph read for a polling board: reuse the parsed graph until the revision changes."""
    path = Path(path)
    store = Store(path, search_words)
    try:
        if not store.open(create=False):
            return load(path, repair=False)
        key = (str(path), path.stat().st_ino, store.revision())
    except (StoreDamaged, sqlite3.DatabaseError, OSError):
        return fill_defaults(GraphState(empty_graph()))
    finally:
        store.close()
    cached = _cache.get(str(path))
    if cached and cached[0] == key:
        return cached[1]
    graph = load(path, repair=False)
    graph.frozen = True  # Shared by board requests: never mutated, so derived views may be cached.
    _cache[str(path)] = (key, graph)
    return graph


def bump_revision(graph):
    graph["revision"] = int(graph.get("revision", 0)) + 1
    graph["updated_at"] = stamp()


def state_paths(repo):
    state = Path(repo) / ".improve"
    if state.is_symlink():
        raise GraphError(".improve must be a directory inside the repository")
    state.mkdir(exist_ok=True)
    path = graph_path(repo)
    for candidate in (path, backup_path(path)):
        if candidate.is_symlink():
            raise GraphError(f"{candidate.name} must not be a symlink")
    return path


def migrate_legacy(store, path):
    """Import a graph.json from before the SQLite store, once."""
    legacy = legacy_path(path)
    if not legacy.is_file() or legacy.is_symlink() or store.con.execute("SELECT count(*) FROM node").fetchone()[0]:
        return False
    try:
        value = parse_legacy(legacy)
    except (StateError, OSError, ValueError, RecursionError):
        return False
    graph = fill_defaults(GraphState(value))
    graph.store = store
    store.begin_write()
    try:
        store.save(graph, lambda item: item.update(updated_at=stamp()))
        store.commit()
    except BaseException:
        store.rollback()
        raise
    legacy.replace(legacy.with_name("graph.json.migrated"))
    return True


@contextlib.contextmanager
def locked(repo, partial=False):
    """Exclusive read-modify-write session shared by the daemon, agents, and the board.
    `partial` loads only metadata; callers fetch the nodes they need (agent CLI calls).
    Only changed rows are written, in one transaction; an exception writes nothing."""
    path = state_paths(repo)
    with writer_queue(path):
        with session(path, partial) as graph:
            yield graph


@contextlib.contextmanager
def writer_queue(path):
    """Writers wait their turn on a file lock: SQLite's own busy-retry is not fair, and a busy
    daemon could otherwise starve an agent's or the board's write. Readers never wait. The
    wait is bounded (IMPROVE_GRAPH_LOCK_WAIT): a writer hung elsewhere must not hang an
    agent, so after the bound the caller gets GraphBusy and may retry later."""
    fd = os.open(path.with_name("graph.lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    owner_closes = True
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise GraphError("graph.lock must be a regular file")
        if not acquire_lock(fd, lock_wait_seconds()):
            owner_closes = False  # The waiting thread releases and closes once it gets the lock.
            raise GraphBusy(f"another process has held the knowledge graph for over {lock_wait_seconds():.0f}s; "
                            "retry later (this never blocks a task)")
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        if owner_closes:
            os.close(fd)


def acquire_lock(fd, timeout):
    """A blocking flock with a deadline. flock does not promise first-come order, but a
    blocked waiter is woken when the lock is released, unlike SQLite's busy retry, which can
    starve a writer. The blocking call runs in a helper thread; if the deadline passes first
    the helper gives the lock straight back when it eventually arrives (and closes the fd), so
    an abandoned wait never holds the graph. If flock itself fails, the helper closes the fd
    once the caller has given up on it."""
    import threading
    state = {"acquired": False, "abandoned": False, "failed": False}
    guard, done = threading.Lock(), threading.Event()

    def take():
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            with guard:
                state["failed"] = True
                if state["abandoned"]:
                    with contextlib.suppress(OSError):
                        os.close(fd)
            done.set()
            return
        with guard:
            if state["abandoned"]:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
                return
            state["acquired"] = True
        done.set()

    waiter = threading.Thread(target=take, name="graph-lock", daemon=True)
    waiter.start()
    done.wait(timeout)
    with guard:
        if state["acquired"]:
            return True
        if state["failed"]:
            raise GraphError("could not lock graph.lock")  # The caller still owns and closes the fd.
        state["abandoned"] = True
    return False


@contextlib.contextmanager
def session(path, partial):
    if not path.exists() and not legacy_path(path).exists() and restore_backup(path, backup_path(path)):
        # The database went missing but its checkpoint did not: start from the checkpoint, not from nothing.
        mark_recovering(path, "backup (graph.db was missing)")
    store, graph = open_for_write(path, partial)
    if recovering_marker(path).exists():
        # Replay in its own committed transaction first: if the caller's work below fails, the
        # recovered knowledge (and the journal sequence) must not roll back with it.
        store.rollback()
        store.close()
        replay_recovery(path)
        store, graph = open_for_write(path, partial)
    repo = path.parent.parent
    try:
        journal.drain_pending(graph, path, repo)
    except BaseException:
        store.rollback()
        store.close()
        raise
    checkpoints = (graph["refine"].get("last_at"), graph["dream"].get("last_at"))
    try:
        yield graph
        journal.number(graph)
        changed = store.save(graph, bump_revision)
        store.commit()
    except BaseException:
        store.rollback()
        store.close()
        raise
    try:
        with contextlib.suppress(OSError, ValueError):  # ValueError covers JournalError and bad encodings.
            journal.flush(graph, path)
        journal.finish_drains(graph)
        if changed and checkpoints != (graph["refine"].get("last_at"), graph["dream"].get("last_at")):
            # A refined or dreamed graph is a consistent checkpoint to recover from.
            store.checkpoint()
            store.backup(backup_path(path))
            journal.truncate_after_checkpoint(path, graph.get("journal_seq", 0))
    finally:
        store.close()


def open_for_write(path, partial):
    """Open the store in a write transaction and load the graph (whole, or meta only). A damaged
    store is set aside and restored from its checkpoint once; a newer format is refused."""
    for attempt in (0, 1):
        store = Store(path, search_words)
        try:
            store.open(create=True)
            if attempt == 0:
                migrate_legacy(store, path)
            store.begin_write()
            graph = GraphState()
            (store.load_meta_only if partial else store.load_all)(graph)
            fill_defaults(graph, partial)
            break
        except (StoreDamaged, sqlite3.DatabaseError) as exc:
            store.rollback()
            store.close()
            if attempt or isinstance(exc, sqlite3.DatabaseError) and not is_damage(exc):
                raise
            recover(path)
    if int(graph.get("version", VERSION)) > VERSION:
        store.rollback()
        store.close()
        raise GraphError(f"graph.db was written by a newer skill (format {graph['version']}, this one reads {VERSION}); "
                         "update the skill before writing to it")
    graph.store = store
    return store, graph


def replay_recovery(path):
    """Re-apply journaled curated writes onto the recovered store and commit, then clear the
    marker. Any failure leaves the marker, so the next writer tries again."""
    marker = recovering_marker(path)
    try:
        detail = json.loads(marker.read_text()).get("detail", "backup")
    except (OSError, ValueError, AttributeError):
        detail = "backup"
    store, graph = open_for_write(path, False)
    try:
        replayed = journal.replay(graph, path, path.parent.parent)
        graph["refine"]["recovered"] = {"at": stamp(), "from": str(detail).split(" ")[0], "detail": detail,
                                        "replayed": replayed}
        store.save(graph, bump_revision)
        store.commit()
    except BaseException:
        store.rollback()
        raise
    finally:
        store.close()
    marker.unlink(missing_ok=True)


@contextlib.contextmanager
def reading(repo):
    """A consistent read-only session that loads nothing until asked (see `fetch`)."""
    path = graph_path(repo)
    if not path.exists():
        legacy = legacy_path(path)
        yield legacy_graph(legacy) if legacy.exists() else fill_defaults(GraphState(empty_graph()))
        return
    store = Store(path, search_words)
    try:
        opened = store.open(create=False)
    except (StoreDamaged, sqlite3.DatabaseError):
        opened = False
    if not opened:
        store.close()
        yield fill_defaults(GraphState(empty_graph()))
        return
    try:
        with store.snapshot():
            graph = fill_defaults(store.load_meta_only(GraphState()), partial=True)
            graph.store = store
            yield graph
    finally:
        store.close()


def fetch(graph, node_ids):
    """In a partial session, load these nodes and the links among loaded nodes."""
    store = getattr(graph, "store", None)
    if getattr(graph, "partial", False) and store is not None:
        store.fetch(graph, node_ids)


def fetch_types(graph, *types):
    store = getattr(graph, "store", None)
    if getattr(graph, "partial", False) and store is not None:
        store.fetch(graph, [node_id for kind in types for node_id in store.ids_of_type(kind)])


# ---------------------------------------------------------------- text helpers

CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
# Scripts written without spaces between words are indexed as character pairs.
UNSPACED = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]")
NEGATION = re.compile(r"\b(?:not|no|never|none|nothing|nor|neither|cannot|avoid|avoids|avoided|avoiding|without|"
                      r"forbid|forbids|forbidden|disallow|disallowed|prohibit|prohibited|mustnt|dont|doesnt|didnt|isnt|"
                      r"arent|wasnt|werent|wont|wouldnt|shouldnt|cant|couldnt)\b|n[\'\u2019]t\b", re.IGNORECASE)
NUMBER = re.compile(r"\d+(?:\.\d+)?")
PATH_LIKE = re.compile(r"[\w.-]+(?:/[\w.-]+)+|\b[\w-]+\.(?:py|pyi|js|jsx|ts|tsx|mjs|cjs|go|rs|rb|java|kt|swift|c|h|cc|"
                       r"cpp|hpp|cs|php|sh|md|json|ya?ml|toml|ini|cfg|sql|html|css|scss|vue|svelte|lock|txt)\b")
# Words whose swap reverses advice: one note says the left word where the other says the right.
OPPOSITES = (("always", "never"), ("before", "after"), ("enable", "disable"), ("enabled", "disabled"),
             ("true", "false"), ("increase", "decrease"), ("add", "remove"), ("allow", "deny"),
             ("include", "exclude"), ("first", "last"), ("more", "less"), ("higher", "lower"),
             ("faster", "slower"), ("sync", "async"), ("required", "optional"), ("safe", "unsafe"))


def clean(text, limit):
    """One line of plain text: control and bidirectional-override characters removed, and
    lone surrogates (undecodable bytes from argv) replaced so the text can always be stored,
    journaled, and printed."""
    text = str(text or "").encode("utf-8", "replace").decode("utf-8")
    return re.sub(r"\s+", " ", CONTROL_CHARS.sub("", text)).strip()[:limit]


def normalized(text):
    return re.sub(r"[^\w ]+", "", re.sub(r"\s+", " ", str(text).lower())).replace("_", "").strip()


@lru_cache(maxsize=8192)
def tokens(text):
    """Search and similarity words: Unicode words of two or more characters, every number, and
    character pairs for scripts written without spaces."""
    found = set()
    for word in re.findall(r"\w+", str(text).lower().replace("/", " ").replace(".", " ")):
        if UNSPACED.search(word):
            found.update(word[index:index + 2] for index in range(max(1, len(word) - 1)))
        elif (len(word) > 1 or word.isdigit()) and word not in STOP_WORDS:
            found.add(word)
    return frozenset(found)


def similarity(left, right):
    a, b = tokens(left), tokens(right)
    return len(a & b) / len(a | b) if a and b else 0.0


@lru_cache(maxsize=8192)
def facts(text):
    """What must agree for two notes to say the same thing: negation count, numbers, paths."""
    text = str(text)
    lowered = text.lower()
    negations = len(NEGATION.findall(lowered.replace("\u2019", "'")))
    return negations, frozenset(NUMBER.findall(lowered)), frozenset(path.strip("./") for path in PATH_LIKE.findall(text))


def opposed(left, right):
    """True when one note says what the other negates or reverses (always/never, before/after)."""
    a, b = facts(left), facts(right)
    if a[0] != b[0]:
        return True
    words_a, words_b = tokens(left), tokens(right)
    return any((x in words_a - words_b and y in words_b - words_a) or (y in words_a - words_b and x in words_b - words_a)
               for x, y in OPPOSITES)


def duplicate(left, right, threshold=None):
    """Two notes are the same knowledge only when their wording overlaps strongly AND they agree
    on polarity, numbers, and the files they name. Word overlap alone merged "always X" with
    "never X", "5 seconds" with "50 seconds", and one file's note with another's."""
    threshold = MERGE_SIMILARITY if threshold is None else threshold
    if similarity(left, right) < threshold or opposed(left, right) or swapped_content(left, right):
        return False
    a, b = facts(left), facts(right)
    return a[1] == b[1] and a[2] == b[2]


# Words whose swap does not change what a note is about: connectives, pronouns, modal and
# auxiliary verbs. Negations and opposites are judged separately (`opposed`).
SWAP_NEUTRAL = STOP_WORDS | frozenset(
    "because since so as then also but if while which who whom whose what where why how than thus hence therefore "
    "should must can will would could may might shall do does did done we you they i he she our your their his her "
    "them us me my into onto upon over via per just only even still very really always usually often first".split())


def content_words(text):
    return [word for word in re.findall(r"\w+", str(text).lower()) if word not in SWAP_NEUTRAL]


def swapped_content(left, right):
    """True when the notes say the same thing about different subjects: aligned word by word
    (after dropping stop words), they differ by a substitution of one content word for another
    ("read lock" / "write lock", "mock the clock" / "mock the network"). An inserted or dropped
    word ("the scheduler" / "the scheduler module") is a paraphrase, not a swap."""
    a, b = content_words(left), content_words(right)
    if len(a) != len(b):
        return False
    return any(x != y for x, y in zip(a, b))


def short_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def blocker_key(reason):
    # Matches improve_learning: collapse SHAs, numbers, and paths so repeats group together.
    reason = re.sub(HEX_RUN + r"|\d+|refs/\S+|/\S+", "#", str(reason).lower())
    return normalized(reason)[:120]


def safe_path(value, repo=None):
    """A repository-relative file path, or None for anything that cannot be one."""
    if not isinstance(value, str):
        return None
    value = value.strip().split(":", 1)[0] if re.search(r":\d+$", value.strip()) else value.strip()
    if not value or len(value) > 300 or "\x00" in value or "\n" in value:
        return None
    if CONTROL_CHARS.search(value):
        return None
    # Separators first, then the checks: `src\..\..\x` must not slip past a `..` test that
    # only understood `/`.
    value = value.replace("\\", "/")
    if value.startswith("/"):
        if repo is None:
            return None
        try:
            value = str(Path(value).resolve().relative_to(Path(repo).resolve()))
        except ValueError:
            return None
    if ".." in value.split("/") or re.match(r"^[A-Za-z]:", value):
        return None
    # One spelling per file: `a//b`, `a/./b`, and `./a` are the same node as `a/b` and `a`.
    value = posixpath.normpath(value).rstrip("/")
    if not value or value == "." or value.startswith("../") or value == ".." or value.startswith("/") \
            or value.split("/", 1)[0] == ".improve":
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
        touch(graph, node)
    return node


def touch(graph, node):
    """Mark a node as used now, on the dream clock (which advances within one long run)."""
    node["last_tick"] = int(graph.get("tick", 0))


def link(graph, src, rel, dst, seq=None, **fields):
    if rel not in RELATIONS:
        raise GraphError(f"unknown relation {rel!r}; use one of {', '.join(USER_RELATIONS)}")
    src, dst = resolve(graph, src), resolve(graph, dst)
    if src == dst or src not in graph["nodes"] or dst not in graph["nodes"]:
        return None
    if rel in ("co_changed", "co_cited", "similar_to", "contradicts") and dst < src:
        src, dst = dst, src
    key = edge_key(src, rel, dst)
    edge = graph["edges"].setdefault(key, {"src": src, "rel": rel, "dst": dst, "weight": 1, "created_at": stamp()})
    for name, value in fields.items():
        if value is not None:
            edge[name] = value
    if seq is not None:
        edge["last_seq"] = seq
    return edge


def remove_nodes(graph, node_ids, fold=None, reason=None, op="removed", extras=None):
    """Delete nodes and their links in one pass over the edges (pruning thousands stays linear).
    The store keeps each removed node, its links, and `reason` in the graph history."""
    doomed = set(node_ids)
    if not doomed:
        return
    removed = getattr(graph, "removed", None)
    if removed is not None:
        for node_id in doomed:
            if node_id in graph["nodes"]:
                removed[node_id] = {"op": op, "reason": reason, "extra": (extras or {}).get(node_id)}
    for key in [key for key, edge in graph["edges"].items() if edge["src"] in doomed or edge["dst"] in doomed]:
        edge = graph["edges"].pop(key)
        if fold:
            fold(edge)
    for node_id in doomed:
        graph["nodes"].pop(node_id, None)


def remove_node(graph, node_id, reason=None, op="removed", extra=None):
    remove_nodes(graph, [node_id], reason=reason, op=op, extras={node_id: extra} if extra else None)


def edge_key(src, rel, dst):
    return f"{src}|{rel}|{dst}"


def edges_of(graph, node_id):
    return [edge for edge in graph["edges"].values() if node_id in (edge["src"], edge["dst"])]


def adjacency(graph):
    """Each node's links in a fixed order, so every traversal (and every sum of scores) is the
    same whether the graph was loaded whole or in part, and in whatever order rows arrived.
    A read-only graph (`load_cached`) keeps the result, so a polling board builds it once per
    revision instead of several times per request."""
    cached = getattr(graph, "adjacency_cache", None)
    if cached is not None and cached[0] == (graph.get("revision"), len(graph["edges"])):
        return cached[1]
    result = {}
    for key in sorted(graph["edges"]):
        edge = graph["edges"][key]
        result.setdefault(edge["src"], []).append((edge, edge["dst"]))
        result.setdefault(edge["dst"], []).append((edge, edge["src"]))
    if getattr(graph, "frozen", False):
        graph.adjacency_cache = ((graph.get("revision"), len(graph["edges"])), result)
    return result


def edge_tier(edge):
    """Graphify's confidence tiers for this graph: `recorded` by an agent or user, `observed`
    from ledgers, git, or code, `inferred` by dreaming."""
    if edge.get("inferred"):
        return "inferred"
    return "observed" if edge["rel"] in STRUCTURAL or edge["rel"] == "used_in" else "recorded"


def ensure(graph, node_id, node_type, label, seq=None, **fields):
    """Upsert, but first bring back a forgotten node of the same id with its history, so its
    folded statistics and links are not lost when the work returns to it."""
    if node_id not in graph["nodes"]:
        store = getattr(graph, "store", None)
        if store is not None and node_id in store.forgotten_ids():
            with contextlib.suppress(GraphError):
                restore_node(graph, node_id, "the work came back to it", source="recall")
    return upsert(graph, node_id, node_type, label, seq, **fields)


def candidate_ids(graph, value, repo=None):
    """Every id `target_id` could resolve `value` to, so a partial session can load them first."""
    value = str(value).strip()
    prefix, _, rest = value.partition(":")
    found = [value, resolve(graph, value)]
    path = safe_path(rest if prefix == "file" else value, repo)
    if path:
        found.append("file:" + path)
    return found


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
    ledger = state / "backlog.jsonl"
    tasks = merged_tasks(state)
    counts = {status: sum(task.get("status") == status for task in tasks.values()) for status in FINISHED}
    outcome = control.get("outcome") if control else run.get("outcome")
    totals = dict(outcome=outcome, tasks=len(tasks), **counts)
    if any(entry.get(key) != value for key, value in totals.items()):
        entry.update(ingested_at=stamp(), **totals)
    # Pruned tasks were folded into file/area baselines; re-adding them would count them twice.
    pruned = set(entry.get("pruned", []))
    prefix = f"task:{run_id}:"
    previous = {}
    for key, edge in graph["edges"].items():
        if edge["src"].startswith(prefix) and edge["rel"] in TASK_RELATIONS:
            previous.setdefault(edge["src"], set()).add(key)
        elif edge["dst"].startswith(prefix) and edge["rel"] == "used_in":
            previous.setdefault(edge["dst"], set()).add(key)
    wanted = {}
    run_node = "run:" + run_id
    ensure(graph, run_node, "run", run_id, seq, outcome=outcome, engine=control.get("engine"),
           started_at=control.get("started_at") or run.get("started_at"),
           ended_at=control.get("ended_at") or run.get("ended_at"), stats={"tasks": len(tasks), **counts})

    gates = flat_gates(run.get("gates"))
    seconds = flat_gates(run.get("gate_seconds"))
    gate_ids = {}
    for name, command in list(gates.items())[:40]:
        if not re.fullmatch(r"[\w.:-]{1,60}", str(name)) or name == "...":
            continue
        cost = seconds.get(name)
        ensure(graph, "gate:" + name, "gate", name, seq,
               summary=clean(command if isinstance(command, str) else json.dumps(command), MAX_SUMMARY),
               seconds=cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None)
        gate_ids[name] = "gate:" + name

    about_files = None

    def keep(node_id, edge):
        if edge is not None:
            wanted.setdefault(node_id, set()).add(edge_key(edge["src"], edge["rel"], edge["dst"]))

    # Every task is ingested; refinement bounds the graph by pruning, which folds each outcome
    # into file and area statistics first. Capping here would silently lose the oldest outcomes.
    for task_id, task in tasks.items():
        if str(task_id) in pruned:
            continue
        status = task.get("status") if isinstance(task.get("status"), str) else "ready"
        area = task.get("area") if isinstance(task.get("area"), str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", task["area"]) else "general"
        node_id = f"task:{run_id}:{task_id}"
        reason = task.get("blocked_reason") if status == "blocked" else None
        detail = task.get("note") if status in ("rejected", "blocked") else task.get("evidence")
        finished = task.get("completed_at") or task.get("blocked_at") or (task.get("updated_at") if status in FINISHED else None)
        upsert(graph, node_id, "task", task.get("title") or task.get("claim") or task_id, seq,
               status=status, area=area, run_id=run_id, task_id=str(task_id),
               finished_at=finished if isinstance(finished, str) else None,
               summary=clean(task.get("claim") or "", MAX_SUMMARY) or None,
               outcome_note=clean(reason or detail or "", MAX_SUMMARY) or None,
               commit=task.get("commit") if isinstance(task.get("commit"), str) else None)
        wanted.setdefault(node_id, set())
        keep(node_id, link(graph, node_id, "in_run", run_node, seq))
        ensure(graph, "area:" + area, "area", area, seq)
        keep(node_id, link(graph, node_id, "in_area", "area:" + area, seq))
        for path in task_files(task, repo):
            file_id = resolve(graph, "file:" + path)  # A renamed file's old path lands on the new node.
            ensure(graph, file_id, "file", file_id[len("file:"):], seq)
            keep(node_id, link(graph, node_id, "touches", file_id, seq))
        for name in verified_gates(task, gate_ids):
            keep(node_id, link(graph, node_id, "verified_by", gate_ids[name], seq))
        # A supervisor block ("3 cycles without evidence", "left uncommitted changes") is an
        # event of the run, not knowledge about the file; it would only pollute blockers.
        if (reason or status == "blocked") and task.get("blocked_by") != "supervisor":
            reason = clean(reason or task.get("note") or "unspecified", 300)
            key = blocker_key(reason) or "unspecified"
            blocker = "blocker:" + short_hash(key)
            ensure(graph, blocker, "blocker", reason, seq, key=key)
            keep(node_id, link(graph, node_id, "blocked_by", blocker, seq))
        refs = task.get("graph_refs")
        for ref in refs[:20] if isinstance(refs, list) else []:
            ref = resolve(graph, str(ref))
            if ref not in graph["nodes"]:
                # A task relied on knowledge that was forgotten: it was valuable after all.
                recall(graph, ref, f"cited by task {task_id}")
            if ref in graph["nodes"] and graph["nodes"][ref]["type"] == "insight":
                keep(node_id, link(graph, ref, "used_in", node_id, seq))
                insight = graph["nodes"][ref]
                insight["last_seq"] = max(int(insight.get("last_seq", 0)), seq)
                if status in FINISHED:
                    touch(graph, insight)
                if status == "done" and isinstance(finished, str) and finished > str(insight.get("verified_at") or ""):
                    # A verified success re-proves the knowledge against the code as it is now.
                    if about_files is None:
                        about_files = about_index(graph)
                    stamp_fingerprints(graph, insight, repo, about_files.get(ref, []))
                    insight["verified_at"] = finished
    # The ledger is the source of truth: drop links a task no longer has (unblocked,
    # paths changed, citation removed).
    for node_id, keys in wanted.items():
        for key in previous.get(node_id, set()) - keys:
            graph["edges"].pop(key, None)
    # Drop tasks that left a readable ledger (for example a damaged line set aside). An
    # unreadable or empty ledger is treated as a read problem, never as mass deletion.
    if ledger.is_file() and not ledger.is_symlink() and tasks:
        vanished = [node_id for node_id, node in graph["nodes"].items()
                    if node["type"] == "task" and node.get("run_id") == run_id and node_id not in wanted
                    and node.get("task_id") not in tasks and node.get("task_id") not in pruned]
        remove_nodes(graph, vanished, reason="left the run's ledger", op="vanished")
    return entry


def flat_gates(value):
    """Gate names to commands (or costs) whatever shape run.json uses: flat (`go.fast`) or
    nested by ecosystem (`go: {fast, batch}`, as references/verification.md records them).
    A nested group keeps its own name too, so a task that cites `go` still matches."""
    if not isinstance(value, dict):
        return {}
    flat = {}
    for name, item in value.items():
        if isinstance(item, dict) and item and all(isinstance(key, str) for key in item):
            flat[str(name)] = item
            for child, leaf in item.items():
                if not isinstance(leaf, dict):
                    flat[f"{name}.{child}"] = leaf
        else:
            flat[str(name)] = item
    return flat


def about_index(graph):
    """Insight id -> the file ids it is recorded about."""
    found = {}
    for edge in graph["edges"].values():
        if edge["rel"] == "about" and edge["dst"].startswith("file:"):
            found.setdefault(edge["src"], []).append(edge["dst"])
    return found


def verified_gates(task, gate_ids):
    """Gates a task's verification names. A structured list is exact; free text matches
    whole gate names only, so a gate called `test` does not claim `latest`."""
    verification = task.get("verification") or task.get("last_verification") or ""
    names = []
    if isinstance(verification, dict) and isinstance(verification.get("gates"), list):
        names = [str(name) for name in verification["gates"]]
    elif isinstance(verification, list) and all(isinstance(item, str) for item in verification):
        names = verification
    if names:
        return [name for name in gate_ids if name in names]
    text = json.dumps(verification) if not isinstance(verification, str) else verification
    found = []
    for name in gate_ids:
        # A gate name may contain '.' or ':' but prose punctuation after it must not hide a match.
        if len(name) > 2 and re.search(rf"(?<![\w.:-]){re.escape(name)}(?![\w-])(?![.:]\w)", text):
            found.append(name)
    return found


def import_lessons(graph, repo):
    """Mirror learning.json lessons as insight nodes, once per lesson. A lesson comes back
    only when it was reinforced since, so capped or archived lessons do not return on
    every sync."""
    from improve_learning import load as load_learning, normalized as lesson_key, store_path

    lessons = load_learning(store_path(repo), repair=False)["lessons"]
    imported = graph.setdefault("lessons", {})
    trimmed = graph.setdefault("lessons_trimmed", {})
    for lesson in lessons[:MAX_INSIGHTS]:
        text = lesson.get("text")
        if not isinstance(text, str) or len(text) < 8:
            continue
        key = lesson.get("key") if isinstance(lesson.get("key"), str) else lesson_key(text)
        strength = int(lesson.get("reinforced", 1) or 1)
        if lesson_strength(imported.get(key)) >= strength or int(trimmed.get(key, 0)) >= strength:
            continue
        node = add_insight(graph, text, "lesson", source="learning", reinforce=False)
        node["reinforced"] = max(int(node.get("reinforced", 1)), strength)
        area = lesson.get("area")
        if isinstance(area, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", area):
            upsert(graph, "area:" + area, "area", area)
            link(graph, node["id"], "about", "area:" + area)
        graph["lessons_seq"] = int(graph.get("lessons_seq", 0) or 0) + 1
        imported[key] = {"strength": strength, "seq": graph["lessons_seq"]}
        trimmed.pop(key, None)
    # Bound by age (insertion sequence), not by key order; remember what was trimmed so it is
    # not imported again on every sync unless it was reinforced since.
    excess = max(0, len(imported) - 2 * MAX_INSIGHTS)
    if excess:
        oldest = sorted(imported, key=lambda key: (lesson_sequence(imported[key]), key))[:excess]
        for key in oldest:
            trimmed[key] = lesson_strength(imported.pop(key))
    for key in sorted(trimmed)[:max(0, len(trimmed) - 4 * MAX_INSIGHTS)]:
        del trimmed[key]


def lesson_strength(value):
    """Imported-lesson records were plain strengths before they carried a sequence."""
    if isinstance(value, dict):
        return int(value.get("strength", 0) or 0)
    return int(value or 0) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def lesson_sequence(value):
    return int(value.get("seq", 0) or 0) if isinstance(value, dict) else 0


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


def refine_due(graph, interval):
    """True when the last refinement is older than `interval` seconds, or never happened:
    file and area statistics are useful from the first ingest on."""
    return due(graph["refine"], interval, bool(graph["nodes"]), first_time=True)


def due(record, interval, populated, first_time=False):
    """`first_time` says whether a record that was never stamped is due at once; otherwise
    the interval is measured from `since`, the first ingest."""
    if interval is None or interval <= 0 or not populated:
        return False
    if not record.get("last_at") and first_time:
        return True
    try:
        return epoch(record.get("last_at") or record.get("since")) + interval <= epoch(stamp())
    except StateError:
        return True


def sync(repo, include_history=None, refine_after=False, refine_interval=None, dream_after=False, dream_interval=None,
         code_index=None, code_seconds=None):
    """Ingest archived runs (first use only, unless asked) and the current run. Then dream
    (which refines first) when asked or due, otherwise refine when asked or due."""
    repo = Path(repo)
    state = repo / ".improve"
    with locked(repo) as graph:
        for record in (graph["refine"], graph["dream"]):
            record.setdefault("since", stamp())
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
        retrieved = journal.drain_reads(graph, repo, touch)
        from improve_dream import dream, dream_due
        code = None
        if code_index is not False:
            # Every sync, not only when refinement is due: `impact` and `context` must describe the
            # code as the last cycle left it, and an unchanged index costs a few milliseconds.
            # It feeds refinement (groups, impact), so it is refreshed first.
            code = refresh_code(graph, repo, code_seconds)
        dreamed = dream(graph, repo) if dream_after or dream_due(graph, dream_interval) else None
        if dreamed:
            report = dreamed["refine"]
        else:
            report = refine(graph, repo) if refine_after or refine_due(graph, refine_interval) else None
        result = {"ingested": ingested, "refine": report, "dream": dreamed,
                  "nodes": len(graph["nodes"]), "edges": len(graph["edges"])}
        if retrieved:
            result["retrieved"] = retrieved
        if code:
            result["code"] = code
        return result


def refresh_code(graph, repo, seconds=None):
    """Bring the code layer up to date inside this write session; never fails the sync."""
    import improve_code
    seconds = improve_code.env_seconds() if seconds is None else seconds
    store = getattr(graph, "store", None)
    if store is None or store.con is None or seconds <= 0:
        return None
    try:
        report = improve_code.index(store.con, repo, seconds)
    except (sqlite3.Error, OSError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return report


def code_stale(graph, repo):
    """True when commits landed since the code layer was last fully indexed (the code layer
    records the commit it indexed; this asks it rather than keeping a second copy)."""
    con = getattr(getattr(graph, "store", None), "con", None)
    if con is None or repo is None:
        return False
    import improve_code
    try:
        return bool(improve_code.freshness(con, repo)["stale"])
    except sqlite3.Error:
        return False


# ---------------------------------------------------------------- agent/user writes

def add_insight(graph, text, kind="fact", source="agent", about=(), run_id=None, repo=None, reinforce=True):
    """Record knowledge. A true duplicate reinforces the existing node instead of adding noise.

    Provenance comes first: when the user (or the learning store) writes a near-duplicate of
    lower-ranked knowledge, the new wording becomes the node and the old note is merged into
    it, never the other way round. A user's correction must not come back as the agent's text
    labelled "from the user"."""
    from improve_code import redact
    # Notes reach every cycle's prompt: a credential pasted into one must not travel with it.
    text = clean(redact(clean(text, MAX_SUMMARY * 2)), MAX_SUMMARY)
    if len(text) < 8:
        raise GraphError("knowledge needs at least 8 characters")
    if kind not in INSIGHT_KINDS:
        raise GraphError(f"kind must be one of {', '.join(INSIGHT_KINDS)}")
    # Resolve every target first: a bad one must not leave a half-recorded note behind.
    targets = []
    for value in about:
        fetch(graph, candidate_ids(graph, value, repo))
        targets.append(target_id(graph, value, repo))
    raw_id = "insight:" + short_hash(normalized(text))
    node_id = resolve(graph, raw_id)
    fetch_types(graph, "insight")
    node = graph["nodes"].get(node_id)
    if node is None:
        node = next((item for item in sorted(graph["nodes"].values(), key=lambda item: item["id"])
                     if item["type"] == "insight" and item.get("kind") == kind
                     and duplicate(item.get("summary", ""), text)), None)
    if node is None:
        node = recall_text(graph, node_id, text, kind)
    seq = run_seq(graph)
    if node is not None and source_rank(source) > source_rank(node.get("source")) \
            and normalized(node.get("summary", "")) != normalized(text):
        # Higher provenance, different wording: the new text wins and absorbs the old note.
        older = node["id"]
        graph["aliases"].pop(raw_id, None)
        node = upsert(graph, raw_id, "insight", text[:120], seq, summary=text, kind=kind, source=source,
                      reinforced=1, useful=0, wrong=0, run_id=run_id)
        if older != raw_id:
            merge_into(graph, raw_id, older, reason="reworded by a higher-provenance source", op="merged")
        node["reinforced"] = max(1, int(node.get("reinforced", 1)))
    elif node is None:
        node = upsert(graph, node_id, "insight", text[:120], seq, summary=text, kind=kind, source=source,
                      reinforced=1, useful=0, wrong=0, run_id=run_id)
    else:
        if reinforce:
            node["reinforced"] = int(node.get("reinforced", 1)) + 1
            touch(graph, node)
        node["last_seq"] = max(int(node.get("last_seq", 0)), seq)
        if source_rank(source) > source_rank(node.get("source")):
            node["source"] = source  # Same words, stronger provenance.
        if reinforce:
            node.pop("archived", None)
            node.pop("archived_tick", None)
    for target in targets:
        link(graph, node["id"], "about", target, seq)
    stamp_fingerprints(graph, node, repo, [target for target in targets if target.startswith("file:")])
    return node


def source_rank(source):
    return SOURCE_RANK.get(source, 0)


def file_fingerprint(repo, path):
    """Short content hash of a repository file, or None when it is missing or unreadable."""
    if repo is None:
        return None
    try:
        target = Path(repo) / path
        if target.is_symlink() or not target.is_file() or target.stat().st_size > FINGERPRINT_MAX_BYTES:
            return None
        return hashlib.sha256(target.read_bytes()).hexdigest()[:12]
    except OSError:
        return None


def stamp_fingerprints(graph, node, repo, file_ids):
    """Remember what the files looked like when this knowledge was recorded or last proven, so
    a query can say when the code has changed since (Graphify's code fingerprint)."""
    if repo is None or not file_ids:
        return
    prints = dict(node.get("fingerprints") or {})
    for file_id in file_ids[:MAX_FINGERPRINTS]:
        path = file_id[len("file:"):]
        value = file_fingerprint(repo, path)
        if value:
            prints[path] = value
    if prints:
        node["fingerprints"] = dict(sorted(prints.items())[:MAX_FINGERPRINTS])


def code_changed(node, repo, cache=None):
    """Paths this knowledge was recorded against whose content has changed since."""
    prints = node.get("fingerprints") or {}
    if repo is None or not prints:
        return []
    cache = {} if cache is None else cache
    changed = []
    for path, value in sorted(prints.items()):
        if path not in cache:
            cache[path] = file_fingerprint(repo, path)
        if cache[path] != value:
            changed.append(path)
    return changed


def recall_text(graph, node_id, text, kind):
    """Writing down forgotten knowledge again brings the original back with its history."""
    store = getattr(graph, "store", None)
    if store is None:
        return None
    best = None
    for _, forgotten_id, body in store.forgotten_insights():
        if forgotten_id in graph["nodes"]:
            continue
        exact = forgotten_id == node_id
        close = body.get("kind") == kind and duplicate(body.get("summary", ""), text)
        if exact or close:
            best = forgotten_id
            if exact:
                break
    if best is None:
        return None
    try:
        return restore_node(graph, best, "the same knowledge was recorded again", source="recall")
    except GraphError:
        return None


def recall(graph, node_id, reason):
    store = getattr(graph, "store", None)
    if store is not None and node_id in store.forgotten_ids():
        with contextlib.suppress(GraphError):
            return restore_node(graph, node_id, reason, source="recall")
    return None


def restore_node(graph, node_id, reason="restored", source="user"):
    """Bring a removed node back exactly: its fields, its links (where the other end still
    exists), and the bookkeeping its removal changed (folded counts, merges, prune lists)."""
    store = getattr(graph, "store", None)
    if store is None:
        raise GraphError("restoring needs a graph session")
    record = store.latest_forgotten(node_id)
    if record is None:
        raise GraphError(f"{node_id!r} is not in the graph history")
    body, extra = dict(record["body"]), record["extra"] or {}
    # The keeper may itself have been merged since: give back to whoever holds the counts now.
    superseded = resolve(graph, extra["superseded_by"]) if extra.get("superseded_by") else None
    if graph["aliases"].get(node_id):
        del graph["aliases"][node_id]
    links = record["edges"]
    neighbors_needed = [resolve(graph, edge["src"] if edge["dst"] == node_id else edge["dst"]) for edge in links]
    fetch(graph, [node_id, *neighbors_needed, *(extra.get("fold") or {}), *(extra.get("cite_fold") or {}),
                  *([superseded] if superseded else [])])
    tick = int(graph.get("tick", 0))
    existing = graph["nodes"].get(node_id)
    if existing is not None:
        # Recreated since (for example by ingest): keep the live node, add back what it had.
        for status, count in (body.get("base") or {}).items():
            existing.setdefault("base", {})[status] = existing.get("base", {}).get(status, 0) + count
        node = existing
    else:
        node = body
        graph["nodes"][node_id] = node
    for key in ("archived", "archived_tick", "dormant"):
        node.pop(key, None)
    node.update(restored={"at": stamp(), "reason": clean(reason, 200), "by": source, "from_op": record["op"]},
                restored_seq=run_seq(graph), protected_until_tick=tick + PROTECT_TICKS)
    touch(graph, node)
    for edge in links:
        src = node_id if edge["src"] == node_id else resolve(graph, edge["src"])
        dst = node_id if edge["dst"] == node_id else resolve(graph, edge["dst"])
        if src != dst and src in graph["nodes"] and dst in graph["nodes"] and edge.get("rel") in RELATIONS:
            graph["edges"].setdefault(edge_key(src, edge["rel"], dst), {**edge, "src": src, "dst": dst})
    for target, status in (extra.get("fold") or {}).items():
        base = graph["nodes"].get(target, {}).get("base")
        if base and base.get(status, 0) > 0:
            base[status] -= 1
    co_base = graph.get("co_base") or {}
    for pair in extra.get("co_fold") or []:
        if int(co_base.get(pair, 0)) > 1:
            co_base[pair] = int(co_base[pair]) - 1
        else:
            co_base.pop(pair, None)
    for target, outcome in (extra.get("cite_fold") or {}).items():
        base = graph["nodes"].get(target, {}).get("cite_base")
        if base and base.get(outcome, 0) > 0:
            base[outcome] -= 1
    if node.get("type") == "task":
        entry = next((row for row in graph["runs"] if row.get("run_id") == node.get("run_id")), None)
        if entry and node.get("task_id") in entry.get("pruned", []):
            entry["pruned"].remove(node["task_id"])
    if superseded and superseded in graph["nodes"]:
        keeper = graph["nodes"][superseded]
        for field, count in (extra.get("merged_counts") or {}).items():
            if isinstance(count, dict):
                base = keeper.get(field) or {}
                for key, value in count.items():
                    base[key] = max(0, int(base.get(key, 0)) - int(value))
                if base:
                    keeper[field] = base
                continue
            floor = 1 if field == "reinforced" else 0
            keeper[field] = max(floor, int(keeper.get(field, floor)) - int(count))
        summary = clean(body.get("summary"), 160)
        if summary in keeper.get("merged_from", []):
            keeper["merged_from"].remove(summary)
    store.mark_restored(record["seq"], stamp())
    getattr(graph, "removed", {}).pop(node_id, None)
    return node


def rate(graph, node_id, verdict, source="agent"):
    node = graph["nodes"].get(resolve(graph, node_id))
    if node is None:
        raise GraphError(f"no graph node {node_id!r}")
    tick = int(graph.get("tick", 0))
    if verdict == "useful":
        node["useful"] = int(node.get("useful", 0)) + 1
        node["last_seq"] = run_seq(graph)
        node.pop("archived", None)
        node.pop("archived_tick", None)
        touch(graph, node)
    elif verdict == "wrong":
        node["wrong"] = int(node.get("wrong", 0)) + 1
        if source == "user":
            node["archived"] = "marked wrong by the user"
            node["archived_tick"] = tick
    elif verdict in ("pin", "unpin"):
        node["pinned"] = verdict == "pin"
        if node["pinned"]:
            node.pop("archived", None)
            node.pop("archived_tick", None)
            touch(graph, node)
    elif verdict in ("archive", "restore"):
        if verdict == "archive":
            node["archived"] = f"archived by {source}"
            node["archived_tick"] = tick
            node["pinned"] = False
        else:
            node.pop("archived", None)
            node.pop("archived_tick", None)
            node["restored_seq"] = run_seq(graph)
            node["protected_until_tick"] = tick + PROTECT_TICKS
            touch(graph, node)
    else:
        raise GraphError("verdict must be useful, wrong, pin, unpin, archive, or restore")
    return node


# ---------------------------------------------------------------- refine

def git_output(repo, *args, timeout=20):
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.decode(errors="replace") if result.returncode == 0 else None


def git_renames(repo, since=None):
    """(old, new) renames in commit order since `since` (or the last RENAME_HISTORY commits),
    and the HEAD they were read at."""
    head = (git_output(repo, "rev-parse", "HEAD") or "").strip()
    if not head:
        return [], None
    if since == head:
        return [], head
    revision = []
    if since and git_output(repo, "merge-base", "--is-ancestor", since, head) is not None:
        revision = [f"{since}..{head}"]
    else:
        revision = ["-n", str(RENAME_HISTORY), head]
    output = git_output(repo, "log", "-M", "--diff-filter=R", "--name-status", "--format=%x00", *revision, timeout=30)
    if output is None:
        return [], head
    commits = [chunk for chunk in output.split("\0") if chunk.strip()]
    renames = []
    for chunk in reversed(commits):  # git log lists newest first
        for line in chunk.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0].startswith("R"):
                old, new = safe_path(parts[1]), safe_path(parts[2])
                if old and new and old != new:
                    renames.append((old, new))
    return renames, head


def track_renames(graph, repo, files, report):
    """Follow `git mv` so a moved file keeps its history: statistics, links, and the
    knowledge recorded about it move to the new path, and the old id resolves to it."""
    report["renamed"] = 0
    if repo is None or files is None:
        return
    renames, head = git_renames(repo, graph["refine"].get("head"))
    if head:
        graph["refine"]["head"] = head
    final = {}
    for old, new in renames:
        for key, value in list(final.items()):
            if value == old:
                final[key] = new
        final[old] = new
    for old, new in sorted(final.items()):
        drop, keep = resolve(graph, "file:" + old), "file:" + new
        if drop not in graph["nodes"] or drop == keep or old in files or new not in files:
            continue
        merge_file(graph, keep, drop)
        report["renamed"] += 1


def merge_file(graph, keep, drop):
    nodes = graph["nodes"]
    old_node = nodes[drop]
    if keep not in nodes:
        upsert(graph, keep, "file", keep[len("file:"):], first_seq=old_node.get("first_seq"))
        nodes[keep]["created_at"] = old_node.get("created_at", nodes[keep]["created_at"])
    keep_node = nodes[keep]
    moved = {key: int(value) for key, value in (old_node.get("base") or {}).items() if int(value)}
    if moved:
        base = keep_node.setdefault("base", {})
        for key, value in moved.items():
            base[key] = int(base.get(key, 0)) + value
    keep_node["last_seq"] = max(int(keep_node.get("last_seq", 0)), int(old_node.get("last_seq", 0)))
    old_path, new_path = drop[len("file:"):], keep[len("file:"):]
    for edge in edges_of(graph, drop):
        if edge["rel"] in DREAM_RELATIONS:
            continue  # Re-derived by the next dream from the moved evidence.
        src = keep if edge["src"] == drop else edge["src"]
        dst = keep if edge["dst"] == drop else edge["dst"]
        if src == dst:
            continue
        fields = {key: value for key, value in edge.items() if key not in ("src", "rel", "dst", "created_at")}
        link(graph, src, edge["rel"], dst, **fields)
        other = nodes.get(edge["src"] if edge["dst"] == drop else edge["dst"], {})
        prints = other.get("fingerprints") if other.get("type") == "insight" else None
        if prints and old_path in prints:
            prints[new_path] = prints.pop(old_path)
    remove_node(graph, drop, reason=f"renamed to {new_path}", op="renamed",
                extra={"superseded_by": keep, "merged_counts": {"base": moved} if moved else {}})
    set_alias(graph, drop, keep)


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


def co_change_pairs(files):
    """`left|right` keys for a done task's files (2 to 8 files; larger sweeps say nothing)."""
    files = sorted(set(files))
    if not 2 <= len(files) <= 8:
        return []
    return [f"{left}|{right}" for index, left in enumerate(files) for right in files[index + 1:]]


def merge_into(graph, keep, drop, reason="near-duplicate", op="merged"):
    keep_node, drop_node = graph["nodes"][keep], graph["nodes"][drop]
    merged_counts = {field: int(drop_node.get(field, 0)) for field in ("reinforced", "useful", "wrong")}
    for field in ("reinforced", "useful", "wrong"):
        keep_node[field] = int(keep_node.get(field, 0)) + int(drop_node.get(field, 0))
    keep_node["last_seq"] = max(int(keep_node.get("last_seq", 0)), int(drop_node.get("last_seq", 0)))
    keep_node["pinned"] = bool(keep_node.get("pinned") or drop_node.get("pinned"))
    # Citation credit folded from pruned tasks is proof; it moves with the knowledge.
    cite = {key: int(value) for key, value in (drop_node.get("cite_base") or {}).items() if int(value)}
    if cite:
        base = keep_node.setdefault("cite_base", {})
        for key, value in cite.items():
            base[key] = int(base.get(key, 0)) + value
        merged_counts["cite_base"] = cite
    if drop_node.get("fingerprints") and not keep_node.get("fingerprints"):
        keep_node["fingerprints"] = dict(drop_node["fingerprints"])
    keep_node.setdefault("merged_from", []).append(clean(drop_node.get("summary"), 160))
    del keep_node["merged_from"][:-10]
    for edge in edges_of(graph, drop):
        src = keep if edge["src"] == drop else edge["src"]
        dst = keep if edge["dst"] == drop else edge["dst"]
        if src != dst and edge["rel"] not in DREAM_RELATIONS:
            link(graph, src, edge["rel"], dst, edge.get("last_seq"))
    remove_node(graph, drop, reason=f"{reason} of {keep}", op=op,
                extra={"superseded_by": keep, "merged_counts": merged_counts})
    set_alias(graph, drop, keep)


def set_alias(graph, old, new):
    """Point an old id at its successor, remembering the order aliases were made in, so the
    size bound drops the oldest (meta is stored with sorted keys, so dict order is not age)."""
    graph["aliases"][old] = new
    order = graph.setdefault("alias_order", [])
    if old in order:
        order.remove(old)
    order.append(old)


def trim_aliases(graph):
    aliases = graph["aliases"]
    order = [alias for alias in graph.get("alias_order") or [] if alias in aliases]
    known = set(order)
    order = sorted(alias for alias in aliases if alias not in known) + order  # Unrecorded ones are older.
    excess = max(0, len(order) - MAX_ALIASES)
    for alias in order[:excess]:
        del aliases[alias]
    graph["alias_order"] = order[excess:]


def refine(graph, repo=None):
    """Periodic maintenance. Deterministic from saved facts, so running it often is harmless."""
    current = run_seq(graph)
    report = {"at": stamp(), "seq": current, "merged": 0, "faded": 0, "pruned_tasks": 0,
              "pruned_files": 0, "stale_files": 0, "co_changed": 0}
    nodes = graph["nodes"]
    tracked = tracked_files(repo)

    # 0. Follow renames first, so the moved file's history counts toward the new path.
    track_renames(graph, repo, tracked, report)

    # 1. Merge near-duplicate insights into the better-supported one.
    # The keeper is chosen by provenance first (user, learning, agent), then pinning, then proof,
    # then reinforcement, so user wording never disappears into an agent's note.
    insights = sorted((node for node in nodes.values() if node["type"] == "insight"),
                      key=lambda node: (-source_rank(node.get("source")), not node.get("pinned"),
                                        -int(node.get("cited_done", 0)) - int((node.get("cite_base") or {}).get("done", 0)),
                                        -int(node.get("reinforced", 1)) - 2 * int(node.get("useful", 0)), node["id"]))
    kept = []
    for node in insights:
        twin = next((other for other in kept if other.get("kind") == node.get("kind")
                     and duplicate(other.get("summary", ""), node.get("summary", ""))), None)
        if twin:
            merge_into(graph, twin["id"], node["id"])
            report["merged"] += 1
        else:
            kept.append(node)

    # 2. Prune the oldest finished tasks, folding their outcomes into file/area baselines.
    tick = int(graph.get("tick", 0))
    tasks = sorted((node for node in nodes.values() if node["type"] == "task"),
                   key=lambda node: (int(node.get("protected_until_tick", -1)) > tick,
                                     node.get("status") not in FINISHED, int(node.get("last_seq", 0)), node["id"]))
    doomed = {node["id"]: node.get("status") if node.get("status") in FINISHED else "open"
              for node in tasks[:max(0, len(tasks) - MAX_TASKS)]}
    runs_by_id = {row.get("run_id"): row for row in graph["runs"]}
    # What each pruned task contributed, so restoring it can take exactly that back.
    folds = {node_id: {"fold": {}, "cite_fold": {}} for node_id in doomed}
    # A done task's co-changed file pairs outlive it as counts, so age alone never erases a
    # co-change link (or the second-generation knowledge links built on it).
    co_base = graph.setdefault("co_base", {})
    doomed_files = {}
    for edge in graph["edges"].values():
        if edge["rel"] == "touches" and edge["src"] in doomed and doomed[edge["src"]] == "done":
            doomed_files.setdefault(edge["src"], []).append(edge["dst"])
    for task_id, files in doomed_files.items():
        pairs = co_change_pairs(files)
        for pair in pairs:
            co_base[pair] = int(co_base.get(pair, 0)) + 1
        if pairs:
            folds[task_id]["co_fold"] = pairs

    def fold(edge):
        if edge["src"] in doomed:
            other = nodes.get(edge["dst"])
            if other and other["type"] in ("file", "area") and edge["dst"] not in doomed:
                status = doomed[edge["src"]]
                base = other.setdefault("base", {})
                base[status] = base.get(status, 0) + 1
                folds[edge["src"]]["fold"][edge["dst"]] = status
        elif edge["dst"] in doomed and edge["rel"] == "used_in" and edge["src"] in nodes:
            status = doomed[edge["dst"]]
            outcome = "done" if status == "done" else "failed" if status == "rejected" else None
            if outcome:
                # Citation credit survives the task, so knowledge does not fade because its proof aged out.
                base = nodes[edge["src"]].setdefault("cite_base", {})
                base[outcome] = base.get(outcome, 0) + 1
                folds[edge["dst"]]["cite_fold"][edge["src"]] = outcome

    for node_id in doomed:
        entry = runs_by_id.get(nodes[node_id].get("run_id"))
        if entry is not None and nodes[node_id].get("task_id"):
            entry.setdefault("pruned", []).append(nodes[node_id]["task_id"])
    remove_nodes(graph, doomed, fold, reason=f"oldest work beyond the {MAX_TASKS}-task bound", op="pruned",
                 extras=folds)
    report["pruned_tasks"] = len(doomed)

    # 3. Recompute derived statistics and co-change edges from the remaining facts.
    touched, in_area, used, uses = {}, {}, {}, {}
    for edge in graph["edges"].values():
        if edge["rel"] in ("verified_by", "blocked_by"):
            uses[edge["dst"]] = uses.get(edge["dst"], 0) + 1
        if edge["rel"] == "touches":
            touched.setdefault(edge["dst"], []).append(edge["src"])
        elif edge["rel"] == "in_area":
            in_area.setdefault(edge["dst"], []).append(edge["src"])
        elif edge["rel"] == "used_in":
            used.setdefault(edge["src"], []).append(edge["dst"])
    pairs = {}
    by_task = {}
    for file_id, task_ids in touched.items():
        for task_id in task_ids:
            by_task.setdefault(task_id, []).append(file_id)
    for task_id, files in by_task.items():
        if nodes[task_id].get("status") == "done":
            for pair in co_change_pairs(files):
                left, right = pair.split("|", 1)
                pairs[(left, right)] = pairs.get((left, right), 0) + 1
    for pair, count in list(co_base.items()):
        left, _, right = pair.partition("|")
        left, right = resolve(graph, left), resolve(graph, right)
        if left in nodes and right in nodes and left != right and int(count) > 0:
            key = (left, right) if left < right else (right, left)
            pairs[key] = pairs.get(key, 0) + int(count)
    if len(co_base) > MAX_CO_BASE:
        for pair, _ in sorted(co_base.items(), key=lambda item: (int(item[1]), item[0]))[:len(co_base) - MAX_CO_BASE]:
            del co_base[pair]
    wanted = {edge_key(left, "co_changed", right): weight
              for (left, right), weight in sorted(pairs.items(), key=lambda item: (-item[1], item[0]))[:MAX_CO_CHANGE]}
    for key in [key for key, edge in graph["edges"].items() if edge["rel"] == "co_changed" and key not in wanted]:
        del graph["edges"][key]
    for key, weight in wanted.items():
        left, _, right = key.split("|", 2)
        link(graph, left, "co_changed", right, weight=weight)  # Updates the weight in place; created_at is kept.
    report["co_changed"] = len(wanted)

    directories = {str(parent) for path in tracked or () for parent in Path(path).parents if str(parent) != "."}
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
            stale = tracked is not None and node["label"] not in tracked and node["label"] not in directories
            node["stale"] = stale or None
            report["stale_files"] += bool(stale)
        elif node["type"] == "area":
            node["stats"] = outcome_stats(in_area.get(node["id"], []), graph, node.get("base"))
        elif node["type"] in ("gate", "blocker"):
            node["uses"] = uses.get(node["id"], 0)  # Counted in the one edge pass above.
        elif node["type"] == "insight":
            cited = used.get(node["id"], [])
            folded = node.get("cite_base") or {}
            node["cited_done"] = sum(nodes[task].get("status") == "done" for task in cited if task in nodes) \
                + int(folded.get("done", 0))
            # A blocked task is environmental: it neither confirms nor refutes the knowledge it cited.
            node["cited_failed"] = sum(nodes[task].get("status") == "rejected" for task in cited
                                       if task in nodes) + int(folded.get("failed", 0))

    # 4. Score everything; fade insights that stopped earning their place.
    for node in nodes.values():
        node["score"] = round(score(node, current), 4)
        if node["type"] == "insight" and not protected(node, tick) and not node.get("archived"):
            idle = current - max(int(node.get("last_seq", 0)), int(node.get("restored_seq", -1)))
            wrong = int(node.get("wrong", 0))
            if wrong >= 2 and wrong > int(node.get("useful", 0)) + int(node.get("cited_done", 0)):
                node["archived"] = "contradicted more often than confirmed"
            elif node["score"] < FADE_SCORE and idle >= FADE_AFTER_RUNS:
                node["archived"] = f"not used in {idle} runs"
            if node.get("archived"):
                node["archived_tick"] = tick
            report["faded"] += bool(node.get("archived"))

    # 5. Bound size: drop the weakest unconnected files and archived insights.
    linked = {end for edge in graph["edges"].values() for end in (edge["src"], edge["dst"])}
    orphans = [key for key, node in nodes.items() if key not in linked and not protected(node, tick) and (
        node["type"] == "blocker" or node["type"] == "file" and not node.get("base"))]
    report["pruned_orphans"] = len(orphans)
    deleted = [key for key in orphans if nodes[key].get("stale")]
    remove_nodes(graph, deleted, reason="file was deleted from the repository and nothing links to it", op="orphaned")
    remove_nodes(graph, [key for key in orphans if key not in deleted], reason="nothing links to it any more",
                 op="orphaned")
    for kind, limit, counter in (("file", MAX_FILES, "pruned_files"), ("insight", MAX_INSIGHTS, None),
                                 ("blocker", MAX_BLOCKERS, None)):
        group = sorted((node for node in nodes.values() if node["type"] == kind and not protected(node, tick)),
                       key=lambda node: (not node.get("archived"), node["score"], node["id"]))
        excess = [node["id"] for node in group[:max(0, len(group) - limit)]]
        remove_nodes(graph, excess, reason=f"weakest {kind} over the {limit}-node bound", op="capped")
        if counter:
            report[counter] += len(excess)
    for alias, target in list(graph["aliases"].items()):
        if resolve(graph, target) not in nodes:
            del graph["aliases"][alias]
    trim_aliases(graph)

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
                            "task_id", "outcome", "merged_from", "last_seq", "first_seq", "created_at", "verdict",
                            "restored", "dormant", "consolidated_from", "last_tick", "retrieved", "finished_at",
                            "code_changed", "resolved", "contradicted_by")):
    return {key: node[key] for key in fields if node.get(key) not in (None, [], {})}



def search(graph, text, limit=15, types=None, include_archived=False):
    words = tokens(text)
    if not words:
        return []
    ranked = []
    for node in graph["nodes"].values():
        if types and node["type"] not in types or (node.get("archived") and not include_archived):
            continue
        hits = len(words & node_tokens(node))
        if hits:
            exact = 1.5 if str(text).lower() in str(node.get("label", "")).lower() else 1.0
            ranked.append((hits / len(words) * exact * (0.5 + float(node.get("score", 0.5))), node))
    ranked.sort(key=lambda item: (-item[0], item[1]["id"]))
    return [{**node_view(node), "match": round(value, 3)} for value, node in ranked[:limit]]


def context_seeds(graph, files=(), area=None, repo=None):
    """The file and area nodes a context query starts from."""
    nodes = graph["nodes"]
    paths = [path for path in (safe_path(value, repo) for value in files) if path]
    seeds = []
    known_files = [(key, node["label"]) for key, node in nodes.items() if node["type"] == "file"]
    for path in paths:
        known = resolve(graph, "file:" + path)
        if known in nodes:
            seeds.append(known)
            continue
        # A directory: its known files. A new file: known files beside it.
        prefix = path.rstrip("/") + "/"
        below = [key for key, label in known_files if label.startswith(prefix)]
        if not below and "/" in path:
            parent = path.rsplit("/", 1)[0] + "/"
            below = [key for key, label in known_files if label.startswith(parent) and "/" not in label[len(parent):]]
        seeds.extend(sorted(below))
    seeds = list(dict.fromkeys(seeds))[:40]
    if area and "area:" + area in nodes:
        seeds.append("area:" + area)
    return seeds


def load_context(graph, files=(), area=None, repo=None, text=None):
    """In a partial session, load exactly what `context` reads and nothing more: the files a
    path can mean, the seeds' neighbors, the blockers, gates, and knowledge of those tasks,
    knowledge matching the text, and knowledge related to all of that."""
    store = getattr(graph, "store", None)
    if not getattr(graph, "partial", False) or store is None:
        return
    paths = [path for path in (safe_path(value, repo) for value in files) if path]
    renamed = [resolve(graph, "file:" + path) for path in paths]
    store.fetch(graph, [*store.file_candidates(paths), *renamed, *(["area:" + area] if area else [])])
    seeds = context_seeds(graph, files, area, repo)
    first = store.neighbor_ids(seeds)
    store.fetch(graph, first)
    tasks = [node_id for node_id in first if graph["nodes"].get(node_id, {}).get("type") == "task"]
    store.fetch(graph, store.neighbor_ids(tasks, types=("blocker", "gate", "insight")))
    if text:
        matches = store.search_candidates(tokens(text))
        if matches is None:
            fetch_types(graph, "insight")
        else:
            store.fetch(graph, matches)
    found = [node_id for node_id, node in graph["nodes"].items() if node["type"] == "insight"]
    store.fetch(graph, store.neighbor_ids(found, types=("insight",), rels=("co_cited", "similar_to")))


def context(graph, files=(), area=None, text=None, limit=8, repo=None, exclude=None):
    """Everything worth knowing before working on these files/area: the primary pre-task query.
    Inferred links count in proportion to their confidence; knowledge reached through a past
    task counts in proportion to how that task ended; a blocker that later work got past is
    marked resolved and ranked last."""
    if not files and not area and not text:
        raise GraphError("context needs --files, --area, or --text")
    nodes, adj = graph["nodes"], adjacency(graph)
    seeds = context_seeds(graph, files, area, repo)
    excluded = excluded_ids(graph, exclude)
    insights, tasks, blockers, gates, related = {}, {}, {}, {}, {}
    resolved = set()

    def weigh(bucket, node, amount):
        bucket[node["id"]] = bucket.get(node["id"], 0) + amount * (0.5 + float(node.get("score", 0.5)))

    for seed in seeds:
        direct = 1.0 if seed.startswith("file:") else 0.5
        latest_success = ""
        if seed.startswith("file:"):
            latest_success = max((str(other.get("finished_at") or "") for _, other in neighbors(graph, seed, adj)
                                  if other["type"] == "task" and other.get("status") == "done"), default="")
        for edge, other in neighbors(graph, seed, adj):
            if other["id"] in excluded:
                continue
            trust = float(edge.get("confidence", 1.0))
            if other["type"] == "insight" and not other.get("archived"):
                weigh(insights, other, 2 * direct * trust)
            elif other["type"] == "blocker":
                weigh(blockers, other, direct * trust)
            elif other["type"] == "task":
                weigh(tasks, other, direct)
                outcome_weight = OUTCOME_WEIGHT.get(other.get("status"), OUTCOME_WEIGHT["open"])
                for edge2, far in neighbors(graph, other["id"], adj):
                    if far["type"] == "blocker":
                        weigh(blockers, far, direct)
                        if latest_success and str(other.get("finished_at") or "") < latest_success \
                                and seed.startswith("file:") and blocker_latest(graph, far["id"], seed, adj) < latest_success:
                            resolved.add(far["id"])
                    elif far["type"] == "gate":
                        weigh(gates, far, direct)
                    elif far["type"] == "insight" and not far.get("archived"):
                        weigh(insights, far, direct * outcome_weight)
            elif other["type"] == "file" and edge["rel"] == "co_changed" and other["id"] not in seeds:
                weigh(related, other, int(edge.get("weight", 1)))
    if text:
        for hit in search(graph, text, limit, types=("insight",)):
            weigh(insights, nodes[hit["id"]], hit["match"])
    # Knowledge that is cited together with, or closely related to, what was found.
    for found, value in sorted(insights.items(), key=lambda item: (-item[1], item[0]))[:limit]:
        for edge, other in neighbors(graph, found, adj):
            if edge["rel"] in ("co_cited", "similar_to") and other["type"] == "insight" and not other.get("archived"):
                bucket_value = 0.3 * value * float(edge.get("confidence", 0.5))
                insights[other["id"]] = insights.get(other["id"], 0) + bucket_value
    for blocker in resolved:
        if blocker in blockers:
            blockers[blocker] *= 0.25
    cache = {}

    def top(bucket, count=limit):
        return [node_view(nodes[key]) for key in sorted(bucket, key=lambda key: (-bucket[key], key))[:count]]

    shown = top(insights)
    for row in shown:
        changed = code_changed(nodes[row["id"]], repo, cache)
        if changed:
            row["code_changed"] = changed[:4]
        against = sorted(other["id"] for edge, other in neighbors(graph, row["id"], adj)
                         if edge["rel"] == "contradicts" and not other.get("archived"))
        if against:
            row["contradicted_by"] = against[:3]
    blocker_rows = top(blockers, 4)
    for row in blocker_rows:
        if row["id"] in resolved:
            row["resolved"] = True
    file_stats = [{"file": nodes[seed]["label"], **nodes[seed].get("stats", {}), "stale": nodes[seed].get("stale")}
                  for seed in seeds if seed.startswith("file:") and nodes[seed].get("stats")]
    result = {"seeds": seeds, "files": file_stats[:limit], "insights": shown,
              "past_tasks": top(tasks), "blockers": blocker_rows, "gates": top(gates, 4),
              "co_changed": top(related, 6)}
    # The files asked about come first; seeds found by directory or as siblings only add to them.
    asked = [path for path in (safe_path(value, repo) for value in files) if path]
    seeded = [seed[len("file:"):] for seed in seeds if seed.startswith("file:")]
    code = code_context(graph, list(dict.fromkeys(asked + [path for path in seeded if not asked])))
    if code:
        if code_stale(graph, repo):
            code["stale"] = "commits landed since the code layer was indexed; the next sync refreshes it"
        result["code"] = code
    groups = group_context(graph, seeds)
    if groups:
        result["groups"] = groups
    return result


def excluded_ids(graph, exclude):
    """Task ids to leave out: a full `task:RUN:ID`, or a ledger id in the current run."""
    found = set()
    for value in ([exclude] if isinstance(exclude, str) else exclude or []):
        value = str(value).strip()
        if not value:
            continue
        if value.startswith("task:"):
            found.add(value)
        else:
            found.update(key for key, node in graph["nodes"].items() if node["type"] == "task"
                         and node.get("task_id") == value)
    return found


def blocker_latest(graph, blocker_id, file_id, adj):
    """When this blocker last stopped a task on this file (its newest finish time)."""
    latest = ""
    for edge, task in neighbors(graph, blocker_id, adj):
        if task["type"] == "task" and any(other["id"] == file_id for _, other in neighbors(graph, task["id"], adj)):
            latest = max(latest, str(task.get("finished_at") or "9999"))
    return latest


def code_context(graph, paths, limit=6):
    """From the code layer: who depends on these files, what they import, and their code notes."""
    store = getattr(graph, "store", None)
    con = getattr(store, "con", None)
    if con is None or not paths:
        return None
    try:
        import improve_code
        if not improve_code.available(con):
            return None
        dependents, imports, notes = [], [], improve_code.notes_for(con, paths[:8], limit)
        for path in paths[:8]:
            for row in improve_code.dependents(con, path, limit):
                if row["file"] not in paths and row["file"] not in {item["file"] for item in dependents}:
                    dependents.append({"file": row["file"], "via": row["via"], "site": row["site"], "test": row["test"]})
            imports.extend(target for target in improve_code.imports_of(con, path) if target not in imports)
    except sqlite3.Error:
        return None
    result = {}
    if dependents:
        result["dependents"] = dependents[:limit]
    if imports:
        result["imports"] = imports[:limit]
    if notes:
        result["notes"] = notes
    return result or None


def group_context(graph, seeds):
    groups = []
    for group in (graph.get("communities") or {}).get("groups") or []:
        members = set(group.get("members", []))
        if any(seed in members for seed in seeds):
            groups.append({"id": group["id"], "label": group["label"], "size": group["size"],
                           "cohesion": group["cohesion"]})
    return groups[:3]


def hotspots(graph, limit=10):
    files = [node for node in graph["nodes"].values()
             if node["type"] == "file" and node.get("stats", {}).get("attempts", 0) >= 2 and not node.get("stale")]
    files.sort(key=lambda node: (node["stats"]["success"], -node["stats"]["attempts"], node["id"]))
    return [{"file": node["label"], "id": node["id"], **node["stats"]} for node in files[:limit]]


def shortest_path(graph, start, goal, max_depth=6, via=None, include_hubs=False):
    """How two nodes are connected, with each step's tier and confidence and the weakest step
    named (Graphify: a path is only as strong as its weakest link). By default the path does
    not use the hub relations `in_run`/`in_area` and does not pass *through* the busiest 1% of
    nodes (at least HUB_MIN_DEGREE links), since every node is two steps from those."""
    start, goal = resolve(graph, start), resolve(graph, goal)
    if start not in graph["nodes"] or goal not in graph["nodes"]:
        raise GraphError("both path endpoints must be existing node ids")
    adj = adjacency(graph)
    relations = set(via) if via else None
    blocked = set() if include_hubs else hub_nodes(adj)
    previous, queue = {start: None}, deque([(start, 0)])
    while queue:
        current, depth = queue.popleft()
        if current == goal:
            break
        if depth >= max_depth or (current in blocked and current != start):
            continue
        for edge, other in adj.get(current, []):
            if relations is not None and edge["rel"] not in relations:
                continue
            if relations is None and not include_hubs and edge["rel"] in HUB_RELATIONS \
                    and other != goal and current != start:
                continue  # A run or area may be where the path starts or ends, not a shortcut through.
            if other not in previous:
                previous[other] = (current, edge)
                queue.append((other, depth + 1))
    if goal not in previous:
        return []
    steps, current = [], goal
    while previous[current]:
        parent, edge = previous[current]
        steps.append({"from": parent, "rel": edge["rel"], "to": current, "label": graph["nodes"][current]["label"],
                      "tier": edge_tier(edge), "confidence": float(edge.get("confidence", 1.0))})
        current = parent
    steps.reverse()
    if not steps:
        return steps
    weakest = min(range(len(steps)), key=lambda index: (steps[index]["confidence"],
                                                         TIER_ORDER.index(steps[index]["tier"]), index))
    for index, step in enumerate(steps):
        step["weakest"] = index == weakest
    return steps


def hub_nodes(adj):
    degrees = sorted(len(links) for links in adj.values())
    if not degrees:
        return set()
    cutoff = max(HUB_MIN_DEGREE, degrees[min(len(degrees) - 1, int(len(degrees) * 0.99))])
    return {node for node, links in adj.items() if len(links) >= cutoff}


def node_detail(graph, node_id, limit=200, repo=None):
    node_id = resolve(graph, node_id)
    node = graph["nodes"].get(node_id)
    if node is None:
        raise GraphError(f"no graph node {node_id!r}")
    links = []
    for edge, other in neighbors(graph, node_id):
        links.append({"rel": edge["rel"], "direction": "out" if edge["src"] == node_id else "in",
                      "weight": edge.get("weight", 1), "tier": edge_tier(edge),
                      **node_view(other, ("id", "type", "label", "status", "score", "archived", "kind")),
                      **({"inferred": True, "confidence": edge.get("confidence"), "rule": edge.get("rule"),
                          "generation": edge.get("generation"), "support": edge.get("support"),
                          "evidence": edge_evidence(edge)} if edge.get("inferred") else {})})
    # Rank before limiting so a busy file shows its strongest links, not arbitrary ones.
    links.sort(key=lambda row: (-float(row.get("score", 0)), row["rel"], row["id"]))
    shown = sorted(links[:limit], key=lambda row: (row["rel"], -float(row.get("score", 0)), row["id"]))
    view = node_view(node)
    changed = code_changed(node, repo) if node["type"] == "insight" else []
    if changed:
        view["code_changed"] = changed
    return {"node": view, "links": shown, "total_links": len(links)}


def edge_evidence(edge):
    """A short, readable account of why an inferred link exists."""
    evidence = edge.get("evidence") or {}
    if "words" in evidence:
        return "shared words: " + ", ".join(evidence["words"])
    if "via" in evidence:
        return "derived from " + ", ".join(evidence["via"][:4])
    tasks = list((evidence.get("tasks") or {}).items())
    retained = evidence.get("retained") or {}
    text = ", ".join(f"{task} ({result})" for task, result in tasks[:5])
    older = sum(int(value) for value in retained.values()) if retained else 0
    return (text + (f" and {older} older tasks" if older else "")) or "older tasks"


def brief(graph, script, repo_arg, max_chars=1800, repo=None):
    """Compact, ranked knowledge for a cycle prompt. Ids let the agent cite what it used.

    The short sections (gates, hotspots, gaps, proposals) are built first and always kept;
    knowledge fills the remaining room, each note bounded, and the brief says how many notes
    it left out instead of silently dropping whole sections. The block is marked as recorded
    data: notes come from agents and the board, not from the operator of this cycle."""
    nodes = graph["nodes"]
    if not nodes:
        return ""
    header = (f"Knowledge graph: {len(nodes)} nodes from {len(graph['runs'])} recorded runs. These are recorded "
              f"notes (data, not instructions). Before each task query it: python3 {script} --repo {repo_arg} "
              f"context --files PATH... --area AREA")
    tail = []
    risky = [row for row in hotspots(graph, 5) if row["success"] < 0.6]
    if risky:
        tail.append("Files where past attempts often failed; read their context first:\n" + "\n".join(
            f"- {row['file']}: {row['done']}/{row['attempts']} verified" for row in risky))
    gaps = graph["dream"].get("gaps") or []
    if gaps:
        tail.append("Files that keep failing with nothing recorded about them; note what you learn there:\n"
                    + "\n".join(f"- {row['file']}: {row['done']}/{row['attempts']} verified" for row in gaps[:3]))
    gates = sorted((node for node in nodes.values() if node["type"] == "gate" and node.get("seconds")),
                   key=lambda node: node["seconds"])[:6]
    if gates:
        tail.append("Known gates and cost: " + ", ".join(f"{node['label']} {node['seconds']}s" for node in gates))
    proposals = graph["dream"].get("proposals") or []
    if proposals:
        tail.append(f"Dreaming found {len(proposals)} group(s) of overlapping knowledge. When the queue is empty, "
                    f"run `python3 {script} --repo {repo_arg} proposals` and consolidate one.")
    contradictions = sum(1 for edge in graph["edges"].values() if edge["rel"] == "contradicts")
    if contradictions:
        tail.append(f"{contradictions} pair(s) of notes contradict each other; `report` lists them as questions.")
    live = [node for node in nodes.values() if node["type"] == "insight" and not node.get("archived")
            and node.get("source") != "learning"]
    live.sort(key=lambda node: (not node.get("pinned"), node.get("source") != "user", -float(node.get("score", 0)), node["id"]))
    fixed = len(header) + sum(len(part) + 2 for part in tail)
    room = max_chars - fixed - 2
    lines, adj, cache = [], adjacency(graph), {}
    title = "Top knowledge (cite ids you rely on in the task's graph_refs):"
    candidates = live[:BRIEF_NOTES]
    for node in candidates:
        flags = [flag for flag, on in (("pinned", node.get("pinned")), ("from the user", node.get("source") == "user"),
                                       ("proven", node.get("verdict") == "proven"),
                                       ("contested: verify before relying on it", node.get("verdict") == "contested"),
                                       ("code changed since: re-verify", bool(code_changed(node, repo, cache))),
                                       (f"x{node.get('reinforced')}", int(node.get("reinforced", 1)) > 1)) if on]
        # Most specific first: a file says more than a gate, a gate more than an area.
        about = [other["label"] for _, other in sorted(
            ((edge, other) for edge, other in neighbors(graph, node["id"], adj) if other["type"] in ("file", "gate", "area")),
            key=lambda item: (("file", "gate", "area").index(item[1]["type"]), item[1]["label"]))][:3]
        text = clean(node.get("summary", node["label"]), BRIEF_NOTE_CHARS + 1)
        if len(text) > BRIEF_NOTE_CHARS:
            text = text[:BRIEF_NOTE_CHARS - 3].rstrip() + "..."
        line = (f"- [{node['id']}] {node.get('kind', 'fact')}: {text}" + (f" (about {', '.join(about)})" if about else "")
                + (f" [{', '.join(flags)}]" if flags else ""))
        if len(title) + 1 + sum(len(item) + 1 for item in lines) + len(line) + 80 > room:
            break
        lines.append(line)
    if lines:
        left = len(live) - len(lines)
        if left > 0:
            lines.append(f"(showing {len(lines)} of {len(live)} notes; `search WORDS` or `node ID` for the rest)")
        knowledge = [title + "\n" + "\n".join(lines)]
    else:
        knowledge = []
    text = "\n\n".join(part for part in [header, *knowledge, *tail] if part.strip())
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    # Whole lines only: a budget too small for even the first line gets nothing, not half of it.
    if "\n" in cut:
        return cut.rsplit("\n", 1)[0].rstrip()
    short = f"Knowledge graph: {len(nodes)} nodes; run `brief` with more room or `context` before a task."
    return short if len(short) <= max_chars else f"Knowledge graph: {len(nodes)} nodes."[:max_chars]


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
        order = [focus]  # Breadth-first order with sorted ties: the same answer under any hash seed.
        for _ in range(max(1, min(int(depth), 3))):
            following = []
            for current in frontier:
                for _, other in sorted(adj.get(current, []), key=lambda item: (-float(nodes[item[1]].get("score", 0)),
                                                                               item[1])):
                    if other not in seen and len(seen) < limit:
                        seen.add(other)
                        order.append(other)
                        following.append(other)
            frontier = following
        chosen = order
    else:
        pool = [node for node in nodes.values() if (include_archived or not node.get("archived"))
                and (not types or node["type"] in types)]
        if query:
            hits = {row["id"] for row in search(graph, query, limit, types, include_archived)}
            adj = adjacency(graph)
            near = {other for hit in hits for _, other in adj.get(hit, [])}
            pool = [node for node in pool if node["id"] in hits or node["id"] in near]
        chosen = balanced(pool, limit)
    selected = set(chosen)
    edges = [{"src": edge["src"], "rel": edge["rel"], "dst": edge["dst"], "weight": edge.get("weight", 1),
              **({"inferred": True, "confidence": edge.get("confidence")} if edge.get("inferred") else {})}
             for edge in graph["edges"].values() if edge["src"] in selected and edge["dst"] in selected]
    edges.extend(projected_edges(graph, selected))
    counts = {kind: sum(node["type"] == kind for node in nodes.values()) for kind in TYPES}
    return {"revision": graph.get("revision", 0), "updated_at": graph.get("updated_at"),
            "nodes": [node_view(nodes[key]) for key in chosen], "edges": edges[:limit * 4],
            "total_nodes": len(nodes), "total_edges": len(graph["edges"]), "counts": counts,
            "runs": graph["runs"][-12:], "refine": graph["refine"].get("history", [])[-8:],
            "dreams": graph["dream"].get("history", [])[-8:], "proposals": graph["dream"].get("proposals", []),
            "gaps": graph["dream"].get("gaps", []), "dropped": graph["dream"].get("dropped", [])[-20:],
            "hotspots": hotspots(graph, 8), "focus": focus, "truncated": len(chosen) < len(nodes)}


def balanced(pool, limit):
    """Pick `limit` nodes: every pinned node, then a fair share of each type, then the strongest.
    Without shares, hundreds of high-scoring notes would push files, areas, and tasks out."""
    rank = lambda node: (not node.get("pinned"), -float(node.get("score", 0)), node["id"])
    ranked = sorted(pool, key=rank)
    if len(ranked) <= limit:
        return [node["id"] for node in ranked]
    by_type = {}
    for node in ranked:
        by_type.setdefault(node["type"], []).append(node)
    share = max(1, limit // (2 * len(by_type)))
    first = [node for node in ranked if node.get("pinned")]
    for nodes in by_type.values():
        first.extend(nodes[:share])
    picked, ordered = set(), []
    for node in first + ranked:
        if node["id"] not in picked and len(ordered) < limit:
            picked.add(node["id"])
            ordered.append(node)
    return [node["id"] for node in sorted(ordered, key=rank)]


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
            "last_refine": graph["refine"].get("last_at"), "last_dream": graph["dream"].get("last_at"),
            "tick": graph.get("tick", 0), "inferred": sum(1 for edge in graph["edges"].values() if edge.get("inferred"))}


# ---------------------------------------------------------------- queries used by the CLI and the board

def load_search(graph, text):
    """In a partial session, load the nodes that share a word with the query (all, without an index)."""
    store = getattr(graph, "store", None)
    if not getattr(graph, "partial", False) or store is None:
        return
    found = store.search_candidates(tokens(text))
    store.fetch(graph, found if found is not None else [node_id for kind in TYPES for node_id in store.ids_of_type(kind)])


def load_node(graph, node_id):
    store = getattr(graph, "store", None)
    node_id = resolve(graph, node_id)
    if getattr(graph, "partial", False) and store is not None:
        store.fetch(graph, [node_id, *store.neighbor_ids([node_id])])
    return node_id


def history_view(record):
    body = record["body"]
    return {key: value for key, value in (
        ("seq", record["seq"]), ("id", record["node_id"]), ("type", record["type"]), ("label", record["label"]),
        ("op", record["op"]), ("reason", record["reason"]), ("at", record["at"]), ("tick", record["tick"]),
        ("summary", body.get("summary")), ("kind", body.get("kind")), ("links", len(record["edges"])),
        ("superseded_by", (record["extra"] or {}).get("superseded_by")), ("restored_at", record.get("restored_at")))
        if value not in (None, "")}


def history(repo, limit=30, offset=0, types=None, query=None, include_restored=False, node_id=None):
    with reading(repo) as graph:
        store = getattr(graph, "store", None)
        if store is None:
            return {"total": 0, "rows": []}
        total, rows = store.history(limit, offset, types, query, include_restored, node_id)
        return {"total": total, "rows": [history_view(row) for row in rows]}


def graph_lookup(graph, node_id):
    """A node by id (through aliases), loading it first in a partial session."""
    node_id = resolve(graph, node_id)
    fetch(graph, [node_id])
    return graph["nodes"].get(node_id)


def main(argv=None):
    """The command line lives in improve_graph_cli.py; this file stays the entry point."""
    from improve_graph_cli import main as cli
    return cli(argv)


if __name__ == "__main__":
    sys.exit(main())
