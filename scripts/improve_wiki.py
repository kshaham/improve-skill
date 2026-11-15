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
# An area page lists fewer notes, so the rows it needs beyond the kind caps stay few.
AREA_NOTES = 60
FILES_PER_AREA = 10
ROWS_PER_CHANGE = 40
SESSION_ROWS = 30
TOPIC_ROWS = 20
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
    # No run files: no improve session ran here, so none has finished.
    return None, False


# ---------------------------------------------------------------- pages

def as_text(value):
    """A graph field as text: the build reads the graph as it is and must never crash on an odd
    value, or the session ends without a wiki."""
    return value if isinstance(value, str) else ""


def as_number(value, fallback=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number == number and abs(number) != float("inf") else fallback


def as_dict(value):
    return value if isinstance(value, dict) else {}


def as_list(value):
    return value if isinstance(value, list) else []


def ratio(value):
    """A success rate, or None when the graph has none (never NaN)."""
    return None if value is None else round(as_number(value), 3)


def as_count(value):
    return int(as_number(value))


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
        src, dst, rel = as_text(edge.get("src")), as_text(edge.get("dst")), edge.get("rel")
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
        src, dst = as_text(edge.get("src")), as_text(edge.get("dst"))
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
        left, right, rel = as_text(edge.get("src")), as_text(edge.get("dst")), edge.get("rel")
        if left not in live_ids or right not in live_ids or left == right:
            continue
        if rel == "contradicts":
            contradictions.append((min(left, right), max(left, right), as_number(edge.get("confidence"), 1.0)))
        elif rel in RELATED_RELATIONS:
            weight = as_number(edge.get("confidence"), 1.0)
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
    return {"id": node["id"], "kind": as_text(node.get("kind")) or "fact",
            "text": clip(node.get("summary") or node.get("label")),
            "verdict": as_text(node.get("verdict")), "score": round(as_number(node.get("score")), 2),
            "pinned": bool(node.get("pinned")), "source": as_text(node.get("source")) or "agent",
            # Space-separated, so every row is flat and TOON writes the notes as one table.
            "areas": " ".join(sorted(linked["areas"])), "files": " ".join(sorted(linked["files"])[:5]),
            "related": (related or {}).get(node["id"], "")}


def rank(node):
    return (not node.get("pinned"), -as_number(node.get("score")), node["id"])


def removal_reason(graph, node_id):
    node = graph["nodes"].get(node_id)
    if node is not None and node.get("archived"):
        reason = node["archived"]
        return reason if isinstance(reason, str) else "archived"
    target = (graph.get("aliases") or {}).get(node_id)
    if target:
        merged = graph["nodes"].get(target) or {}
        words = merged.get("summary") or merged.get("label")
        return f'merged into "{clip(words, 120)}"' if words else f"merged into {target}"
    return "forgotten (the graph's history keeps it)"


def session_changes(graph, previous, rows, built_at, run_id):
    """What this session changed, compared with the build before it: the session entry, the
    bounded session list, and the snapshots (`now`, and the `before` it was compared with). A
    second build for the same session replaces that session's entry and compares with the same
    base."""
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
    sessions = (earlier + [session])[-CHANGE_SESSIONS:]
    return session, sessions, now, before


def area_pages(nodes, rows, by_file):
    """One page per area (note ids, files that touched it) and the index's area rows."""
    areas = sorted((node for node in nodes.values() if node["type"] == "area"), key=lambda node: (as_text(node.get("label")), node["id"]))
    files = [node for node in nodes.values() if node["type"] == "file" and not node.get("archived")]
    result, summaries, taken = {}, [], set()
    for area in areas:
        name = as_text(area.get("label")) or area["id"][5:]
        # Area names are free text, so two can share a slug ("UI" and "ui"): number the later ones.
        page_name, number = f"areas/{slug(name)}", 1
        while page_name in taken:
            number += 1
            page_name = f"areas/{slug(name)}-{number}"
        taken.add(page_name)
        stats = as_dict(area.get("stats"))
        in_notes = [row for row in rows if name in row["areas"].split()]
        in_area = sorted((node for node in files if name in by_file.get(node["id"], ())),
                         key=lambda node: (-as_count(as_dict(node.get("stats")).get("attempts")), node["id"]))
        area_files = [{"path": as_text(node.get("label")) or node["id"][5:],
                       "attempts": as_count(as_dict(node.get("stats")).get("attempts")),
                       "success": ratio(as_dict(node.get("stats")).get("success"))} for node in in_area[:FILES_PER_AREA]]
        summary = {"name": name, "page": page_name, "attempts": stats.get("attempts", 0),
                   "done": stats.get("done", 0), "rejected": stats.get("rejected", 0),
                   # The graph starts every area at a smoothed rate; untried, that is not evidence.
                   "success": ratio(stats.get("success")) if as_count(stats.get("attempts")) else None,
                   "notes": len(in_notes)}
        summaries.append(summary)
        # Note ids only: the rows live once, on the knowledge page, so the TOON document stays small.
        result[summary["page"]] = {"title": f"Area: {name}", **summary, "total": len(in_notes),
                                   "knowledge": [row["id"] for row in in_notes[:AREA_NOTES]],
                                   "files": area_files}
    return result, summaries, len(files)


def gap_text(row):
    """A knowledge gap as a sentence: a file whose tasks keep failing with nothing recorded."""
    if isinstance(row, str):
        return row
    if not isinstance(row, dict):
        return str(row)
    if row.get("file"):
        return (f"{row['file']}: {row.get('done', 0)}/{row.get('attempts', 0)} attempts verified, "
                "nothing recorded about why")
    return row.get("text") or row.get("label") or json.dumps(row)


def dreams_page(graph, contradicting):
    """The latest dream: what it inferred, archived, and forgot, its proposals and gaps, and the
    notes that contradict each other."""
    nodes = graph["nodes"]
    dream = graph.get("dream") or {}
    latest = as_dict((as_list(dream.get("history")) or [{}])[-1])
    inferred = as_dict(latest.get("inferred"))
    text = lambda key: clip(nodes[key].get("summary") or nodes[key].get("label"))  # noqa: E731
    return {"title": "Dreams", "at": latest.get("at") or "", "tick": latest.get("tick", graph.get("tick", 0)),
            "inferred": {"new": inferred.get("new", 0), "kept": inferred.get("kept", 0),
                         "dropped": inferred.get("dropped", 0)},
            "archived": latest.get("archived", 0), "forgotten": latest.get("forgotten", 0),
            "proven": latest.get("proven", 0), "contested": latest.get("contested", 0),
            # A proposal names its member notes; show their words, which is what a person merges.
            "proposals": [{"id": as_text(row.get("id")), "members": len(as_list(row.get("members"))),
                           "text": clip(" / ".join(text(member) for member in as_list(row.get("members"))
                                                   if member in nodes) or row.get("text") or "")}
                          for row in (dream.get("proposals") or [])[:ROWS_PER_CHANGE]],
            "contradictions": [{"a": left, "a_text": text(left), "b": right, "b_text": text(right),
                                "confidence": round(confidence, 2)}
                               for left, right, confidence in contradicting[:ROWS_PER_CHANGE]],
            "gaps": [clip(gap_text(row))
                     for row in (dream.get("gaps") or [])[:ROWS_PER_CHANGE]]}


def sessions_page(graph):
    """Each recorded session, newest first: what it attempted and the knowledge it recorded."""
    recorded = {}
    for node in graph["nodes"].values():
        if node["type"] == "insight" and node.get("run_id"):
            recorded[node["run_id"]] = recorded.get(node["run_id"], 0) + 1
    runs = sorted((row for row in graph.get("runs") or [] if isinstance(row, dict) and row.get("run_id")),
                  key=lambda row: (str(row.get("ingested_at") or row.get("first_ingested_at") or ""),
                                   as_number(row.get("seq"))), reverse=True)
    return {"title": "Sessions", "runs": [
        # Built only after the current session finished, so a run with no outcome crashed or
        # was interrupted; it is never live.
        {"run_id": str(row["run_id"]), "outcome": str(row.get("outcome") or "ended without an outcome"),
         "tasks": as_count(row.get("tasks")), "done": as_count(row.get("done")),
         "rejected": as_count(row.get("rejected")), "blocked": as_count(row.get("blocked")),
         "notes": recorded.get(row["run_id"], 0),
         "at": str(row.get("ingested_at") or "")} for row in runs[:SESSION_ROWS]]}


def resisting_files(graph):
    """Files that keep failing: the graph's hotspots under HOTSPOT_SUCCESS."""
    try:
        from improve_graph import hotspots
        return [{"file": row["file"], "attempts": row.get("attempts", 0), "done": row.get("done", 0),
                 "success": row.get("success", 0)}
                for row in hotspots(graph, HOTSPOT_ROWS * 3) if row.get("success", 1) < HOTSPOT_SUCCESS]
    except (KeyError, TypeError):
        return []


def topics_page(graph, rows, links):
    """The module groups dreaming discovered (files that change together or depend on each
    other), each with its files and the ids of the notes about them."""
    found = (graph.get("communities") or {}).get("groups") or []
    topics = []
    for group in found[:TOPIC_ROWS]:
        members = [member[5:] for member in as_list(group.get("members")) if as_text(member).startswith("file:")]
        inside = set(members)
        notes = [row["id"] for row in rows if links.get(row["id"], {}).get("files", set()) & inside]
        topics.append({"id": str(group.get("id") or ""), "label": str(group.get("label") or ""),
                       "size": as_count(group.get("size", len(members))),
                       "cohesion": round(as_number(group.get("cohesion")), 3),
                       "files": " ".join(members[:FILES_PER_AREA]), "total": len(notes),
                       "knowledge": notes[:AREA_NOTES]})
    return {"title": "Topics", "total": len(found), "groups": topics}


def knowledge_groups(rows):
    """Each kind shows its strongest notes; `total` says how many it has, so a capped page says so."""
    groups = []
    for kind in KIND_ORDER + tuple(sorted({row["kind"] for row in rows} - set(KIND_ORDER))):
        of_kind = [row for row in rows if row["kind"] == kind]
        groups.append({"kind": kind, "total": len(of_kind), "notes": of_kind[:NOTES_PER_PAGE]})
    return groups


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
    session, sessions, now, before = session_changes(graph, previous, rows, built_at, run_id)
    areas, area_rows, file_count = area_pages(nodes, rows, by_file)
    groups = knowledge_groups(rows)
    topics = topics_page(graph, rows, links)
    topic_notes = [key for topic in topics["groups"] for key in topic["knowledge"]]
    result = {
        "index": {"title": "Knowledge wiki", "built_at": built_at, "run_id": run_id or "",
                  "revision": graph.get("revision", 0), "tick": graph.get("tick", 0),
                  "counts": {"notes": len(rows), "archived": archived, "areas": len(area_rows), "files": file_count,
                             "proven": sum(1 for row in rows if row["verdict"] == "proven"),
                             "contested": sum(1 for row in rows if row["verdict"] == "contested")},
                  "key": [{"id": row["id"], "kind": row["kind"], "text": row["text"]} for row in rows[:KEY_NOTES]],
                  "areas": area_rows, "hotspots": resisting_files(graph)[:HOTSPOT_ROWS],
                  "last_session": session["counts"]},
        "knowledge": {"title": "Knowledge", "groups": [group for group in groups if group["notes"]],
                      # Rows that area pages list but the kind caps leave off this page, so every id
                      # an area lists resolves; this page itself still shows only `groups`.
                      "more": beyond_cap(groups, areas, rows, topic_notes)},
        "changes": {"title": "Changes", "sessions": list(reversed(sessions))},
        "dreams": dreams_page(graph, contradicting),
        "sessions": sessions_page(graph),
        "topics": topics,
        **areas,
    }
    manifest = {"built_at": built_at, "run_id": run_id or "", "revision": graph.get("revision", 0),
                "notes": now, "base": before, "sessions": sessions, "pages": sorted(result)}
    return result, manifest


# ---------------------------------------------------------------- Markdown

def plural(count, word, many=None):
    """`1 note`, `2 notes`; `many` for irregular plurals."""
    return f"{count} {word if count == 1 else (many or word + 's')}"


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
    knowledge = result["knowledge"]
    rows = {row["id"]: row for group in knowledge["groups"] for row in group["notes"]}
    rows.update((row["id"], row) for row in knowledge.get("more", []))
    return {name: markdown(name, page, rows) for name, page in result.items()}


def beyond_cap(groups, area_pages, rows, extra=()):
    shown = {row["id"] for group in groups for row in group["notes"]}
    wanted = ({key for page in area_pages.values() for key in page["knowledge"]} | set(extra)) - shown
    return [row for row in rows if row["id"] in wanted]


def capped_line(shown, total, kept="strongest"):
    """A capped list says how much it leaves out, and which it kept; the graph holds the rest."""
    if total and total > shown:
        return [f"Showing the {kept} {shown} of {total}; search the graph for the rest.", ""]
    return []


def md_index(page):
    """The overview: counts, key knowledge, areas, files that resist change, last session."""
    lines = []
    counts = page["counts"]
    lines += [f"Built {page['built_at']} at the end of session `{page['run_id']}`, graph revision "
              f"{page['revision']}, dream tick {page['tick']}. Rebuilt only when a session finishes.", "",
              f"{plural(counts['notes'], 'live note')} ({counts['proven']} proven, {counts['contested']} contested, "
              f"{counts['archived']} archived), {plural(counts['areas'], 'area')}, {plural(counts['files'], 'file')}.", "",
              "Pages: [Knowledge](knowledge.md) · [Changes](changes.md) · [Dreams](dreams.md) · "
              "[Topics](topics.md) · [Sessions](sessions.md)", "",
              "## Key knowledge", ""]
    lines += [f"- **{row['kind']}**: {md(row['text'])}" for row in page["key"]] or ["- None recorded yet."]
    lines += ["", "## Areas", ""]
    lines += [f"- [{md(row['name'])}]({row['page']}.md): {plural(row['notes'], 'note')}, {attempts_text(row)}"
              for row in page["areas"]] or ["- None yet."]
    lines += ["", "## Files that resist change", ""]
    lines += [f"- `{row['file']}`: {row['done']}/{plural(row['attempts'], 'attempt')} verified"
              for row in page.get("hotspots", [])] or ["- None: no file keeps failing."]
    last = page["last_session"]
    lines += ["", "## Last session", "",
              f"{last['added']} added, {last['removed']} removed, {plural(last['verdicts'], 'verdict')} changed. "
              "See [Changes](changes.md)."]
    return lines


def md_knowledge(page):
    """Every listed note, by kind."""
    lines = []
    for group in page["groups"]:
        lines += [f"## {group['kind'].capitalize()}", ""] + capped_line(len(group["notes"]), group.get("total"))
        lines += [note_line(row) for row in group["notes"]] + [""]
    if not page["groups"]:
        lines.append("No knowledge recorded yet.")
    return lines


def md_changes(page):
    """Per session: knowledge added, removed with the reason, and verdicts changed."""
    lines = []
    for session in page["sessions"]:
        counts = session["counts"]
        lines += [f"## Session `{session['run_id']}`, built {session['built_at']}", "",
                  f"{counts['added']} added, {counts['removed']} removed, {plural(counts['verdicts'], 'verdict')} changed."
                  + (" First build of the wiki." if session.get("first") else ""), ""]
        lines += [f"- Added: {md(row['text'])}" for row in session["added"]]
        lines += [f"- Removed ({md(row['reason'])}): {md(row['text'])}" for row in session["removed"]]
        lines += [f"- Verdict {row['from']} → {row['to']}: {md(row['text'])}" for row in session["verdicts"]]
        lines.append("")
    return lines


def md_sessions(page):
    """Each recorded session, newest first."""
    lines = []
    lines += ["Each recorded improve session, newest first: what it attempted and the knowledge it recorded.", ""]
    lines += [f"- `{row['run_id']}` ({md(row['outcome'])}): {row['done']}/{row['tasks']} done, "
              f"{row['rejected']} rejected, {row['blocked']} blocked; {plural(row['notes'], 'note')} recorded"
              for row in page["runs"]] or ["- No session recorded yet."]
    return lines


def md_dreams(page):
    """The latest dream, its proposals, contradictions, and gaps."""
    lines = []
    inferred = page["inferred"]
    lines += [f"Latest dream: tick {page['tick']}" + (f", {page['at']}" if page["at"] else "") + ".", "",
              f"- Inferred links: {inferred['new']} new, {inferred['kept']} kept, {inferred['dropped']} dropped",
              f"- Knowledge archived {page['archived']}, forgotten {page['forgotten']}",
              f"- Verdicts: {page['proven']} proven, {page['contested']} contested", "",
              "## Consolidation proposals", ""]
    lines += [f"- `{row['id']}` ({plural(row['members'], 'note')}): {md(row['text'])}" for row in page["proposals"]] or ["- None."]
    lines += ["", "## Contradictions", "", "Notes that say opposite things; decide which holds and rate the other.", ""]
    lines += [f"- {md(row['a_text'])} (`{row['a']}`) **contradicts** {md(row['b_text'])} (`{row['b']}`)"
              for row in page.get("contradictions", [])] or ["- None."]
    lines += ["", "## Knowledge gaps", ""] + ([f"- {md(row)}" for row in page["gaps"]] or ["- None."])
    return lines


def attempts_text(row):
    """How an area's attempts went, or that none were made (never "0/0")."""
    if not as_count(row.get("attempts")):
        return "no attempts yet"
    return f"{row['done']}/{plural(row['attempts'], 'attempt')} verified"


def md_area(page, rows):
    """One area: its notes (ids resolved against `rows`) and files."""
    lines = []
    success = page.get("success")
    tried = attempts_text(page)
    lines += [tried[:1].upper() + tried[1:]
              + (f" (success {success:.2f})" if isinstance(success, (int, float)) else "") + ".", "",
              "[Back to the index](../index.md)", "", "## Knowledge", ""]
    lines += capped_line(len(page["knowledge"]), page.get("total"))
    lines += [note_line(rows[key]) for key in page["knowledge"] if key in (rows or {})] or [
        "- None recorded for this area."]
    lines += ["", "## Files", ""]
    lines += [f"- `{row['path']}`: {plural(row['attempts'], 'attempt')}" for row in page["files"]] or ["- None recorded."]
    return lines


def files_label(topic):
    listed = len(topic["files"].split())
    return f"Files ({listed} of {topic['size']}): " if topic["size"] > listed else "Files: "


def md_topics(page, rows):
    """Each module group dreaming discovered, with its files and the knowledge about them."""
    lines = ["Groups of files that change together or depend on each other, found by dreaming.", ""]
    # Groups come largest first (by file count), so the cap keeps the largest.
    lines += capped_line(len(page["groups"]), page.get("total"), "largest")
    for topic in page["groups"]:
        lines += [f"## {md(topic['label'])} ({plural(topic['size'], 'file')}, cohesion {topic['cohesion']})", "",
                  files_label(topic) + ", ".join(f"`{path}`" for path in topic["files"].split()), ""]
        lines += capped_line(len(topic["knowledge"]), topic.get("total"))
        lines += [note_line(rows[key]) for key in topic["knowledge"] if key in rows] or ["- No knowledge recorded."]
        lines.append("")
    if not page["groups"]:
        lines.append("No topics yet: dreaming groups files once they change together or depend on each other.")
    return lines


MARKDOWN_PAGES = {"index": md_index, "knowledge": md_knowledge, "changes": md_changes,
                  "sessions": md_sessions, "dreams": md_dreams}


def markdown(name, page, rows=None):
    lines = [f"# {md(page.get('title', name))}", ""]
    render_page = MARKDOWN_PAGES.get(name)
    if name == "topics":
        lines += md_topics(page, rows or {})
    else:
        lines += render_page(page) if render_page else md_area(page, rows or {})
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
    # Refuse before writing anything: a symlinked folder would put pages outside .improve.
    for folder in (directory, directory / "areas"):
        if folder.is_symlink():
            raise WikiError(f"the wiki folder {folder.name} must not be a symlink")
    for name, text in render(result).items():
        atomic_text(directory / f"{name}.md", text)
    atomic_text(directory / "wiki.toon", improve_toon.dumps({"built_at": manifest["built_at"], "pages": result}))
    atomic_text(directory / "manifest.json", json.dumps(manifest, indent=1, ensure_ascii=False) + "\n")
    clean_up(directory, result)


def clean_up(directory, result):
    """The wiki folder is the wiki's own: remove every `.md` page this build did not write and
    every `.wiki-*` temp file a crashed write left behind. Files of any other kind are left
    alone. Unlinking never follows a symlink."""
    for folder, prefix in ((directory, ""), (directory / "areas", "areas/")):
        if not folder.is_dir() or folder.is_symlink():
            continue
        for path in folder.iterdir():
            stale_page = path.suffix == ".md" and f"{prefix}{path.stem}" not in result
            if (stale_page or path.name.startswith(".wiki-")) and (path.is_symlink() or path.is_file()):
                path.unlink()


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
        live = (state / "supervisor.json").exists() or (state / "run.json").exists()
        return {"skipped": "a run is still live" if live else "no improve session has finished here"}
    fetch_types(graph, "insight", "area", "file")
    directory = state / "wiki"
    result, manifest = pages(graph, read_manifest(directory), built_at or stamp(), run_id)
    write(directory, result, manifest)
    last = manifest["sessions"][-1]["counts"]
    return {"pages": len(result), "notes": len(manifest["notes"]), **last, "path": str(directory)}
