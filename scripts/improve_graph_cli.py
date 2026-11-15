"""Command line for the knowledge graph (`improve_graph.py` delegates here). Standard library.

Exit codes: 0 an answer (or a write queued while another process held the graph), 2 an
error, 3 busy for a read-modify-write that cannot be queued (retry later), 4 an ambiguous name
(the candidates are printed), 5 nothing by that name. Graphify's rule: never guess a name.

Output: TOON by default (`--format json` or `IMPROVE_FORMAT=json` for JSON). Long text cells
are cut at `--width` (160) with a marker unless `--full`; identifiers and paths are never cut.
`context`, `search`, and `node` accept `--budget TOKENS`: the lowest-ranked rows go first and
the answer says what it left out. Every output switch works before or after the subcommand.
"""

import argparse
import json
import math
from pathlib import Path
import posixpath
import sqlite3
import sys

import improve_graph as graph_module
from improve_graph import (DREAM_RELATIONS, GraphError, INSIGHT_KINDS, TYPES, USER_RELATIONS, GraphBusy, graph_path,
                           load, locked, reading)
import improve_graph_journal as journal
from improve_runtime import StateError
import improve_toon as toon

DEFAULT_WIDTH = 160
CHARS_PER_TOKEN = 3.5
BUDGET_PASSES = 12
EXIT_AMBIGUOUS = 4
EXIT_NOT_FOUND = 5
# Never shortened by --width: a cut identifier or path cannot be used.
KEEP = frozenset(("id", "ids", "topic", "file", "files", "src", "dst", "from", "to", "seeds", "node_id", "path", "paths", "commit",
                  "site", "target", "key", "superseded_by", "run_id", "task_id", "hub", "members", "label", "about",
                  "code_changed", "contradicted_by", "imports", "rule", "proposal", "other", "consolidated_from",
                  "run", "tests", "symbols", "lines", "changed"))  # Commands and names an agent runs as given.
QUEUEABLE = ("note", "link", "rate", "unlink")


Ambiguous = graph_module.GraphAmbiguous
NotFound = graph_module.GraphNotFound


# ---------------------------------------------------------------- rendering

def tabular(value, delimiter=","):
    """Every array of objects in its shortest TOON form (see improve_toon.compact_rows)."""
    if isinstance(value, list):
        items = [tabular(item, delimiter) for item in value]
        return toon.compact_rows(items, delimiter) if items and all(isinstance(item, dict) for item in items) else items
    if isinstance(value, dict):
        return {key: tabular(item, delimiter) for key, item in value.items()}
    if isinstance(value, str):
        # Text stored before surrogates were cleaned on input must not make a whole answer fail.
        return value.encode("utf-8", "replace").decode("utf-8")
    return value


def render(result, fmt="toon", delimiter=","):
    if isinstance(result, str):
        return result
    if fmt == "json":
        return json.dumps(result, indent=2, ensure_ascii=False)
    symbol = toon.delimiter_symbol(delimiter)
    return toon.dumps(tabular(result, symbol), delimiter=symbol)


def bounded(result, args):
    """Cut long text cells (not ids or paths) unless --full or JSON output."""
    if args.full or args.format == "json" or isinstance(result, str):
        return result  # Text answers (brief, path, messages) are already sized by their command.
    width = args.width if args.width is not None else DEFAULT_WIDTH
    return toon.truncate(result, width, keep=KEEP) if width else result


def row_lists(result):
    """The cuttable lists of a result: (key, list) pairs; `seeds` is never cut."""
    if isinstance(result, list):
        return [(None, result)]
    if isinstance(result, dict):
        found = [(key, value) for key, value in result.items() if isinstance(value, list) and key not in ("seeds",)]
        for key, value in result.items():
            if isinstance(value, dict) and key == "code":
                found.extend((f"code.{child}", item) for child, item in value.items() if isinstance(item, list))
        return found
    return []


def cut(result, key):
    if key is None:
        result.pop()
    elif key.startswith("code."):
        result["code"][key[5:]].pop()
    else:
        result[key].pop()


def budgeted(result, budget, draw, order=None):
    """Graphify's token-budgeted answer: drop the lowest-ranked rows of the longest lists until
    the rendering fits, then say exactly what was left out and what budget shows it all.
    `order` names lists to empty one after another instead (rows that others refer to last).
    Each list keeps its top row (an empty answer reads as "nothing found"); only lists that an
    `order` cuts before its last one may empty.

    Each row's cost is estimated once (its share of the rendered size, scaled from its JSON
    size), the cut is planned from those estimates, and the result is rendered only a few
    times to correct the estimate: re-rendering after every dropped row was quadratic
    (66 s for a 1000-row search)."""
    if not budget or budget <= 0:
        return draw(result)
    limit = int(budget * CHARS_PER_TOKEN)
    full = draw(result)
    if len(full) <= limit:
        return full
    total = sum(len(rows) for _, rows in row_lists(result))
    trimmed = json.loads(json.dumps(result))
    scale = len(full) / max(1, len(json.dumps(result, separators=(",", ":"))))
    text = full
    for _ in range(BUDGET_PASSES):
        overshoot = len(text) - limit
        if overshoot <= 0:
            break
        saved = 0.0
        while saved < overshoot:
            lists = cuttable(trimmed, order)
            if not lists:
                break
            ordered = [item for name in (order or ()) for item in lists if item[0] == name]
            key, rows = ordered[0] if ordered else max(lists, key=lambda item: (len(item[1]), str(item[0])))
            saved += scale * (len(json.dumps(rows[-1], separators=(",", ":"))) + 1)
            cut(trimmed, key)
        text = draw(trimmed)
        if not cuttable(trimmed, order):
            break
    shown = sum(len(rows) for _, rows in row_lists(trimmed))
    needed = math.ceil(len(full) / CHARS_PER_TOKEN) + 20
    note = (f"[!] TRUNCATED: showing {shown} of {total} rows (highest ranked kept); --budget {needed} shows everything"
            if shown < total else f"[!] Complete answer is over budget ({math.ceil(len(text) / CHARS_PER_TOKEN)} tokens)")
    if "\u2026[+" in text:
        note += "; cells ending in \u2026[+N] are shortened (--full shows them)"
    return note + "\n" + text


def cuttable(result, order):
    """Lists that may lose a row: any with two or more, or one an `order` empties before its last."""
    early = set((order or ())[:-1])
    return [(key, rows) for key, rows in row_lists(result) if len(rows) > 1 or (rows and key in early)]


def print_context(result):
    lines = []
    for row in result["files"]:
        lines.append(f"{row['file']}: {row['done']}/{row['attempts']} past attempts verified"
                     + (" (file no longer tracked)" if row.get("stale") else ""))
    known = [seed[len("file:"):] for seed in result.get("seeds", []) if seed.startswith("file:")]
    unmeasured = [path for path in known if path not in {row["file"] for row in result["files"]}]
    if unmeasured:
        lines.append("Known files here without outcome statistics yet: " + ", ".join(unmeasured[:8])
                     + (f" and {len(unmeasured) - 8} more" if len(unmeasured) > 8 else ""))
    for group in result.get("groups") or []:
        lines.append(f"Module group {group['id']} around {group['label']} ({group['size']} files, "
                     f"cohesion {group['cohesion']})")

    def knowledge(node):
        flags = []
        if node.get("code_changed"):
            flags.append("code changed since: re-verify (" + ", ".join(node["code_changed"][:2]) + ")")
        if node.get("contradicted_by"):
            flags.append("contradicted by " + ", ".join(node["contradicted_by"]))
        if node.get("verdict"):
            flags.append(node["verdict"])
        return (f"[{node['id']}] {node.get('kind', '')}: {node.get('summary', node['label'])}"
                + (f" [{'; '.join(flags)}]" if flags else ""))

    for title, key, draw in (
            ("Knowledge", "insights", knowledge),
            ("Past tasks here", "past_tasks", lambda n: f"[{n.get('status')}] {n['label']}" + (f" - {n['outcome_note']}" if n.get("outcome_note") else "")),
            ("Blockers seen here", "blockers", lambda n: n["label"] + (" (resolved: later work here succeeded)" if n.get("resolved") else "")),
            ("Gates used here", "gates", lambda n: n["label"] + (f" ({n['seconds']}s)" if n.get("seconds") else "")),
            ("Files usually changed together", "co_changed", lambda n: n["label"])):
        if result.get(key):
            lines.append(title + ":")
            lines.extend("- " + draw(node) for node in result[key])
    code = result.get("code") or {}
    if code.get("dependents"):
        lines.append("Files that depend on these (a change may break them):")
        lines.extend(f"- {row['file']} ({row['via']} at {row['site']}{', test' if row.get('test') else ''})"
                     for row in code["dependents"])
    if code.get("imports"):
        lines.append("These files import: " + ", ".join(code["imports"]))
    if code.get("notes"):
        lines.append("Code notes:")
        lines.extend(f"- {row['path']}:{row['line']} {row['tag']}: {row['text']}" for row in code["notes"])
    return "\n".join(lines) or "The graph has nothing on these yet. Record what you learn with `note`."


# ---------------------------------------------------------------- parser

def build_parser():
    parser = argparse.ArgumentParser(prog="improve_graph.py", description=graph_module.__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--repo", required=True)
    toon.output_options(parser)
    parser.add_argument("--budget", type=int, default=None, help="answer in at most this many tokens (context, search, node, wiki)")
    shared = argparse.ArgumentParser(add_help=False)
    toon.output_options(shared, suppress=True)
    shared.add_argument("--budget", type=int, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="action", required=True)

    def command(name, text):
        return sub.add_parser(name, help=text, parents=[shared])

    sync_parser = command("sync", "ingest the current run (and archived runs on first use)")
    sync_parser.add_argument("--history", action="store_true", help="re-ingest every archived run")
    sync_parser.add_argument("--refine", action="store_true", help="refine after ingesting")
    sync_parser.add_argument("--dream", action="store_true", help="dream (which refines) after ingesting")
    sync_parser.add_argument("--code", action="store_true", help="refresh the code layer even if no refinement is due")
    command("refine", "merge duplicates, follow renames, rescore, fade stale knowledge, bound size")
    command("dream", "infer links, archive and forget what is unused, propose consolidations, group files")
    brief_parser = command("brief", "ranked knowledge for the next cycle")
    brief_parser.add_argument("--max-chars", type=int, default=1800)
    ctx = command("context", "what is known about files/an area before working on them")
    ctx.add_argument("--files", nargs="*", default=[])
    ctx.add_argument("--area")
    ctx.add_argument("--text")
    ctx.add_argument("--limit", type=int, default=8)
    ctx.add_argument("--exclude", action="append", help="a task id to leave out (the task you are working on)")
    ctx.add_argument("--json", action="store_true", help="structured JSON instead of the text brief")
    ctx.add_argument("--toon", action="store_true", help="structured TOON instead of the text brief")
    find = command("search", "rank nodes by words")
    find.add_argument("text")
    find.add_argument("--type", action="append", choices=TYPES)
    find.add_argument("--limit", type=int, default=15)
    show = command("node", "a node and its links")
    show.add_argument("id")
    path = command("path", "how two nodes are connected, with the weakest step named")
    path.add_argument("start")
    path.add_argument("goal")
    path.add_argument("--via", action="append", help="only these relations (repeatable)")
    path.add_argument("--all", action="store_true", help="also route through run/area hubs and the busiest nodes")
    path.add_argument("--max-depth", type=int, default=6)
    hot = command("hotspots", "files where verified success is lowest")
    hot.add_argument("--limit", type=int, default=10)
    note = command("note", "record knowledge; repeats reinforce it")
    note.add_argument("--text", required=True)
    note.add_argument("--kind", choices=INSIGHT_KINDS, default="fact")
    note.add_argument("--about", nargs="*", default=[], help="file:PATH, area:NAME, gate:NAME, or a node id")
    connect = command("link", "relate two nodes")
    connect.add_argument("src")
    connect.add_argument("rel", choices=USER_RELATIONS)
    connect.add_argument("dst")
    unlink = command("unlink", "say an inferred link is wrong; it is never inferred again")
    unlink.add_argument("src")
    unlink.add_argument("rel", choices=DREAM_RELATIONS)
    unlink.add_argument("dst")
    unlink.add_argument("--reason")
    verdict = command("rate", "say whether knowledge helped")
    verdict.add_argument("id")
    verdict.add_argument("verdict", choices=("useful", "wrong", "pin", "unpin", "archive", "restore"))
    past = command("history", "removed nodes and why (newest first)")
    past.add_argument("--type", action="append", choices=TYPES)
    past.add_argument("--query")
    past.add_argument("--id")
    past.add_argument("--limit", type=int, default=30)
    past.add_argument("--offset", type=int, default=0, help="skip this many newest rows (paging)")
    past.add_argument("--all", action="store_true", help="include removals that were already restored")
    back = command("restore", "bring a removed node back with its links")
    back.add_argument("id")
    back.add_argument("--reason", default="needed again")
    command("proposals", "groups of overlapping knowledge dreaming suggests consolidating")
    merge = command("consolidate", "replace a proposal's notes with one stronger note")
    merge.add_argument("--proposal", required=True)
    merge.add_argument("--text", required=True)
    merge.add_argument("--kind", choices=INSIGHT_KINDS, default="technique")
    merge.add_argument("--consume", nargs="*", default=[], help="member ids to fold in (default: all)")
    command("stats", "graph size, last refinement, last dream, and the code layer")
    add_code_commands(command, shared)
    command("communities", "groups of files that change together or depend on each other")
    command("questions", "what is worth a look: contested, contradicted, changed, failing, bridging")
    command("diff", "what the latest dreams changed")
    page = command("wiki", "the knowledge wiki, rebuilt from the graph when a session finishes")
    page.add_argument("page", nargs="?", default="index",
                      help="index (default), knowledge, changes, dreams, topics, sessions, areas/NAME; `pages` "
                           "lists them; `build` rebuilds the wiki once the session has finished; --budget N fits "
                           "the answer in N tokens")
    page.add_argument("--markdown", action="store_true", help="print the page's Markdown instead of TOON or JSON")
    out = command("report", "the graph as a Markdown report")
    out.add_argument("--out", help="write to this file instead of printing")
    dump = command("export", "the graph as JSON, GraphML, or Markdown")
    dump.add_argument("--as", dest="export_as", choices=("json", "graphml", "markdown"), default="json")
    dump.add_argument("--out", help="write to this file instead of printing")
    return parser


# ---------------------------------------------------------------- commands

def write_args(args, repo):
    if args.action == "note":
        run_id, _ = graph_module.run_identity(repo / ".improve")
        return {"text": args.text, "kind": args.kind, "source": "agent", "about": list(args.about), "run_id": run_id}
    if args.action == "link":
        return {"src": args.src, "rel": args.rel, "dst": args.dst, "source": "agent"}
    if args.action == "rate":
        return {"id": args.id, "verdict": args.verdict, "source": "agent"}
    if args.action == "unlink":
        return {"src": args.src, "rel": args.rel, "dst": args.dst, "by": "agent", "reason": args.reason, "source": "agent"}
    if args.action == "restore":
        return {"id": args.id, "reason": args.reason, "source": "agent"}
    return {"proposal": args.proposal, "text": args.text, "kind": args.kind, "consume": list(args.consume),
            "source": "agent"}


def view_of(action, result):
    if action in ("note", "rate", "restore", "consolidate"):
        return graph_module.node_view(result)
    return result


def run_write(args, repo):
    values = {key: value for key, value in write_args(args, repo).items() if value is not None}
    partial = args.action in ("note", "link", "rate")
    try:
        with locked(repo, partial=partial) as graph:
            return view_of(args.action, journal.apply_write(graph, args.action, values, repo))
    except GraphBusy:
        if args.action not in QUEUEABLE:
            raise
        entry = journal.queue_pending(repo, args.action, values)
        return {"queued": True, "id": entry["id"], "op": args.action,
                "message": "another process holds the graph; this write is queued and applied by the next writer"}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    toon.resolve_format(args)
    repo = Path(args.repo).expanduser().resolve()
    script = Path(graph_module.__file__).resolve()
    draw = lambda value: render(value, args.format, args.delimiter)  # noqa: E731
    try:
        if args.action == "sync":
            result = graph_module.sync(repo, include_history=True if args.history else None, refine_after=args.refine,
                                       dream_after=args.dream, code_index=True if args.code else None)
        elif args.action in ("note", "link", "rate", "unlink", "restore", "consolidate"):
            result = run_write(args, repo)
        elif args.action in ("refine", "dream"):
            from improve_dream import dream
            with locked(repo) as graph:
                result = graph_module.refine(graph, repo) if args.action == "refine" else dream(graph, repo)
        elif args.action == "wiki":
            result = wiki_command(args, repo)
            if isinstance(result, str):
                return write_out(result, None)
            if args.budget:
                # Only the flattened knowledge page carries a list of notes and their total.
                listed = len(result["notes"]) if args.page == "knowledge" else 0
                if args.page == "knowledge" and result.get("total", 0) > listed:
                    # The wiki already keeps only the strongest notes of each kind; a budget cannot
                    # bring the rest back, so say so before any budget note.
                    print(f"[!] The wiki lists {listed} of {result['total']} notes (the strongest of each kind); "
                          "`search` the graph for the rest")
                # A topic's notes go before any topic, so no note names a topic left out.
                order = ("notes", "groups") if args.page == "topics" else None
                print(budgeted(bounded(result, args), args.budget, draw, order=order))
                return 0
        elif args.action == "history":
            result = graph_module.history(repo, args.limit, max(0, args.offset), args.type, args.query, args.all, args.id)
        elif args.action in CODE_ACTIONS:
            result = run_code_command(args, repo)
        elif args.action in ("context", "search", "node"):
            with reading(repo) as graph:
                warn_newer(graph)
                if args.action == "context":
                    graph_module.load_context(graph, args.files, args.area, repo, args.text)
                    result = graph_module.context(graph, args.files, args.area, args.text, args.limit, repo, args.exclude)
                    journal.record_reads(repo, [row["id"] for row in result["insights"]],
                                         [seed[len("file:"):] for seed in result["seeds"] if seed.startswith("file:")]
                                         + [path for path in (graph_module.safe_path(value, repo) for value in args.files) if path])
                    if args.json:
                        args.format = "json"
                    elif not args.toon:
                        text_result = bounded(result, args)
                        print(budgeted(text_result, args.budget, print_context))
                        return 0
                elif args.action == "search":
                    graph_module.load_search(graph, args.text)
                    result = graph_module.search(graph, args.text, args.limit, args.type)
                    journal.record_reads(repo, [row["id"] for row in result], command="search")
                else:
                    node_id = graph_module.load_node(graph, args.id)
                    if node_id not in graph["nodes"]:
                        raise NotFound(f"no graph node {args.id!r}")
                    result = graph_module.node_detail(graph, node_id, repo=repo)
                    journal.record_reads(repo, [result["node"]["id"]], command="node")
            print(budgeted(bounded(result, args), args.budget, draw))
            return 0
        else:
            graph = load(graph_path(repo), repair=False)
            warn_newer(graph)
            result = whole_graph_command(args, graph, repo, script, parser)
            if isinstance(result, tuple):  # (text, out path)
                return write_out(*result)
        print(draw(bounded(result, args)))
        return 0
    except Ambiguous as exc:
        print(f"improve-graph: {exc}", file=sys.stderr)
        print(draw({"ambiguous": True, "candidates": exc.candidates}))
        return EXIT_AMBIGUOUS
    except NotFound as exc:
        print(f"improve-graph: {exc}", file=sys.stderr)
        return EXIT_NOT_FOUND
    except GraphBusy as exc:
        print(f"improve-graph: {exc}", file=sys.stderr)
        return 3
    except sqlite3.OperationalError as exc:
        retryable = any(word in str(exc).lower() for word in ("locked", "busy"))
        print(f"improve-graph: {exc}" + ("; retry later" if retryable else ""), file=sys.stderr)
        return 3 if retryable else 2
    except (StateError, OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print(f"improve-graph: {exc}", file=sys.stderr)
        return 2


def whole_graph_command(args, graph, repo, script, parser):
    import improve_analysis as analysis
    if args.action == "brief":
        return graph_module.brief(graph, script, repo, args.max_chars, repo=repo) or "The knowledge graph is empty."
    if args.action == "path":
        steps = graph_module.shortest_path(graph, args.start, args.goal, args.max_depth, args.via, args.all)
        return steps or "No connection within six steps (hub links skipped; --all includes them)."
    if args.action == "hotspots":
        return graph_module.hotspots(graph, args.limit)
    if args.action == "proposals":
        from improve_dream import proposal_view
        return [proposal_view(graph, row) for row in graph["dream"]["proposals"]] or "No proposals."
    if args.action == "communities":
        groups = (graph.get("communities") or {}).get("groups") or analysis.communities(graph)
        return [{**{key: group[key] for key in ("label", "size", "cohesion") if key in group}, "id": group.get("id", "-"),
                 "members": group["members"][:12], "more": max(0, len(group["members"]) - 12)} for group in groups] \
            or "No groups yet: files need co-changes or code links."
    if args.action == "questions":
        return analysis.suggested_questions(graph, repo)  # An empty list when nothing stands out.
    if args.action == "diff":
        rows = [{"tick": row.get("tick"), "at": row.get("at"), **(row.get("diff") or {})}
                for row in graph["dream"].get("history", [])[-5:] if row.get("diff")]
        return rows or "No dream has recorded a diff yet."
    if args.action == "report":
        return analysis.report(with_code(graph, repo), repo), args.out
    if args.action == "export":
        graph = with_code(graph, repo)
        text = {"json": analysis.export_json, "graphml": analysis.export_graphml,
                "markdown": lambda value: analysis.report(value, repo)}[args.export_as](graph)
        return text, args.out
    summary = graph_module.summary(graph)
    with reading(repo) as session:
        con = getattr(getattr(session, "store", None), "con", None)
        if con is not None:
            import improve_code
            summary["code"] = improve_code.stats(con)
    return summary


def with_code(graph, repo):
    """A whole-graph view with a live connection, so reports can include the code layer."""
    path = graph_path(repo)
    if not path.exists():
        return graph
    store = graph_module.Store(path, graph_module.search_words)
    try:
        if store.open(create=False):
            graph.store = store
    except (graph_module.StoreDamaged, sqlite3.Error):
        store.close()
    return graph


def wiki_command(args, repo):
    """Read a wiki page (TOON by default), list the pages, or rebuild it after a session."""
    import improve_wiki
    directory = repo / ".improve" / "wiki"
    if directory.is_symlink() or directory.parent.is_symlink():
        raise StateError("the wiki and state folders must not be symlinks")
    if args.page == "build":
        with locked(repo) as graph:
            result = improve_wiki.build(graph, repo)
        if result.get("skipped"):
            raise StateError("the wiki is rebuilt only when an improve session finishes; " + result["skipped"])
        return result
    document = directory / "wiki.toon"
    if not document.is_file() or document.is_symlink():
        raise NotFound("no wiki yet: it is built when an improve session finishes (the run-end dream, "
                       "or a foreground run's final report, `improve_ledger.py finish`, then `sync --dream`)")
    from improve_control import read_local
    # Bounded, and never through a symlinked file.
    pages = toon.loads(read_local(document, limit=4 * 1024 * 1024))["pages"]
    if args.page == "pages":
        return [{"page": name, "title": page.get("title", name)} for name, page in pages.items()]
    # Only a page the wiki built can be read: the name never becomes a free path.
    if args.page not in pages:
        raise NotFound(f"no wiki page {args.page!r}; `wiki pages` lists them")
    if args.markdown:
        return improve_wiki.render(pages)[args.page]
    page = pages[args.page]
    if args.page.startswith("areas/") or args.page == "topics":
        # The stored document lists note ids; an agent reading one page gets the notes themselves.
        knowledge = pages.get("knowledge") or {}
        rows = {row["id"]: row for row in [note for group in knowledge.get("groups", []) for note in group["notes"]]
                + list(knowledge.get("more", []))}
        brief = lambda key: {field: rows[key][field] for field in ("id", "kind", "verdict", "text")}  # noqa: E731
        resolve = lambda ids: [brief(key) for key in ids if key in rows]  # noqa: E731
        if args.page == "topics":
            page = {**page, "groups": [{**topic, "knowledge": resolve(topic["knowledge"])} for topic in page["groups"]]}
        else:
            page = {**page, "knowledge": resolve(page["knowledge"])}
    if args.page == "knowledge":
        # `more` only lets area pages resolve their ids inside the wiki document.
        page = {key: value for key, value in page.items() if key != "more"}
    if args.budget and args.page == "topics":
        # A budget cuts top-level lists only: lift the notes out of their topics, interleaved so
        # the weakest note of every topic goes first, and each topic keeps its row.
        topics = page.get("groups", [])
        notes = [{"topic": topic["id"], **topic["knowledge"][rank]}
                 for rank in range(max((len(topic["knowledge"]) for topic in topics), default=0))
                 for topic in topics if rank < len(topic["knowledge"])]
        return {**page, "groups": [{key: value for key, value in topic.items() if key != "knowledge"}
                                   for topic in topics], "notes": notes}
    if args.budget and args.page == "knowledge":
        # One list, strongest first across kinds, so a budget drops the weakest notes first.
        notes = [row for group in page.get("groups", []) for row in group["notes"]]
        notes.sort(key=lambda row: (not row.get("pinned"), -float(row.get("score") or 0), row["id"]))
        totals = {group["kind"]: group.get("total", len(group["notes"])) for group in page.get("groups", [])}
        return {"title": page.get("title", "Knowledge"), "totals": totals, "total": sum(totals.values()),
                "notes": notes}
    return page


def write_out(text, out):
    if not out:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return 0
    target = Path(out).expanduser()
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(text)
    temporary.replace(target)
    print(f"wrote {target}")
    return 0


def warn_newer(graph):
    if int(graph.get("version", graph_module.VERSION)) > graph_module.VERSION:
        print(f"improve-graph: warning: graph.db was written by a newer skill (format {graph['version']}); "
              f"this one reads format {graph_module.VERSION} and may miss fields", file=sys.stderr)


# ---------------------------------------------------------------------------------------------
# Command line for the code layer (`improve_graph.py code|impact|tests-for|cycles ...`). Standard library.
#
# `add_code_commands` registers the subcommands; `run_code_command` executes them. Names are
# never guessed: an ambiguous name raises GraphAmbiguous (exit 4), an unknown one GraphNotFound
# (exit 5). Answers that could be read as "nothing depends on this" say what the code layer does
# not cover (unsupported languages, files that do not parse, an index older than HEAD).

CODE_ACTIONS = ("code", "impact", "tests-for", "cycles")


def add_code_commands(command, shared):
    """Register the code-layer subcommands with the graph CLI's `command(name, help)` factory."""
    code = command("code", "the code layer: index it, list a file's symbols, find or search names")
    code_sub = code.add_subparsers(dest="code_action", required=True)
    code_index = code_sub.add_parser("index", help="bring the code layer up to date", parents=[shared])
    code_index.add_argument("--max-seconds", type=float, default=60.0)
    code_index.add_argument("--force", action="store_true", help="prune even when git ls-files shrank suspiciously")
    code_symbols = code_sub.add_parser("symbols", help="classes and functions defined in a file", parents=[shared])
    code_symbols.add_argument("path")
    code_find = code_sub.add_parser("find", help="where a name is defined: NAME, Class.method, or pkg.mod.name "
                                                 "(exit 4 ambiguous, 5 none)", parents=[shared])
    code_find.add_argument("name")
    code_search = code_sub.add_parser("search", help="rank symbols and code notes by words (identifier-aware; "
                                                     "exit 5 with suggestions when nothing matches)", parents=[shared])
    code_search.add_argument("words", nargs="+")
    code_search.add_argument("--limit", type=int, default=10)
    code_sub.add_parser("stats", help="size, coverage by language, and links by relation and tier", parents=[shared])
    blast = command("impact", "files that depend on a file, a symbol, or the uncommitted change, with the line that "
                              "uses it")
    blast.add_argument("target", nargs="?", help="PATH, file:PATH, PATH::Qual.name, or a symbol name")
    blast.add_argument("--changed", nargs="?", const="HEAD", metavar="REV",
                       help="impact of the working tree's changes against REV (default HEAD; untracked files included)")
    blast.add_argument("--depth", type=int, default=2)
    blast.add_argument("--limit", type=int, default=40)
    tests = command("tests-for", "test files that reach a target or the uncommitted change, nearest first, "
                                 "with a command to run them")
    tests.add_argument("target", nargs="?", help="PATH, file:PATH, PATH::Qual.name, or a symbol name")
    tests.add_argument("--changed", nargs="?", const="HEAD", metavar="REV", help="tests for the working tree's changes")
    tests.add_argument("--depth", type=int, default=3)
    tests.add_argument("--limit", type=int, default=20)
    loops = command("cycles", "import cycles (up to five files), shortest first")
    loops.add_argument("--max-length", type=int, default=5)
    loops.add_argument("--limit", type=int, default=50)
    loops.add_argument("--all", action="store_true", help="include imports inside functions and TYPE_CHECKING")


def unindexed_reason(repo, path):
    """Why a path that exists has no code facts, or None if it should have them."""
    import improve_code
    if not path or not (Path(repo) / path).is_file():
        return None
    if improve_code.language(path) is None:
        other = improve_code.UNSUPPORTED.get(posixpath.splitext(path)[1].lower())
        if other:
            return (f"{path} is {other}, which the code layer does not index; its dependents are unknown, "
                    "not zero (search the code instead)")
        return f"{path} is not source code the code layer reads"
    if improve_code.vendored(path):
        return f"{path} is vendored or minified code, which the code layer skips"
    return f"{path} is not indexed yet; run `code index`"


def resolve_symbol(con, target, repo=None):
    """A path, a symbol id, or a name -> what `impact` should start from; never a guess."""
    import improve_code
    path = graph_module.safe_path(target[len("file:"):] if target.startswith("file:") else target)
    if path and con.execute("SELECT 1 FROM code_file WHERE path=?", (path,)).fetchone():
        return path
    if "::" in target and con.execute("SELECT 1 FROM code_symbol WHERE id=?", (target,)).fetchone():
        return target
    reason = unindexed_reason(repo, path) if repo is not None else None
    if reason:
        raise NotFound(reason)
    found = improve_code.find(con, target)
    if not found:
        raise NotFound(f"nothing in the code layer is called {target!r} (run `code index` if the code changed)")
    if len(found) > 1:
        raise Ambiguous(f"{target!r} names {len(found)} symbols; pass one id", found[:20])
    return found[0]["id"]


def warnings_for(con, paths):
    """Files among `paths` whose facts are stale or missing because they do not parse."""
    import improve_code
    notes = []
    for path in paths:
        state = improve_code.file_state(con, path)
        if state and state.get("stale_parse"):
            notes.append(f"{path} does not parse now ({state['error']}); using its last good parse")
        elif state and state.get("error"):
            notes.append(f"{path} does not parse ({state['error']}); its symbols and calls are unknown")
    return notes


def annotate(result, con, repo, paths=()):
    """Add what the caller must know to read an answer correctly: parse problems, coverage
    gaps, and an index older than HEAD."""
    import improve_code
    notes = warnings_for(con, paths)
    if notes:
        result["warnings"] = notes
    line = improve_code.coverage_line(con)
    if line and ("not indexed" in line or "unparsed" in line or "stale" in line or "over the" in line):
        result["coverage"] = line
    fresh = improve_code.freshness(con, repo)
    if fresh["stale"]:
        result["code_stale"] = f"indexed at {fresh['indexed_head'][:10]}, HEAD is {fresh['head'][:10]}; run `code index`"
    return result


def groups_touched(graph, paths):
    """Module groups (from the last dream) containing any of these files."""
    found = []
    wanted = {"file:" + path for path in paths}
    for group in (graph.get("communities") or {}).get("groups") or []:
        if wanted & set(group.get("members", [])):
            found.append({"id": group.get("id", "-"), "label": group.get("label"), "size": group.get("size")})
    return found[:10]


def with_stats(graph, rows):
    for row in rows:
        node = graph_module.graph_lookup(graph, "file:" + row["file"])
        if node and node.get("stats"):
            row["success"] = node["stats"].get("success")
            row["attempts"] = node["stats"].get("attempts")
    return rows


def impact_command(args, con, graph, repo):
    import improve_code
    if args.changed is not None:
        if args.target:
            raise GraphError("pass a target or --changed, not both")
        result = improve_code.changed_impact(con, repo, args.changed, args.depth, args.limit)
        with_stats(graph, result["dependents"])
        touched = [change["file"] for change in result["changed"]] + [row["file"] for row in result["dependents"]]
        groups = groups_touched(graph, touched)
        if groups:
            result["groups"] = groups
        return annotate(result, con, repo, [change["file"] for change in result["changed"]])
    if not args.target:
        raise GraphError("impact needs a target or --changed")
    start = resolve_symbol(con, args.target, repo)
    rows, total = improve_code.impact(con, start, args.depth, args.limit)
    result = {"target": start, "total": total, "shown": len(rows), "dependents": with_stats(graph, rows)}
    return annotate(result, con, repo, [start.split("::", 1)[0]])


def tests_command(args, con, repo):
    import improve_code
    if args.changed is not None:
        if args.target:
            raise GraphError("pass a target or --changed, not both")
        changes, seeds, origins = improve_code.change_set(con, repo, args.changed)
        label = f"changes since {args.changed}"
    elif args.target:
        start = resolve_symbol(con, args.target, repo)
        seeds, origins = improve_code.start_seeds(con, start)
        label = start
    else:
        raise GraphError("tests-for needs a target or --changed")
    rows, total = improve_code.tests_for(con, repo, seeds, origins, args.depth, args.limit)
    result = {"target": label, "total": total, "tests": rows}
    if not rows:
        result["note"] = ("no test reaches this through calls, imports, or uses within depth "
                          f"{args.depth}; run the whole suite (or a wider --depth)")
    else:
        result["note"] = "run these first for fast feedback; they never replace the verification gate"
    return annotate(result, con, repo, sorted(origins)[:20])


def code_command(args, repo):
    import improve_code
    if args.action == "code" and args.code_action == "index":
        with locked(repo) as graph:
            return improve_code.index(graph.store.con, repo, args.max_seconds, force=args.force)
    with reading(repo) as graph:
        store = getattr(graph, "store", None)
        con = getattr(store, "con", None)
        if con is None or not improve_code.available(con):
            if args.action == "code" and args.code_action == "stats":
                return {"indexed": False}
            raise NotFound("the code layer is empty; run `code index` (every sync refreshes it)")
        if args.action == "impact":
            return impact_command(args, con, graph, repo)
        if args.action == "tests-for":
            return tests_command(args, con, repo)
        if args.action == "cycles":
            found = improve_code.find_cycles(con, args.max_length, max(1, args.limit), include_deferred=args.all)
            result = {"total": found["found"], "cycles": found["cycles"]}
            if not found["complete"]:
                result["note"] = (f"at least {found['found']} cycles; the search stopped early (--limit "
                                  f"{args.limit}); the import graph is dense")
            return result
        return code_subcommand(args, con, repo)


def code_subcommand(args, con, repo):
    import improve_code
    if args.code_action == "stats":
        result = improve_code.stats(con)
        line = improve_code.coverage_line(con)
        if line:
            result["coverage_line"] = line
        return result
    if args.code_action == "symbols":
        path = graph_module.safe_path(args.path, repo) or args.path
        rows = improve_code.symbols_of(con, path)
        state = improve_code.file_state(con, path)
        if not rows and state is None:
            raise NotFound(unindexed_reason(repo, path) or f"{path!r} is not in the code layer")
        result = {"path": path, "symbols": rows}
        if state and state.get("stale_parse"):
            result["stale_parse"] = True
            result["error"] = state["error"]
            result["note"] = "the file does not parse now; these symbols are from its last good parse"
        elif state and state.get("error"):
            result["error"] = state["error"]
        if state and state.get("generated"):
            result["generated"] = True
        return result
    if args.code_action == "search":
        text = " ".join(args.words)
        rows, suggestions = improve_code.search(con, text, max(1, args.limit))
        if not rows:
            hint = f"; did you mean: {', '.join(suggestions)}" if suggestions else ""
            raise NotFound(f"no symbol or code note matches {text!r}{hint}")
        return {"query": text, "results": rows}
    found = improve_code.find(con, args.name)
    if not found:
        raise NotFound(f"nothing is called {args.name!r}")
    if len(found) > 1:
        raise Ambiguous(f"{args.name!r} names {len(found)} symbols", found[:20])
    return found[0]


def run_code_command(args, repo):
    return code_command(args, repo)

if __name__ == "__main__":
    sys.exit(main())
