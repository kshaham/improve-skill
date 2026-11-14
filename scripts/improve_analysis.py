"""Whole-graph analysis: module groups, surprising links, bridges, questions, reports, export.

Standard library only. Ideas from Graphify (`cluster.py`, `analyze.py`, `report.py`,
`export.py`), rebuilt for this graph's work history plus the code layer:

- **Groups of files** that change together or depend on each other: deterministic weighted
  label propagation over co-change weights and code edges. Files the work has never touched
  are grouped too when the code links them (discovery should look exactly there). Edges and
  nodes are visited in a fixed order and ties go to the smallest label, so the same graph
  always gives the same groups. Hub files (top 1% by degree, in graphs of 100+ files) are
  grouped by a vote of their neighbours afterwards so one hub cannot glue everything together,
  and an oversized group (over a quarter of all files with ten or more members, or fifty or
  more members with cohesion under 0.05) is split by propagating again inside it with its own
  hubs set aside (Graphify's `_split_community`). Each group has a cohesion (internal links /
  possible pairs), a label (its best-connected file), and an id that stays the same across
  dreams while most of its members do.
- **Surprising connections**: links that cross top-level directories or groups, or tie a
  rarely-touched file to a hub, each with the reason.
- **Bridges**: files with the highest betweenness (Brandes), where changes ripple between groups.
- **Suggested questions**: contested or contradicted knowledge, knowledge whose code changed,
  failing files with nothing recorded, bridges, loose groups; kinds are interleaved.
- **report** (Markdown) and **export** (JSON, GraphML, Markdown).
"""

from collections import deque
import contextlib
import json
import xml.etree.ElementTree as ElementTree

MAX_PASSES = 20
HUB_PERCENTILE = 0.99
HUB_MIN_NODES = 100
BETWEENNESS_SAMPLE = 200
SAMPLE_ABOVE = 1500
LOW_COHESION = 0.15
STABLE_OVERLAP = 0.5
MAX_GROUPS = 60
SPLIT_SHARE = 0.25
SPLIT_MIN = 10
SPLIT_LOOSE_SIZE = 50
SPLIT_LOOSE_COHESION = 0.05
SPLIT_HUB_PERCENTILE = 0.9


def short(text, limit=110):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit - 3] + "..."


def code_connection(graph):
    store = getattr(graph, "store", None)
    con = getattr(store, "con", None)
    if con is None:
        return None
    from improve_code import available
    try:
        return con if available(con) else None
    except Exception:  # noqa: BLE001 - a closed or foreign connection means no code layer.
        return None


def label_of(graph, node_id):
    """A node's label; a file known only to the code layer is labelled by its path."""
    node = graph["nodes"].get(node_id)
    if node:
        return node["label"]
    return node_id[len("file:"):] if node_id.startswith("file:") else node_id


def file_weights(graph):
    """Undirected weights between file ids: co-change weight plus code links (imports, calls,
    inheritance, uses). Code links count even for files with no knowledge node yet."""
    nodes = graph["nodes"]
    weights = {}

    def add(left, right, amount):
        if left == right:
            return
        key = (left, right) if left < right else (right, left)
        weights[key] = weights.get(key, 0) + amount

    for edge in graph["edges"].values():
        if edge["rel"] == "co_changed" and edge["src"] in nodes and edge["dst"] in nodes:
            add(edge["src"], edge["dst"], int(edge.get("weight", 1)))
    con = code_connection(graph)
    if con is not None:
        from improve_code import file_edges
        for (left, right), count in file_edges(con).items():
            add("file:" + left, "file:" + right, min(3, count))
    return weights


def neighbours(weights):
    adjacent = {}
    for (left, right), weight in sorted(weights.items()):
        adjacent.setdefault(left, {})[right] = weight
        adjacent.setdefault(right, {})[left] = weight
    return adjacent


def hubs(adjacent):
    if len(adjacent) < HUB_MIN_NODES:
        return set()
    degrees = sorted(len(others) for others in adjacent.values())
    cutoff = degrees[min(len(degrees) - 1, int(len(degrees) * HUB_PERCENTILE))]
    return {node for node, others in adjacent.items() if len(others) >= max(cutoff, 10)}


def propagate(adjacent, excluded=frozenset()):
    labels = {node: node for node in adjacent if node not in excluded}
    order = sorted(labels)
    for _ in range(MAX_PASSES):
        moved = False
        for node in order:
            votes = {}
            for other, weight in adjacent[node].items():
                if other in labels:
                    votes[labels[other]] = votes.get(labels[other], 0) + weight
            if not votes:
                continue
            best = min(votes, key=lambda label: (-votes[label], label))
            if votes[best] > votes.get(labels[node], 0) and best != labels[node]:
                labels[node] = best
                moved = True
        if not moved:
            break
    for hub in sorted(excluded):
        votes = {}
        for other, weight in adjacent[hub].items():
            if other in labels:
                votes[labels[other]] = votes.get(labels[other], 0) + weight
        labels[hub] = min(votes, key=lambda label: (-votes[label], label)) if votes else hub
    return labels


def grouped(labels):
    groups = {}
    for node, label in labels.items():
        groups.setdefault(label, []).append(node)
    return [sorted(members) for _, members in sorted(groups.items())]


def cohesion(adjacent, members):
    inside = set(members)
    internal = sum(1 for node in members for other in adjacent[node] if other in inside) // 2
    possible = len(members) * (len(members) - 1) / 2
    return internal / possible if possible else 0.0


def oversized(adjacent, members, total):
    size = len(members)
    return (size >= SPLIT_MIN and size > SPLIT_SHARE * total) or \
        (size >= SPLIT_LOOSE_SIZE and cohesion(adjacent, members) < SPLIT_LOOSE_COHESION)


def split_group(adjacent, members):
    """Propagate again inside one group, with the group's own hubs (top 10% by degree inside
    it) set aside and voting afterwards. A group that is really one module stays whole."""
    inside = set(members)
    sub = {node: {other: weight for other, weight in adjacent[node].items() if other in inside} for node in members}
    degrees = sorted(len(others) for others in sub.values())
    cutoff = degrees[min(len(degrees) - 1, int(len(degrees) * SPLIT_HUB_PERCENTILE))]
    centre = {node for node, others in sub.items() if len(others) >= max(cutoff, 3)}
    if len(centre) == len(members):
        centre = set()
    parts = grouped(propagate(sub, frozenset(centre)))
    return parts if len(parts) > 1 else [members]


def communities(graph):
    """Groups of files, largest first, with cohesion and a hub label."""
    adjacent = neighbours(file_weights(graph))
    labels = propagate(adjacent, hubs(adjacent))
    groups = []
    for members in grouped(labels):
        groups.extend(split_group(adjacent, members) if oversized(adjacent, members, len(labels)) else [members])
    result = []
    for members in groups:
        if len(members) < 2:
            continue
        inside = set(members)
        hub = min(members, key=lambda node: (-sum(1 for other in adjacent[node] if other in inside), node))
        result.append({"label": label_of(graph, hub), "hub": hub, "members": members, "size": len(members),
                       "cohesion": round(cohesion(adjacent, members), 3)})
    result.sort(key=lambda group: (-group["size"], group["hub"]))
    return result[:MAX_GROUPS]


def refresh_communities(graph):
    """Recompute groups at a dream, keeping each group's id while most of its members stay."""
    from improve_graph import short_hash
    previous = (graph.get("communities") or {}).get("groups") or []
    fresh = communities(graph)
    used = set()
    for group in fresh:
        members = set(group["members"])
        best, best_overlap = None, 0.0
        for old in previous:
            if old["id"] in used:
                continue
            old_members = set(old.get("members") or [])
            shared = len(members & old_members)
            # Graphify's rule: a strict majority of both the old and the new membership.
            if shared * 2 > len(members) and shared * 2 > len(old_members):
                overlap = shared / len(members | old_members)
                if overlap > best_overlap:
                    best, best_overlap = old, overlap
        group["id"] = best["id"] if best and best_overlap >= STABLE_OVERLAP / 2 else "group:" + short_hash(
            "|".join(group["members"]))[:8]
        used.add(group["id"])
    graph["communities"] = {"groups": fresh, "tick": int(graph.get("tick", 0))}
    return {"groups": len(fresh), "largest": fresh[0]["size"] if fresh else 0}


def group_of(graph, file_id):
    for group in (graph.get("communities") or {}).get("groups") or []:
        if file_id in group.get("members", []):
            return group
    return None


# ---------------------------------------------------------------- bridges and surprises

def betweenness(adjacent, sample=None):
    """Brandes' algorithm on the unweighted graph (sampled sources above SAMPLE_ABOVE nodes)."""
    nodes = sorted(adjacent)
    sources = nodes
    if sample is None and len(nodes) > SAMPLE_ABOVE:
        sources = sorted(nodes, key=lambda node: (-len(adjacent[node]), node))[:BETWEENNESS_SAMPLE]
    score = dict.fromkeys(nodes, 0.0)
    for source in sources:
        stack, predecessors = [], {node: [] for node in nodes}
        paths = dict.fromkeys(nodes, 0)
        paths[source] = 1
        distance = dict.fromkeys(nodes, -1)
        distance[source] = 0
        queue = deque([source])
        while queue:
            node = queue.popleft()
            stack.append(node)
            for other in adjacent[node]:
                if distance[other] < 0:
                    distance[other] = distance[node] + 1
                    queue.append(other)
                if distance[other] == distance[node] + 1:
                    paths[other] += paths[node]
                    predecessors[other].append(node)
        delta = dict.fromkeys(nodes, 0.0)
        while stack:
            node = stack.pop()
            for before in predecessors[node]:
                delta[before] += paths[before] / paths[node] * (1 + delta[node])
            if node != source:
                score[node] += delta[node]
    return score


def bridges(graph, limit=5):
    adjacent = neighbours(file_weights(graph))
    if len(adjacent) < 3:
        return []
    score = betweenness(adjacent)
    ranked = sorted((node for node in score if score[node] > 0), key=lambda node: (-score[node], node))[:limit]
    return [{"file": label_of(graph, node), "id": node, "betweenness": round(score[node], 2),
             "degree": len(adjacent[node])} for node in ranked]


def top_directory(file_id):
    path = file_id[len("file:"):]
    return path.split("/", 1)[0] if "/" in path else "."


def surprises(graph, limit=8):
    """Links worth a second look (Graphify's surprise score), each with its reasons."""
    nodes = graph["nodes"]
    adjacent = neighbours(file_weights(graph))
    membership = {}
    for group in (graph.get("communities") or {}).get("groups") or []:
        for member in group.get("members", []):
            membership[member] = group["id"]
    found = []
    for edge in graph["edges"].values():
        src, dst = edge["src"], edge["dst"]
        if not (src.startswith("file:") or dst.startswith("file:")) or edge["rel"] in ("touches", "in_area", "in_run"):
            continue
        if edge["rel"] not in ("co_changed", "applies_to", "affects", "about"):
            continue
        from improve_graph import edge_tier
        tier = edge_tier(edge)
        score, why = (2 if tier == "inferred" else 1), [{"inferred": "inferred by dreaming",
                                                          "observed": "observed in the work", "recorded": "recorded"}[tier]]
        files = [end for end in (src, dst) if end.startswith("file:")]
        if len(files) == 2:
            if top_directory(files[0]) != top_directory(files[1]):
                score += 2
                why.append(f"crosses {top_directory(files[0])}/ and {top_directory(files[1])}/")
            if membership.get(files[0]) and membership.get(files[1]) and membership[files[0]] != membership[files[1]]:
                score += 1
                why.append("crosses module groups")
            low, high = sorted((len(adjacent.get(end, {})) for end in files))
            if low <= 2 and high >= 5:
                score += 1
                why.append("ties a rarely-changed file to a hub")
        if score < 3:
            continue
        found.append({"src": src, "rel": edge["rel"], "dst": dst, "score": score, "why": "; ".join(why),
                      "src_label": short(nodes.get(src, {}).get("label"), 60),
                      "dst_label": short(nodes.get(dst, {}).get("label"), 60)})
    found.sort(key=lambda row: (-row["score"], row["src"], row["dst"]))
    return found[:limit]


# ---------------------------------------------------------------- questions and report

def suggested_questions(graph, repo=None, limit=10):
    """What a person (or the agent between tasks) should look at, a few of each kind."""
    from improve_graph import code_changed, hotspots
    nodes = graph["nodes"]
    kinds = {"contested": [], "contradiction": [], "changed": [], "gap": [], "bridge": [], "loose": []}
    cache = {}
    for node in sorted(nodes.values(), key=lambda node: (-float(node.get("score", 0)), node["id"])):
        if node["type"] != "insight" or node.get("archived"):
            continue
        if node.get("verdict") == "contested":
            kinds["contested"].append({"kind": "contested", "id": node["id"], "question": (
                f"Is this still true? Outcomes are mixed ({node.get('cited_done', 0)} verified, "
                f"{node.get('cited_failed', 0)} rejected): {short(node.get('summary'))}")})
        changed = code_changed(node, repo, cache)
        if changed:
            kinds["changed"].append({"kind": "changed", "id": node["id"], "question": (
                f"Re-verify: {', '.join(changed[:2])} changed since this was recorded: {short(node.get('summary'))}")})
    for edge in sorted(graph["edges"].values(), key=lambda edge: (-float(edge.get("confidence", 0)), edge["src"])):
        if edge["rel"] == "contradicts" and edge["src"] in nodes and edge["dst"] in nodes:
            kinds["contradiction"].append({"kind": "contradiction", "id": edge["src"], "other": edge["dst"], "question": (
                f"These say opposite things; which is right? [{edge['src']}] {short(nodes[edge['src']].get('summary'), 70)} "
                f"vs [{edge['dst']}] {short(nodes[edge['dst']].get('summary'), 70)}")})
    for row in graph["dream"].get("gaps") or []:
        kinds["gap"].append({"kind": "gap", "id": row["id"], "question": (
            f"Why do tasks on {row['file']} keep failing ({row['done']}/{row['attempts']} verified)? Nothing is recorded.")})
    if not kinds["gap"]:
        for row in hotspots(graph, 3):
            if row["success"] < 0.5:
                kinds["gap"].append({"kind": "gap", "id": row["id"], "question": (
                    f"What makes {row['file']} hard to change ({row['done']}/{row['attempts']} verified)?")})
    for row in bridges(graph, 3):
        kinds["bridge"].append({"kind": "bridge", "id": row["id"], "question": (
            f"{row['file']} links otherwise separate groups of files; does a change there need wider checks?")})
    for group in (graph.get("communities") or {}).get("groups") or []:
        if group["size"] >= 5 and group["cohesion"] < LOW_COHESION:
            kinds["loose"].append({"kind": "loose", "id": group["id"], "question": (
                f"The group around {group['label']} ({group['size']} files) is loosely connected "
                f"(cohesion {group['cohesion']}); is it really one module?")})
    questions, rounds = [], max((len(rows) for rows in kinds.values()), default=0)
    for index in range(rounds):
        for rows in kinds.values():
            if index < len(rows) and len(questions) < limit:
                questions.append(rows[index])
    return questions


def god_files(graph, limit=8):
    """Files most connected by work and by code: where changes are riskiest."""
    nodes = graph["nodes"]
    code = {}
    con = code_connection(graph)
    if con is not None:
        from improve_code import degrees
        code = degrees(con)
    rows = []
    for node in nodes.values():
        if node["type"] != "file" or node.get("stale"):
            continue
        stats = node.get("stats") or {}
        path = node["label"]
        used_by = int(code.get(path, {}).get("used_by", 0))
        attempts = int(stats.get("attempts", 0))
        if used_by + attempts == 0:
            continue
        rows.append({"file": path, "used_by": used_by, "attempts": attempts, "success": stats.get("success"),
                     "weight": used_by + 2 * attempts})
    rows.sort(key=lambda row: (-row["weight"], row["file"]))
    return rows[:limit]


def tiers(graph):
    counts = {"recorded": 0, "observed": 0, "inferred": 0}
    from improve_graph import edge_tier
    for edge in graph["edges"].values():
        counts[edge_tier(edge)] += 1
    total = sum(counts.values()) or 1
    return {tier: {"links": count, "share": round(count / total, 3)} for tier, count in counts.items()}


def report(graph, repo=None):
    """The graph as a Markdown report for a person or the final report."""
    from improve_graph import code_changed, hotspots, summary
    nodes = graph["nodes"]
    info = summary(graph)
    lines = ["# Knowledge graph report", "",
             f"Revision {info['revision']} - {info['nodes']} nodes, {info['edges']} links, {info['runs']} runs, "
             f"{info['insights']} live notes, dream tick {info['tick']}.", ""]
    share = tiers(graph)
    lines.append("Links: " + ", ".join(f"{tier} {row['share']:.0%}" for tier, row in share.items())
                 + " (recorded by agents and users / observed from ledgers, git, and code / inferred by dreaming).")
    con = code_connection(graph)
    if con is not None:
        from improve_code import coverage_line, find_cycles, stats as code_stats
        code = code_stats(con)
        lines.append(f"Code layer: {code['files']} files, {code['symbols']} symbols, {code['edges']} links, "
                     f"{code['notes']} code notes.")
        coverage = coverage_line(con)
        if coverage:
            lines.append(f"Code coverage: {coverage}.")
    lines.append("")

    def section(title, rows, render):
        if rows:
            lines.extend([f"## {title}", ""] + [f"- {render(row)}" for row in rows] + [""])

    section("Files that resist change", hotspots(graph, 8),
            lambda row: f"`{row['file']}`: {row['done']}/{row['attempts']} verified (success {row['success']})")
    section("Most connected files", god_files(graph),
            lambda row: f"`{row['file']}`: used by {row['used_by']} files, {row['attempts']} attempts")
    proven = [node for node in nodes.values() if node["type"] == "insight" and node.get("verdict") == "proven"]
    section("Proven knowledge", sorted(proven, key=lambda node: node["id"])[:8],
            lambda node: f"[{node['id']}] {short(node.get('summary'), 160)}")
    contested = [node for node in nodes.values() if node["type"] == "insight" and node.get("verdict") == "contested"]
    section("Contested knowledge", sorted(contested, key=lambda node: node["id"])[:8],
            lambda node: f"[{node['id']}] {short(node.get('summary'), 160)}")
    cache = {}
    changed = [(node, code_changed(node, repo, cache)) for node in nodes.values()
               if node["type"] == "insight" and not node.get("archived")]
    section("Knowledge whose code changed since", [row for row in changed if row[1]][:8],
            lambda row: f"[{row[0]['id']}] {short(row[0].get('summary'), 120)} (changed: {', '.join(row[1][:3])})")
    section("Failing files with nothing recorded", graph["dream"].get("gaps") or [],
            lambda row: f"`{row['file']}`: {row['done']}/{row['attempts']} verified")
    groups = (graph.get("communities") or {}).get("groups") or []
    section("Module groups", groups[:10],
            lambda group: f"{group['id']} around `{group['label']}`: {group['size']} files, cohesion {group['cohesion']}")
    section("Surprising connections", surprises(graph),
            lambda row: f"{row['src_label']} --{row['rel']}--> {row['dst_label']}: {row['why']}")
    if con is not None:
        found = find_cycles(con, limit=8)
        title = "Import cycles" + ("" if found["complete"] else f" (at least {found['found']}; search stopped early)")
        section(title, found["cycles"], lambda row: " -> ".join(f"`{path}`" for path in row["files"] + row["files"][:1]))
    section("Suggested questions", suggested_questions(graph, repo), lambda row: row["question"])
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- export

def export_json(graph):
    return json.dumps({"revision": graph.get("revision", 0), "nodes": [graph["nodes"][key] for key in sorted(graph["nodes"])],
                       "edges": [graph["edges"][key] for key in sorted(graph["edges"])],
                       "runs": graph.get("runs", []), "communities": graph.get("communities") or {}},
                      indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def export_graphml(graph):
    """GraphML for Gephi, yEd, or networkx: node type/label/score and link rel/weight/confidence/tier."""
    from improve_graph import edge_tier
    root = ElementTree.Element("graphml", xmlns="http://graphml.graphdrawing.org/xmlns")
    keys = (("type", "node", "string"), ("label", "node", "string"), ("score", "node", "double"),
            ("status", "node", "string"), ("rel", "edge", "string"), ("weight", "edge", "double"),
            ("confidence", "edge", "double"), ("tier", "edge", "string"))
    for name, owner, kind in keys:
        ElementTree.SubElement(root, "key", id=name, attrib={"for": owner, "attr.name": name, "attr.type": kind})
    body = ElementTree.SubElement(root, "graph", id="improve", edgedefault="directed")

    def data(parent, key, value):
        if value is not None:
            ElementTree.SubElement(parent, "data", key=key).text = str(value)

    for key in sorted(graph["nodes"]):
        node = graph["nodes"][key]
        element = ElementTree.SubElement(body, "node", id=key)
        for name in ("type", "label", "score", "status"):
            data(element, name, node.get(name))
    for index, key in enumerate(sorted(graph["edges"])):
        edge = graph["edges"][key]
        element = ElementTree.SubElement(body, "edge", id=f"e{index}", source=edge["src"], target=edge["dst"])
        data(element, "rel", edge["rel"])
        data(element, "weight", edge.get("weight", 1))
        data(element, "confidence", edge.get("confidence"))
        data(element, "tier", edge_tier(edge))
    with contextlib.suppress(AttributeError):  # Python 3.9+
        ElementTree.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ElementTree.tostring(root, encoding="unicode") + "\n"
