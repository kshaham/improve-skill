"""Dreaming: autonomous maintenance of the knowledge graph between bursts of work.

Each dream advances the graph's tick (its clock, which moves within one long run as well as
across runs), refines, and then, deterministically:

1. Infers links the evidence supports but nobody wrote down. Every inferred link carries its
   rule, a confidence, and the evidence that produced it:
     applies_to  insight -> file     successful tasks that cited the insight touched the file, and the
                                     insight accounts for a real share of that file's cited work
     applies_to  insight -> file     (second generation) via a strong co-change partner of a
                                     file the insight applies to
     applies_to  insight -> area     most files it applies to are in that area
     co_cited    insight <-> insight cited together by successful tasks, on a real share of their citations
     similar_to  insight <-> insight overlapping wording, short of a merge, and a shared file
     affects     blocker -> file     the blocker stopped several tasks on that file
   Blocked tasks are neutral evidence about knowledge (a blocker is environmental). Creating a
   link needs twice as many successes as failures; keeping one needs a quarter more (hysteresis,
   so links do not flap). Inferred links are recomputed every dream: unsupported or contradicted ones are dropped,
   rejected ones are never inferred again, and evidence from pruned tasks is retained as
   counts, so age alone never erases a relationship. Second-generation links are built only
   from first-generation or asserted links, never from each other.
2. Archives agent knowledge that went dormant and forgets (removes, with full history in the
   store) knowledge archived long enough, stale files nothing refers to, and unused gates.
3. Flags proven and contested knowledge, lists knowledge gaps, and proposes clusters of
   overlapping knowledge that an agent may consolidate; plain code applies a consolidation.
4. Repairs integrity problems.
"""

import time

from improve_graph import (DREAM_RELATIONS, FINISHED, MERGE_SIMILARITY, GraphError, add_insight, clean, edge_key,
                           fetch_types, hotspots, merge_into, refine, remove_nodes, resolve, short_hash, stamp,
                           tokens, epoch, StateError)

DORMANT_TICKS = 8
FORGET_TICKS = 8
STALE_FILE_TICKS = 4
GATE_TICKS = 8
MIN_SUPPORT = 2
MIN_CONFIDENCE = 0.5
KEEP_RATIO = 1.25
CREATE_RATIO = 2.0
SIMILAR_MIN = 0.4
MIN_FILE_SHARE = 0.15
MIN_OVERLAP = 0.25
CO_CHANGE_MIN_WEIGHT = 3
CO_CHANGE_MIN_STRENGTH = 0.5
GEN2_SOURCE_MIN = 0.6
GEN2_DISCOUNT = 0.8
GEN2_MIN = 0.45
AREA_MIN_FILES = 3
AREA_SHARE = 0.6
MAX_EVIDENCE = 200
MAX_CITERS = 8
MAX_DREAM_LOG = 30
MAX_DROPPED = 200
MAX_PROPOSALS = 8
CLUSTER_MAX = 6
PROTECTED_SOURCES = ("user", "learning")
UNDIRECTED = ("co_cited", "similar_to")


def outcome(status):
    """Evidence about knowledge: a blocked task says nothing about whether the knowledge was right."""
    return "done" if status == "done" else "failed" if status == "rejected" else None


def dream_due(graph, interval):
    if interval is None or interval <= 0 or not graph["nodes"]:
        return False
    try:
        return epoch(graph["dream"].get("last_at")) + interval <= epoch(stamp())
    except StateError:
        return True


class Evidence:
    """Task outcomes supporting one candidate link, live and retained from pruned tasks."""

    def __init__(self):
        self.tasks = {}

    def add(self, task_id, result):
        if result:
            self.tasks[task_id] = result


def tally(live, previous, pruned):
    """Counts for a link: live evidence plus what earlier dreams saw on tasks since pruned."""
    retained = dict((previous or {}).get("evidence", {}).get("retained") or {"done": 0, "failed": 0})
    for task_id, result in ((previous or {}).get("evidence", {}).get("tasks") or {}).items():
        if task_id not in live and task_id in pruned:
            retained[result] = retained.get(result, 0) + 1
    done = sum(result == "done" for result in live.values()) + retained.get("done", 0)
    failed = sum(result == "failed" for result in live.values()) + retained.get("failed", 0)
    tasks = dict(sorted(live.items())[:MAX_EVIDENCE])
    return done, failed, {"tasks": tasks, "retained": retained}


def task_rule(rel, rule, candidates, previous, pruned, rejected, asserted, association=None,
              min_support=MIN_SUPPORT, failures_count=True):
    """Accept task-evidenced candidates; returns (accepted specs, contradicted keys).
    `association(key)` is how strongly the two ends go together in the live evidence (0-1)."""
    accepted, contradicted = {}, set()
    keys = set(candidates) | {key for key, edge in previous.items() if edge.get("rule") == rule}
    for key in keys:
        src, _, dst = key.split("|", 2)
        done, failed, evidence = tally(candidates.get(key, {}), previous.get(key), pruned)
        if not failures_count:
            done, failed = done + failed, 0
        if done + failed < min_support or key in rejected or (src, dst) in asserted:
            continue
        ratio = KEEP_RATIO if key in previous else CREATE_RATIO
        if done < min_support or done < ratio * failed:
            contradicted.add(key)
            continue
        if association is not None and key not in previous and key in candidates and association(key) is not None \
                and association(key) < (MIN_FILE_SHARE if rel == "applies_to" else MIN_OVERLAP):
            continue
        confidence = round(done / (done + failed + 1), 3)
        if confidence >= MIN_CONFIDENCE:
            accepted[key] = dict(src=src, rel=rel, dst=dst, rule=rule, generation=1, confidence=confidence,
                                 support={"done": done, "failed": failed}, evidence=evidence)
    return accepted, contradicted


def infer(graph, report):
    nodes, edges = graph["nodes"], graph["edges"]
    rejected = graph["dream"]["rejected"]
    previous = {key: edge for key, edge in edges.items() if edge.get("inferred")}
    pruned = {f"task:{row.get('run_id')}:{task_id}" for row in graph["runs"] for task_id in row.get("pruned", [])}
    live_insight = lambda node_id: (nodes.get(node_id, {}).get("type") == "insight"
                                    and not nodes[node_id].get("archived"))
    touches, citers, blockers = {}, {}, {}
    asserted = set()
    for edge in edges.values():
        if edge.get("inferred"):
            continue
        asserted.add((edge["src"], edge["dst"]))
        asserted.add((edge["dst"], edge["src"]))
        if edge["rel"] == "touches":
            touches.setdefault(edge["src"], []).append(edge["dst"])
        elif edge["rel"] == "used_in" and live_insight(edge["src"]) and nodes.get(edge["dst"], {}).get("type") == "task":
            citers.setdefault(edge["dst"], []).append(edge["src"])
        elif edge["rel"] == "blocked_by":
            blockers.setdefault(edge["src"], []).append(edge["dst"])

    # Task evidence for applies_to (insight -> file), co_cited, and affects.
    applies, cocited, affects = {}, {}, {}
    cited_on_file, citations = {}, {}
    for task_id, insights in citers.items():
        for file_id in touches.get(task_id, []):
            cited_on_file[file_id] = cited_on_file.get(file_id, 0) + 1
        for insight in set(insights):
            citations[insight] = citations.get(insight, 0) + 1
    for task_id, insights in citers.items():
        result = outcome(nodes[task_id].get("status"))
        if not result:
            continue
        for insight in insights:
            for file_id in touches.get(task_id, []):
                applies.setdefault(edge_key(insight, "applies_to", file_id), {})[task_id] = result
        group = sorted(set(insights))[:MAX_CITERS]
        for index, left in enumerate(group):
            for right in group[index + 1:]:
                cocited.setdefault(edge_key(left, "co_cited", right), {})[task_id] = result
    for task_id, found in blockers.items():
        for blocker in found:
            for file_id in touches.get(task_id, []):
                affects.setdefault(edge_key(blocker, "affects", file_id), {})[task_id] = "done"

    def file_share(key):
        # How much of the file's cited work this insight accounts for: noise citations are a sliver.
        src, _, dst = key.split("|", 2)
        return len(applies.get(key, {})) / cited_on_file[dst] if cited_on_file.get(dst) else None

    def overlap(key):
        # How often two insights are cited together relative to the rarer one's citations.
        src, _, dst = key.split("|", 2)
        least = min(citations.get(src, 0), citations.get(dst, 0))
        return len(cocited.get(key, {})) / least if least else None

    accepted, contradicted = {}, set()
    for rel, rule, candidates, failures, association in (
            ("applies_to", "cited-on-file", applies, True, file_share),
            ("co_cited", "cited-together", cocited, True, overlap),
            ("affects", "blocked-on-file", affects, False, None)):
        found, against = task_rule(rel, rule, candidates, previous, pruned, rejected, asserted, association,
                                   failures_count=failures)
        accepted.update(found)
        contradicted |= against
    # Endpoints that left the graph or were archived cannot carry inferred links.
    accepted = {key: spec for key, spec in accepted.items()
                if spec["src"] in nodes and spec["dst"] in nodes
                and (nodes[spec["src"]]["type"] != "insight" or live_insight(spec["src"]))
                and (nodes[spec["dst"]]["type"] != "insight" or live_insight(spec["dst"]))}

    # Second generation: through strong co-change partners of files the insight applies to.
    sources = {}
    for edge in edges.values():
        if not edge.get("inferred") and edge["rel"] == "about" and live_insight(edge["src"]) \
                and nodes.get(edge["dst"], {}).get("type") == "file":
            sources.setdefault(edge["src"], {})[edge["dst"]] = (1.0, edge_key(edge["src"], "about", edge["dst"]))
    for key, spec in accepted.items():
        if spec["rel"] == "applies_to" and spec["confidence"] >= GEN2_SOURCE_MIN:
            sources.setdefault(spec["src"], {})[spec["dst"]] = (spec["confidence"], key)
    partners = {}
    for key, edge in edges.items():
        if edge["rel"] == "co_changed" and int(edge.get("weight", 1)) >= CO_CHANGE_MIN_WEIGHT:
            left, right = nodes.get(edge["src"], {}), nodes.get(edge["dst"], {})
            least = min(left.get("stats", {}).get("done", 0), right.get("stats", {}).get("done", 0)) or 1
            strength = min(1.0, int(edge["weight"]) / least)
            if strength >= CO_CHANGE_MIN_STRENGTH:
                partners.setdefault(edge["src"], []).append((edge["dst"], strength, key))
                partners.setdefault(edge["dst"], []).append((edge["src"], strength, key))
    for insight, files in sources.items():
        for file_id, (confidence, via) in files.items():
            for other, strength, co_key in partners.get(file_id, []):
                key = edge_key(insight, "applies_to", other)
                value = round(confidence * strength * GEN2_DISCOUNT, 3)
                live = applies.get(key, {})
                against = sum(result == "failed" for result in live.values())
                if (other in files or key in rejected or (insight, other) in asserted or value < GEN2_MIN
                        or against > len(live) - against):
                    continue
                if key not in accepted or accepted[key]["generation"] == 2 and accepted[key]["confidence"] < value:
                    accepted[key] = dict(src=insight, rel="applies_to", dst=other, rule="via-co-change",
                                         generation=2, confidence=value, evidence={"via": [via, co_key]},
                                         support={"strength": round(strength, 3)})

    # Area level: most of the files an insight applies to share an area.
    for insight, files in sources.items():
        by_area = {}
        for file_id, (confidence, via) in files.items():
            area = nodes.get(file_id, {}).get("area")
            if area and "area:" + area in nodes:
                by_area.setdefault(area, []).append((confidence, via))
        for area, rows in by_area.items():
            share = len(rows) / len(files)
            key = edge_key(insight, "applies_to", "area:" + area)
            if len(rows) < AREA_MIN_FILES or share < AREA_SHARE or key in rejected or (insight, "area:" + area) in asserted:
                continue
            value = round(share * sum(row[0] for row in rows) / len(rows), 3)
            if value >= MIN_CONFIDENCE:
                accepted[key] = dict(src=insight, rel="applies_to", dst="area:" + area, rule="files-in-area",
                                     generation=2, confidence=value, evidence={"via": sorted(row[1] for row in rows)[:12]},
                                     support={"files": len(rows), "share": round(share, 3)})

    # Wording: related knowledge that is not a duplicate, about a shared subject.
    subjects = {}
    for key, spec in accepted.items():
        if spec["rel"] == "applies_to":
            subjects.setdefault(spec["src"], set()).add(spec["dst"])
    for edge in edges.values():
        if edge["rel"] == "about" and not edge.get("inferred"):
            subjects.setdefault(edge["src"], set()).add(edge["dst"])
    insights = sorted(node_id for node_id in nodes if live_insight(node_id))
    words = {node_id: tokens(nodes[node_id].get("summary") or nodes[node_id].get("label", "")) for node_id in insights}
    index = {}
    for node_id in insights:
        for word in words[node_id]:
            index.setdefault(word, []).append(node_id)
    shared_counts = {}
    for word, members in index.items():
        if len(members) > 60:  # A word on that many notes says nothing about any pair of them.
            continue
        for position, left in enumerate(members):
            for right in members[position + 1:]:
                shared_counts[(left, right)] = shared_counts.get((left, right), 0) + 1
    for (left, right), count in shared_counts.items():
        if count < 2:
            continue
        union = len(words[left] | words[right])
        jaccard = count / union if union else 0
        # A shared file, not a shared area: an area spans unrelated work.
        common_subject = any(target.startswith("file:") for target in subjects.get(left, set()) & subjects.get(right, set()))
        key = edge_key(left, "similar_to", right)
        # Wording alone cannot tell shared phrasing from shared meaning; a shared subject can.
        if jaccard < SIMILAR_MIN or jaccard >= MERGE_SIMILARITY or key in rejected or not common_subject:
            continue
        value = round(min(0.95, 0.6 + (jaccard - SIMILAR_MIN)), 3)
        accepted[key] = dict(src=left, rel="similar_to", dst=right, rule="shared-wording", generation=1,
                             confidence=value, evidence={"words": sorted(words[left] & words[right])[:8],
                                                         "shared_subject": common_subject},
                             support={"jaccard": round(jaccard, 3)})

    apply_inferred(graph, accepted, previous, contradicted, report)


def apply_inferred(graph, accepted, previous, contradicted, report):
    edges, tick, rejected = graph["edges"], int(graph["tick"]), graph["dream"]["rejected"]
    counts = {"new": 0, "kept": 0, "strengthened": 0, "weakened": 0, "dropped": 0, "by_rule": {}}
    for key, spec in accepted.items():
        if key in edges and not edges[key].get("inferred"):
            continue
        before = previous.get(key)
        edge = {"created_at": stamp(), "first_tick": tick, **(before or {}), **spec,
                "inferred": True, "weight": 1, "confirmed_tick": tick}
        if before is None:
            counts["new"] += 1
        else:
            delta = spec["confidence"] - float(before.get("confidence", 0))
            counts["strengthened" if delta > 0.001 else "weakened" if delta < -0.001 else "kept"] += 1
        edges[key] = edge
        counts["by_rule"][spec["rule"]] = counts["by_rule"].get(spec["rule"], 0) + 1
    dropped = graph["dream"]["dropped"]
    for key, edge in previous.items():
        if key in accepted:
            continue
        edges.pop(key, None)
        reason = ("rejected" if key in rejected else "contradicted by later outcomes" if key in contradicted
                  else "endpoint archived or removed" if edge["src"] not in graph["nodes"] or edge["dst"] not in graph["nodes"]
                  or graph["nodes"][edge["src"]].get("archived") or graph["nodes"][edge["dst"]].get("archived")
                  else "evidence no longer supports it")
        dropped.append({"key": key, "rel": edge["rel"], "src": edge["src"], "dst": edge["dst"],
                        "confidence": edge.get("confidence"), "reason": reason, "tick": tick})
        counts["dropped"] += 1
    del dropped[:-MAX_DROPPED]
    report["inferred"] = counts


def forget(graph, report):
    nodes, tick = graph["nodes"], int(graph["tick"])
    linked = {}
    for edge in graph["edges"].values():
        if not edge.get("inferred"):
            linked.setdefault(edge["src"], set()).add(edge["rel"])
            linked.setdefault(edge["dst"], set()).add(edge["rel"])
    dormant, doomed, reasons = 0, [], {}
    for node_id, node in sorted(nodes.items()):
        kind = node["type"]
        protected = node.get("pinned") or int(node.get("protected_until_tick", -1)) > tick
        if "last_tick" not in node:
            node["last_tick"] = tick
        idle = tick - int(node["last_tick"])
        if kind == "insight":
            if protected or node.get("source") in PROTECTED_SOURCES:
                continue
            if node.get("archived"):
                node.setdefault("archived_tick", tick)
                waited = tick - int(node["archived_tick"])
                if waited >= FORGET_TICKS:
                    doomed.append(node_id)
                    reasons[node_id] = f"archived for {waited} dreams ({node['archived']})"
                continue
            validated = (int(node.get("cited_done", 0)) or int(node.get("useful", 0))
                         or int(node.get("reinforced", 1)) > 1)
            if idle >= DORMANT_TICKS and not validated:
                node.update(archived=f"dormant: unused for {idle} dreams", archived_tick=tick, dormant=True)
                dormant += 1
        elif kind == "file":
            if not node.get("stale"):
                node.pop("stale_since_tick", None)
                continue
            node.setdefault("stale_since_tick", tick)
            if not protected and tick - int(node["stale_since_tick"]) >= STALE_FILE_TICKS and not linked.get(node_id):
                doomed.append(node_id)
                reasons[node_id] = "file was deleted from the repository and nothing refers to it"
        elif kind == "gate" and not protected and not int(node.get("uses", 0)) and idle >= GATE_TICKS:
            doomed.append(node_id)
            reasons[node_id] = f"no task used this gate for {idle} dreams"
    for node_id in doomed:
        remove_nodes(graph, [node_id], reason=reasons[node_id], op="forgotten")
    report.update(archived=dormant, forgotten=len(doomed),
                  forgotten_by_type={kind: sum(1 for node_id in doomed if node_id.startswith(kind + ":"))
                                     for kind in ("insight", "file", "gate")})


def assess(graph, report):
    nodes, edges = graph["nodes"], graph["edges"]
    proven = contested = 0
    for node in nodes.values():
        if node["type"] != "insight":
            continue
        done, failed = int(node.get("cited_done", 0)), int(node.get("cited_failed", 0))
        ratio = done / (done + failed) if done + failed else 0
        verdict = ("proven" if done >= 3 and ratio >= 0.8 else
                   "contested" if done + failed >= 4 and 0.35 <= ratio <= 0.65 else None)
        if verdict:
            node["verdict"] = verdict
        else:
            node.pop("verdict", None)
        proven += verdict == "proven"
        contested += verdict == "contested"
    known = {edge["dst"] for edge in edges.values() if edge["rel"] in ("about", "applies_to")
             and nodes.get(edge["src"], {}).get("type") == "insight" and not nodes[edge["src"]].get("archived")}
    graph["dream"]["gaps"] = [{"file": row["file"], "id": row["id"], "attempts": row["attempts"], "done": row["done"]}
                              for row in hotspots(graph, 50) if row["id"] not in known and row["attempts"] >= 3
                              and row["success"] < 0.5][:5]
    report.update(proven=proven, contested=contested, gaps=len(graph["dream"]["gaps"]))


def propose(graph, report):
    """Groups of overlapping agent knowledge an agent could rewrite as one stronger note."""
    nodes, tick = graph["nodes"], int(graph["tick"])
    eligible = {node_id for node_id, node in nodes.items() if node["type"] == "insight" and not node.get("archived")
                and not node.get("pinned") and node.get("source") not in PROTECTED_SOURCES}
    adjacent = {}
    strength = {}
    for edge in graph["edges"].values():
        if edge["rel"] in UNDIRECTED and edge.get("inferred") and float(edge.get("confidence", 0)) >= 0.6 \
                and edge["src"] in eligible and edge["dst"] in eligible:
            adjacent.setdefault(edge["src"], set()).add(edge["dst"])
            adjacent.setdefault(edge["dst"], set()).add(edge["src"])
            for end in (edge["src"], edge["dst"]):
                strength[end] = strength.get(end, 0) + float(edge["confidence"])
    seen, clusters = set(), []
    for start in sorted(adjacent):
        if start in seen:
            continue
        component, frontier = set(), [start]
        while frontier:
            current = frontier.pop()
            if current in component:
                continue
            component.add(current)
            frontier.extend(adjacent.get(current, ()))
        seen |= component
        members = sorted(component, key=lambda node_id: (-strength.get(node_id, 0), node_id))[:CLUSTER_MAX]
        if len(members) >= 2:
            clusters.append(sorted(members))
    previous = {row["id"]: row for row in graph["dream"]["proposals"]}
    proposals = []
    for members in clusters:
        proposal_id = "dream:" + short_hash("|".join(members))
        weight = sum(strength.get(member, 0) for member in members) / len(members)
        proposals.append({"id": proposal_id, "members": members, "strength": round(weight, 3),
                          "kinds": sorted({nodes[member].get("kind", "fact") for member in members}),
                          "created_tick": previous.get(proposal_id, {}).get("created_tick", tick)})
    proposals.sort(key=lambda row: (-row["strength"], row["id"]))
    graph["dream"]["proposals"] = proposals[:MAX_PROPOSALS]
    report["proposals"] = len(graph["dream"]["proposals"])


def repair(graph, report):
    repaired = 0
    for alias in list(graph["aliases"]):
        target = resolve(graph, alias)
        if target == alias or target not in graph["nodes"]:
            del graph["aliases"][alias]
            repaired += 1
        elif graph["aliases"][alias] != target:
            graph["aliases"][alias] = target  # Collapse chains so lookups stay one step.
            repaired += 1
    store = getattr(graph, "store", None)
    if store is not None and not getattr(graph, "partial", False):
        repaired += store.integrity()
    report["repaired"] = repaired


def dream(graph, repo=None):
    """One dream. Deterministic given the saved facts; safe to run at any time."""
    started = time.perf_counter()
    graph["tick"] = int(graph.get("tick", 0)) + 1
    report = {"at": stamp(), "tick": graph["tick"]}
    report["refine"] = {key: value for key, value in refine(graph, repo).items() if key not in ("at",)}
    repair(graph, report)
    infer(graph, report)
    forget(graph, report)
    assess(graph, report)
    propose(graph, report)
    report["nodes"], report["edges"] = len(graph["nodes"]), len(graph["edges"])
    report["inferred_total"] = sum(1 for edge in graph["edges"].values() if edge.get("inferred"))
    report["seconds"] = round(time.perf_counter() - started, 3)
    history = graph["dream"]["history"]
    history.append(report)
    del history[:-MAX_DREAM_LOG]
    graph["dream"].update(last_at=report["at"], last_tick=graph["tick"])
    return report


# ---------------------------------------------------------------- agent and user actions

def consolidate(graph, proposal_id, text, kind="technique", consume=(), source="agent"):
    """Apply an agent's consolidation of a dream proposal. The agent proposes; this code
    decides: it may consume only members of that proposal, never pinned or user knowledge,
    and every consumed note stays in history with a pointer to the new one."""
    proposal = next((row for row in graph["dream"]["proposals"] if row["id"] == proposal_id), None)
    if proposal is None:
        raise GraphError("unknown or expired dream proposal; run `proposals` for the current ones")
    fetch_types(graph, "insight")
    members = set(proposal["members"])
    chosen = [resolve(graph, str(node_id)) for node_id in (consume or proposal["members"])]
    chosen = [node_id for node_id in dict.fromkeys(chosen) if node_id in members and node_id in graph["nodes"]
              and not graph["nodes"][node_id].get("pinned") and graph["nodes"][node_id].get("source") not in PROTECTED_SOURCES]
    if len(chosen) < 1:
        raise GraphError("consume must name at least one current member of the proposal")
    summary = add_insight(graph, text, kind, source)
    summary["consolidated_from"] = sorted(set(summary.get("consolidated_from", [])) | set(chosen))[-20:]
    for node_id in chosen:
        if node_id != summary["id"] and node_id in graph["nodes"]:
            merge_into(graph, summary["id"], node_id, reason="consolidated into", op="consolidated")
    graph["dream"]["proposals"] = [row for row in graph["dream"]["proposals"] if row["id"] != proposal_id]
    return summary


def normalize_link(src, rel, dst):
    return (dst, rel, src) if rel in UNDIRECTED and dst < src else (src, rel, dst)


def reject_link(graph, src, rel, dst, by="user", reason=None):
    """Say an inferred link is wrong: it is removed now and never inferred again."""
    if rel not in DREAM_RELATIONS:
        raise GraphError(f"only inferred links can be rejected; relation must be one of {', '.join(DREAM_RELATIONS)}")
    src, rel, dst = normalize_link(resolve(graph, src), rel, resolve(graph, dst))
    missing = [node_id for node_id in (src, dst) if node_id not in graph["nodes"]]
    if missing:
        raise GraphError(f"no graph node {missing[0]!r}; check the ids with `node` or `search`")
    key = edge_key(src, rel, dst)
    edge = graph["edges"].get(key)
    if edge is not None and not edge.get("inferred"):
        raise GraphError("that link was recorded, not inferred; it cannot be rejected")
    graph["dream"]["rejected"][key] = {"by": by, "at": stamp(), "reason": clean(reason or "marked unrelated", 200)}
    if edge is not None:
        graph["edges"].pop(key)
        graph["dream"]["dropped"].append({"key": key, "rel": rel, "src": src, "dst": dst,
                                          "confidence": edge.get("confidence"), "reason": "rejected",
                                          "tick": int(graph.get("tick", 0))})
        del graph["dream"]["dropped"][:-MAX_DROPPED]
    return {"key": key, "removed": edge is not None}


def proposal_view(graph, proposal):
    nodes = graph["nodes"]
    return {**proposal, "notes": [{"id": member, "kind": nodes[member].get("kind"),
                                   "text": nodes[member].get("summary", nodes[member]["label"])}
                                  for member in proposal["members"] if member in nodes]}
