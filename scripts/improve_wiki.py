"""The knowledge wiki: a readable, token-cheap digest of the knowledge graph.

The graph (`.improve/graph.db`) is what a run relies on while it works. The wiki is built from
it only when an improve session finishes: by the supervisor's run-end dream, or by a
foreground run's final `improve_graph.py sync --dream`. It never changes during a run, and no
cycle reads it.

Each build writes `.improve/wiki/`:

    index.md        counts, key knowledge, areas, what the last session changed
    knowledge.md    every live note, grouped by kind, with its verdict and where it applies
    areas/<a>.md    one page per area: its success rate, notes, and files that resist change
    changes.md      per session: knowledge added, removed (archived, merged, or forgotten), and
                    verdicts that changed; the last CHANGE_SESSIONS sessions
    dreams.md       what the latest dream inferred, archived, forgot, and proposes
    wiki.toon       every page as one TOON document, for agents and tools (far fewer tokens)
    manifest.json   the snapshot the next build compares against

The wiki lives and cleans itself up the way the graph does: new knowledge appears, knowledge
the graph archives, merges, or forgets leaves the pages and is listed in the change log with
the reason (the graph's history still holds it), and pages for areas that no longer exist are
deleted. Standard library only.
"""
import json
import os
from pathlib import Path
import re
import tempfile

import improve_toon

CHANGE_SESSIONS = 12
KEY_NOTES = 8
NOTES_PER_PAGE = 200
FILES_PER_AREA = 10
ROWS_PER_CHANGE = 40
SESSION_ROWS = 30
HOTSPOT_ROWS = 8
HOTSPOT_SUCCESS = 0.5
RELATED_PER_NOTE = 5
RELATED_RELATIONS = ("similar_to", "co_cited", "relates_to", "supersedes")
TEXT_LIMIT = 300
KIND_ORDER = ("decision", "convention", "pitfall", "technique", "lesson", "fact")


class WikiError(ValueError):
    pass


# ---------------------------------------------------------------- when to build

def run_state(state):
    """The current run's id and whether it has finished. Under the supervisor, supervisor.json
    alone decides: an agent can write an early outcome to run.json that the supervisor rejects.
    Without one, run.json decides; with neither, no run is live."""
    for name in ("supervisor.json", "run.json"):
        path = Path(state) / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            value = None
        if not isinstance(value, dict):
            # An unreadable record cannot prove the run has finished.
            return None, False
        return value.get("run_id") or value.get("started_at"), bool(value.get("outcome"))
    return None, True


# ---------------------------------------------------------------- pages

def clip(text, limit=TEXT_LIMIT):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def slug(name):
    value = re.sub(r"[^a-z0-9._-]+", "-", str(name).lower()).strip("-.")
    return value or "area"


def file_areas(graph):
    """Each file's areas: only tasks link files to areas (a task touches files and is in an area)."""
    touched, areas = {}, {}
    for edge in graph.get("edges", {}).values():
        src, dst, rel = edge.get("src", ""), edge.get("dst", ""), edge.get("rel")
        if rel == "touches" and dst.startswith("file:"):
            touched.setdefault(src, set()).add(dst)
        elif rel == "in_area" and dst.startswith("area:"):
            areas.setdefault(src, set()).add(dst[5:])
    result = {}
    for task, files in touched.items():
        for path in files:
            result.setdefault(path, set()).update(areas.get(task, ()))
    return result


def note_links(graph, by_file=None):
    """For each note, the areas and files it is about or applies to; a note about a file also
    belongs to that file's areas."""
    by_file = file_areas(graph) if by_file is None else by_file
    links = {}
    for edge in graph.get("edges", {}).values():
        src, dst = edge.get("src", ""), edge.get("dst", "")
        if src.startswith("insight:") and edge.get("rel") in ("about", "applies_to"):
            row = links.setdefault(src, {"areas": set(), "files": set()})
            if dst.startswith("area:"):
                row["areas"].add(dst[5:])
            elif dst.startswith("file:"):
                row["files"].add(dst[5:])
                row["areas"].update(by_file.get(dst, ()))
    return links


def note_relations(graph, live_ids):
    """What dreaming (and people) linked between live notes: related ids per note, strongest
    first, and contradicting pairs. A contradiction is not a relation."""
    related, contradictions = {}, []
    for edge in graph.get("edges", {}).values():
        left, right, rel = edge.get("src"), edge.get("dst"), edge.get("rel")
        if left not in live_ids or right not in live_ids or left == right:
            continue
        if rel == "contradicts":
            contradictions.append((min(left, right), max(left, right), float(edge.get("confidence") or 1)))
        elif rel in RELATED_RELATIONS:
            weight = float(edge.get("confidence") or 1)
            related.setdefault(left, {})[right] = max(weight, related.get(left, {}).get(right, 0))
            related.setdefault(right, {})[left] = max(weight, related.get(right, {}).get(left, 0))
    # Dreaming can infer both similar_to and contradicts for one pair; a contradiction wins.
    for left, right, _ in contradictions:
        related.get(left, {}).pop(right, None)
        related.get(right, {}).pop(left, None)
    ordered = {key: " ".join(other for other, _ in sorted(value.items(), key=lambda item: (-item[1], item[0]))
                             [:RELATED_PER_NOTE]) for key, value in related.items()}
    return ordered, sorted(set(contradictions), key=lambda row: (-row[2], row[0], row[1]))


def note_row(node, links, related=None):
    linked = links.get(node["id"], {"areas": set(), "files": set()})
    return {"id": node["id"], "kind": node.get("kind") or "fact",
            "text": clip(node.get("summary") or node.get("label")),
            "verdict": node.get("verdict") or "", "score": round(float(node.get("score") or 0), 2),
            "pinned": bool(node.get("pinned")), "source": node.get("source") or "agent",
            # Space-separated, so every row is flat and TOON writes the notes as one table.
            "areas": " ".join(sorted(linked["areas"])), "files": " ".join(sorted(linked["files"])[:5]),
            "related": (related or {}).get(node["id"], "")}


def rank(node):
    return (not node.get("pinned"), -float(node.get("score") or 0), node["id"])


def removal_reason(graph, node_id):
    node = graph["nodes"].get(node_id)
    if node is not None and node.get("archived"):
        reason = node["archived"]
        return reason if isinstance(reason, str) else "archived"
    target = (graph.get("aliases") or {}).get(node_id)
    if target:
        return f"merged into {target}"
    return "forgotten (the graph's history keeps it)"


def pages(graph, previous, built_at, run_id):
    """Every page as plain data, and the manifest the next build compares against."""
    nodes = graph["nodes"]
    by_file = file_areas(graph)
    links = note_links(graph, by_file)
    live = sorted((node for node in nodes.values() if node["type"] == "insight" and not node.get("archived")),
                  key=rank)
    related, contradicting = note_relations(graph, {node["id"] for node in live})
    rows = [note_row(node, links, related) for node in live]
    archived = sum(1 for node in nodes.values() if node["type"] == "insight" and node.get("archived"))

    # What this session changed, compared with the build before it. A second build for the
    # same session replaces that session's entry and compares with the same base.
    previous = previous or None
    same = previous is not None and run_id and previous.get("run_id") == run_id and previous.get("sessions")
    before = (previous.get("base") if same else previous.get("notes")) if previous else {}
    before = before or {}
    now = {row["id"]: {"text": row["text"], "verdict": row["verdict"]} for row in rows}
    added = [{"id": key, "text": now[key]["text"]} for key in now if key not in before]
    removed = [{"id": key, "text": value.get("text", ""), "reason": removal_reason(graph, key)}
               for key, value in sorted(before.items()) if key not in now]
    verdicts = [{"id": key, "text": now[key]["text"], "from": before[key].get("verdict") or "none",
                 "to": now[key]["verdict"] or "none"}
                for key in now if key in before and (before[key].get("verdict") or "") != now[key]["verdict"]]
    first = previous is None or bool(same and previous["sessions"][-1].get("first"))
    session = {"run_id": run_id or "", "built_at": built_at, "first": first,
               # A first build is the starting point, not a list of changes: counts only.
               "added": [] if first else added[:ROWS_PER_CHANGE], "removed": removed[:ROWS_PER_CHANGE],
               "verdicts": verdicts[:ROWS_PER_CHANGE],
               "counts": {"added": len(added), "removed": len(removed), "verdicts": len(verdicts)}}
    earlier = list((previous or {}).get("sessions") or [])
    if same:
        earlier = earlier[:-1]
    sessions = earlier + [session]
    sessions = sessions[-CHANGE_SESSIONS:]

    areas = sorted((node for node in nodes.values() if node["type"] == "area"), key=lambda node: node["label"])
    files = [node for node in nodes.values() if node["type"] == "file" and not node.get("archived")]
    area_pages, area_rows = {}, []
    for area in areas:
        name = area["label"]
        stats = area.get("stats") or {}
        notes = [row for row in rows if name in row["areas"].split()][:NOTES_PER_PAGE]
        in_area = sorted((node for node in files if name in by_file.get(node["id"], ())),
                         key=lambda node: (-(node.get("stats") or {}).get("attempts", 0), node["id"]))
        area_files = [{"path": node["label"], "attempts": (node.get("stats") or {}).get("attempts", 0),
                       "success": (node.get("stats") or {}).get("success")} for node in in_area[:FILES_PER_AREA]]
        summary = {"name": name, "page": f"areas/{slug(name)}", "attempts": stats.get("attempts", 0),
                   "done": stats.get("done", 0), "rejected": stats.get("rejected", 0),
                   "success": stats.get("success"), "notes": len(notes)}
        area_rows.append(summary)
        # Note ids only: the rows live once, on the knowledge page, so the TOON document stays small.
        area_pages[summary["page"]] = {"title": f"Area: {name}", **summary, "knowledge": [row["id"] for row in notes],
                                       "files": area_files}

    dream = graph.get("dream") or {}
    latest = (dream.get("history") or [{}])[-1]
    inferred = latest.get("inferred") or {}
    dreams = {"title": "Dreams", "at": latest.get("at") or "", "tick": latest.get("tick", graph.get("tick", 0)),
              "inferred": {"new": inferred.get("new", 0), "kept": inferred.get("kept", 0),
                           "dropped": inferred.get("dropped", 0)},
              "archived": latest.get("archived", 0), "forgotten": latest.get("forgotten", 0),
              "proven": latest.get("proven", 0), "contested": latest.get("contested", 0),
              "proposals": [{"id": row.get("id", ""), "members": len(row.get("members") or []),
                             "text": clip(row.get("text") or row.get("summary") or "")}
                            for row in (dream.get("proposals") or [])[:ROWS_PER_CHANGE]],
              "contradictions": [{"a": left, "a_text": clip(nodes[left].get("summary") or nodes[left].get("label")),
                                  "b": right, "b_text": clip(nodes[right].get("summary") or nodes[right].get("label")),
                                  "confidence": round(confidence, 2)}
                                 for left, right, confidence in contradicting[:ROWS_PER_CHANGE]],
              "gaps": [clip(row if isinstance(row, str) else row.get("text") or row.get("label") or json.dumps(row))
                       for row in (dream.get("gaps") or [])[:ROWS_PER_CHANGE]]}

    # Each recorded session, newest first: what it attempted and the knowledge it recorded.
    recorded = {}
    for node in nodes.values():
        if node["type"] == "insight" and node.get("run_id"):
            recorded[node["run_id"]] = recorded.get(node["run_id"], 0) + 1
    runs = sorted((row for row in graph.get("runs") or [] if isinstance(row, dict) and row.get("run_id")),
                  key=lambda row: (str(row.get("ingested_at") or row.get("first_ingested_at") or ""),
                                   row.get("seq", 0)), reverse=True)
    sessions_page = {"title": "Sessions", "runs": [
        # Built only after the current session finished, so a run with no outcome crashed or
        # was interrupted; it is never live.
        {"run_id": str(row["run_id"]), "outcome": str(row.get("outcome") or "ended without an outcome"),
         "tasks": row.get("tasks", 0), "done": row.get("done", 0), "rejected": row.get("rejected", 0),
         "blocked": row.get("blocked", 0), "notes": recorded.get(row["run_id"], 0),
         "at": str(row.get("ingested_at") or "")} for row in runs[:SESSION_ROWS]]}
    try:
        from improve_graph import hotspots
        resisting = [{"file": row["file"], "attempts": row.get("attempts", 0), "done": row.get("done", 0),
                      "success": row.get("success", 0)}
                     for row in hotspots(graph, HOTSPOT_ROWS * 3) if row.get("success", 1) < HOTSPOT_SUCCESS]
    except (KeyError, TypeError):
        resisting = []

    groups = [{"kind": kind, "notes": [row for row in rows if row["kind"] == kind][:NOTES_PER_PAGE]}
              for kind in KIND_ORDER + tuple(sorted({row["kind"] for row in rows} - set(KIND_ORDER)))]
    result = {
        "index": {"title": "Knowledge wiki", "built_at": built_at, "run_id": run_id or "",
                  "revision": graph.get("revision", 0), "tick": graph.get("tick", 0),
                  "counts": {"notes": len(rows), "archived": archived, "areas": len(areas), "files": len(files),
                             "proven": sum(1 for row in rows if row["verdict"] == "proven"),
                             "contested": sum(1 for row in rows if row["verdict"] == "contested")},
                  "key": [{"id": row["id"], "kind": row["kind"], "text": row["text"]} for row in rows[:KEY_NOTES]],
                  "areas": area_rows, "hotspots": resisting[:HOTSPOT_ROWS], "last_session": session["counts"]},
        "knowledge": {"title": "Knowledge", "groups": [group for group in groups if group["notes"]]},
        "changes": {"title": "Changes", "sessions": list(reversed(sessions))},
        "dreams": dreams,
        "sessions": sessions_page,
        **area_pages,
    }
    manifest = {"built_at": built_at, "run_id": run_id or "", "revision": graph.get("revision", 0),
                "notes": now, "base": before, "sessions": sessions, "pages": sorted(result)}
    return result, manifest


# ---------------------------------------------------------------- Markdown

def md(text):
    """Note text in Markdown: no raw HTML, no accidental emphasis or links."""
    text = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_\[\]])", r"\\\1", text)


def note_line(row):
    tags = [row["kind"]] + ([row["verdict"]] if row.get("verdict") else []) + (["pinned"] if row.get("pinned") else [])
    where = ", ".join([f"area {area}" for area in row.get("areas", "").split()]
                      + [f"`{path}`" for path in row.get("files", "").split()])
    line = f"- **{', '.join(tags)}**: {md(row['text'])}" + (f" ({where})" if where else "")
    return line + (f"; related: {', '.join(f'`{key}`' for key in row['related'].split())}" if row.get("related") else "")


def render(result):
    """Every page as Markdown, keyed by page name; area pages resolve their note ids."""
    rows = {row["id"]: row for group in result["knowledge"]["groups"] for row in group["notes"]}
    return {name: markdown(name, page, rows) for name, page in result.items()}


def markdown(name, page, rows=None):
    lines = [f"# {md(page.get('title', name))}", ""]
    if name == "index":
        counts = page["counts"]
        lines += [f"Built {page['built_at']} at the end of session `{page['run_id']}`, graph revision "
                  f"{page['revision']}, dream tick {page['tick']}. Rebuilt only when a session finishes.", "",
                  f"{counts['notes']} live notes ({counts['proven']} proven, {counts['contested']} contested, "
                  f"{counts['archived']} archived), {counts['areas']} areas, {counts['files']} files.", "",
                  "Pages: [Knowledge](knowledge.md) · [Changes](changes.md) · [Dreams](dreams.md) · "
                  "[Sessions](sessions.md)", "",
                  "## Key knowledge", ""]
        lines += [f"- **{row['kind']}**: {md(row['text'])}" for row in page["key"]] or ["- None recorded yet."]
        lines += ["", "## Areas", ""]
        lines += [f"- [{md(row['name'])}]({row['page']}.md): {row['notes']} notes, {row['done']}/{row['attempts']} "
                  f"attempts verified" for row in page["areas"]] or ["- None yet."]
        lines += ["", "## Files that resist change", ""]
        lines += [f"- `{row['file']}`: {row['done']}/{row['attempts']} attempts verified"
                  for row in page.get("hotspots", [])] or ["- None: no file keeps failing."]
        last = page["last_session"]
        lines += ["", "## Last session", "",
                  f"{last['added']} added, {last['removed']} removed, {last['verdicts']} verdicts changed. "
                  "See [Changes](changes.md)."]
    elif name == "knowledge":
        for group in page["groups"]:
            lines += [f"## {group['kind'].capitalize()}", ""] + [note_line(row) for row in group["notes"]] + [""]
        if not page["groups"]:
            lines.append("No knowledge recorded yet.")
    elif name == "changes":
        for session in page["sessions"]:
            counts = session["counts"]
            lines += [f"## Session `{session['run_id']}`, built {session['built_at']}", "",
                      f"{counts['added']} added, {counts['removed']} removed, {counts['verdicts']} verdicts changed."
                      + (" First build of the wiki." if session.get("first") else ""), ""]
            lines += [f"- Added: {md(row['text'])}" for row in session["added"]]
            lines += [f"- Removed ({md(row['reason'])}): {md(row['text'])}" for row in session["removed"]]
            lines += [f"- Verdict {row['from']} → {row['to']}: {md(row['text'])}" for row in session["verdicts"]]
            lines.append("")
    elif name == "sessions":
        lines += ["Each recorded improve session, newest first: what it attempted and the knowledge it recorded.", ""]
        lines += [f"- `{row['run_id']}` ({md(row['outcome'])}): {row['done']}/{row['tasks']} done, "
                  f"{row['rejected']} rejected, {row['blocked']} blocked; {row['notes']} notes recorded"
                  for row in page["runs"]] or ["- No session recorded yet."]
    elif name == "dreams":
        inferred = page["inferred"]
        lines += [f"Latest dream: tick {page['tick']}" + (f", {page['at']}" if page["at"] else "") + ".", "",
                  f"- Inferred links: {inferred['new']} new, {inferred['kept']} kept, {inferred['dropped']} dropped",
                  f"- Knowledge archived {page['archived']}, forgotten {page['forgotten']}",
                  f"- Verdicts: {page['proven']} proven, {page['contested']} contested", "",
                  "## Consolidation proposals", ""]
        lines += [f"- `{row['id']}` ({row['members']} notes): {md(row['text'])}" for row in page["proposals"]] or ["- None."]
        lines += ["", "## Contradictions", "", "Notes that say opposite things; decide which holds and rate the other.", ""]
        lines += [f"- {md(row['a_text'])} (`{row['a']}`) **contradicts** {md(row['b_text'])} (`{row['b']}`)"
                  for row in page.get("contradictions", [])] or ["- None."]
        lines += ["", "## Knowledge gaps", ""] + ([f"- {md(row)}" for row in page["gaps"]] or ["- None."])
    else:
        success = page.get("success")
        lines += [f"{page['done']}/{page['attempts']} attempts verified"
                  + (f" (success {success:.2f})" if isinstance(success, (int, float)) else "") + ".", "",
                  "[Back to the index](../index.md)", "", "## Knowledge", ""]
        lines += [note_line(rows[key]) for key in page["knowledge"] if key in (rows or {})] or [
            "- None recorded for this area."]
        lines += ["", "## Files", ""]
        lines += [f"- `{row['path']}`: {row['attempts']} attempts" for row in page["files"]] or ["- None recorded."]
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- disk

def atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".wiki-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def write(directory, result, manifest):
    """Write every page, the TOON document, and the manifest; delete pages that no longer exist."""
    directory = Path(directory)
    if directory.is_symlink():
        raise WikiError("the wiki directory must not be a symlink")
    for name, text in render(result).items():
        atomic_text(directory / f"{name}.md", text)
    areas = directory / "areas"
    if areas.is_dir() and not areas.is_symlink():
        for path in areas.glob("*.md"):
            if f"areas/{path.stem}" not in result:
                path.unlink()
    atomic_text(directory / "wiki.toon", improve_toon.dumps({"built_at": manifest["built_at"], "pages": result}))
    atomic_text(directory / "manifest.json", json.dumps(manifest, indent=1, ensure_ascii=False) + "\n")


def read_manifest(directory):
    path = Path(directory) / "manifest.json"
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def build(graph, repo, built_at=None):
    """Rebuild the wiki from `graph` when the current session has finished; otherwise skip."""
    from improve_graph import fetch_types, stamp
    state = Path(repo) / ".improve"
    run_id, finished = run_state(state)
    if not finished:
        return {"skipped": "a run is still live"}
    fetch_types(graph, "insight", "area", "file")
    directory = state / "wiki"
    result, manifest = pages(graph, read_manifest(directory), built_at or stamp(), run_id)
    write(directory, result, manifest)
    last = manifest["sessions"][-1]["counts"]
    return {"pages": len(result), "notes": len(manifest["notes"]), **last, "path": str(directory)}
