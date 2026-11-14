"""Command line for the code layer (`improve_graph.py code|impact|tests-for|cycles ...`). Standard library.

Kept apart from improve_graph_cli.py so code-layer commands can grow without touching the
knowledge-graph CLI. `add_commands` registers the subcommands; `run` executes them. Names are
never guessed: an ambiguous name raises GraphAmbiguous (exit 4), an unknown one GraphNotFound
(exit 5). Answers that could be read as "nothing depends on this" say what the code layer does
not cover (unsupported languages, files that do not parse, an index older than HEAD).
"""

from pathlib import Path
import posixpath

import improve_graph as graph_module
from improve_graph import GraphAmbiguous as Ambiguous, GraphError, GraphNotFound as NotFound, locked, reading

ACTIONS = ("code", "impact", "tests-for", "cycles")


def add_commands(command, shared):
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


def run(args, repo):
    return code_command(args, repo)
