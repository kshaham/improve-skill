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
import sqlite3
import sys

import improve_graph as graph_module
from improve_graph import (DREAM_RELATIONS, INSIGHT_KINDS, TYPES, USER_RELATIONS, GraphBusy, graph_path,
                           load, locked, reading)
import improve_code_cli
import improve_graph_journal as journal
from improve_runtime import StateError
import improve_toon as toon

DEFAULT_WIDTH = 160
CHARS_PER_TOKEN = 3.5
BUDGET_PASSES = 12
EXIT_AMBIGUOUS = 4
EXIT_NOT_FOUND = 5
# Never shortened by --width: a cut identifier or path cannot be used.
KEEP = frozenset(("id", "ids", "file", "files", "src", "dst", "from", "to", "seeds", "node_id", "path", "paths", "commit",
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


def budgeted(result, budget, draw):
    """Graphify's token-budgeted answer: drop the lowest-ranked rows of the longest lists until
    the rendering fits, then say exactly what was left out and what budget shows it all.

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
            lists = [(key, rows) for key, rows in row_lists(trimmed) if rows]
            if not lists:
                break
            key, rows = max(lists, key=lambda item: (len(item[1]), str(item[0])))
            saved += scale * (len(json.dumps(rows[-1], separators=(",", ":"))) + 1)
            cut(trimmed, key)
        text = draw(trimmed)
        if not any(rows for _, rows in row_lists(trimmed)):
            break
    shown = sum(len(rows) for _, rows in row_lists(trimmed))
    needed = math.ceil(len(full) / CHARS_PER_TOKEN) + 20
    note = (f"[!] TRUNCATED: showing {shown} of {total} rows (highest ranked kept); --budget {needed} shows everything"
            if shown < total else f"[!] Complete answer is over budget ({math.ceil(len(text) / CHARS_PER_TOKEN)} tokens)")
    return note + "\n" + text


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
    parser.add_argument("--budget", type=int, default=None, help="answer in at most this many tokens (context, search, node)")
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
    improve_code_cli.add_commands(command, shared)
    command("communities", "groups of files that change together or depend on each other")
    command("questions", "what is worth a look: contested, contradicted, changed, failing, bridging")
    command("diff", "what the latest dreams changed")
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
        elif args.action == "history":
            result = graph_module.history(repo, args.limit, max(0, args.offset), args.type, args.query, args.all, args.id)
        elif args.action in improve_code_cli.ACTIONS:
            result = improve_code_cli.run(args, repo)
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


if __name__ == "__main__":
    sys.exit(main())
