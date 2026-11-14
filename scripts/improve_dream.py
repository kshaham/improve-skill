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

from improve_graph import (DREAM_RELATIONS, MERGE_SIMILARITY, NEGATION, PROTECTED_SOURCES, GraphError,
                           add_insight, clean, due, edge_key, fetch_types, hotspots, merge_into, opposed, refine,
                           remove_nodes, resolve, short_hash, stamp, tokens)

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
MAX_EVIDENCE = 1500  # The task bound: every live outcome is kept, so pruning never loses one.
MAX_RETAINED_TASKS = 500
MAX_CITERS = 8
MAX_DREAM_LOG = 30
MAX_DROPPED = 200
MAX_PROPOSALS = 8
CLUSTER_MAX = 6
UNDIRECTED = ("co_cited", "similar_to", "contradicts")
CONTRADICT_MIN = 0.5
VERDICT_HALF_LIFE_RUNS = 8
MAX_REJECTED = 2000
MAX_DIFF_EXAMPLES = 10
# Bumped when inference rules change, so the first dream after an upgrade re-derives links
# from evidence instead of keeping them by hysteresis.
RULES_VERSION = 2


def outcome(status):
    """Evidence about knowledge: a blocked task says nothing about whether the knowledge was right."""
    return "done" if status == "done" else "failed" if status == "rejected" else None


def dream_due(graph, interval):
    """True when the last dream (or, before any, the first ingest) is older than `interval` seconds."""
    return due(graph["dream"], interval, bool(graph["nodes"]))


class Evidence:
    """Task outcomes supporting one candidate link, live and retained from pruned tasks."""

    def __init__(self):
        self.tasks = {}

    def add(self, task_id, result):
        if result:
            self.tasks[task_id] = result


def tally(live, previous, pruned):
    """Counts for a link: live evidence plus what earlier dreams saw on tasks since pruned.

    `retained_tasks` remembers which pruned tasks the retained counts came from, so a task
    that is restored (live again) is taken back out of them instead of being counted twice."""
    before = (previous or {}).get("evidence", {}) or {}
    retained = {"done": 0, "failed": 0, **(before.get("retained") or {})}
    retained_tasks = dict(before.get("retained_tasks") or {})
    for task_id in [task_id for task_id in retained_tasks if task_id in live]:
        result = retained_tasks.pop(task_id)
        retained[result] = max(0, int(retained.get(result, 0)) - 1)
    for task_id, result in (before.get("tasks") or {}).items():
        if task_id not in live and task_id in pruned and task_id not in retained_tasks:
            retained[result] = int(retained.get(result, 0)) + 1
            retained_tasks[task_id] = result
    if len(retained_tasks) > MAX_RETAINED_TASKS:
        retained_tasks = dict(list(retained_tasks.items())[-MAX_RETAINED_TASKS:])
    done = sum(result == "done" for result in live.values()) + int(retained.get("done", 0))
    failed = sum(result == "failed" for result in live.values()) + int(retained.get("failed", 0))
    tasks = dict(sorted(live.items())[:MAX_EVIDENCE])
    evidence = {"tasks": tasks, "retained": retained}
    if retained_tasks:
        evidence["retained_tasks"] = retained_tasks
    return done, failed, evidence


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

    # Opposite advice: the same wording with the polarity reversed ("always X" / "never X").
    # Neither note is wrong by itself; the pair is a question for the agent or the user.
    plain = {node_id: frozenset(word for word in words[node_id] if not NEGATION.fullmatch(word)) for node_id in insights}
    for (left, right), count in shared_counts.items():
        key = edge_key(left, "contradicts", right)
        a, b = plain[left], plain[right]
        overlap = len(a & b) / len(a | b) if a and b else 0
        if overlap < CONTRADICT_MIN or key in rejected:
            continue
        left_text = nodes[left].get("summary") or nodes[left].get("label", "")
        right_text = nodes[right].get("summary") or nodes[right].get("label", "")
        if not opposed(left_text, right_text):
            continue
        accepted[key] = dict(src=left, rel="contradicts", dst=right, rule="opposite-wording", generation=1,
                             confidence=round(min(0.95, overlap), 3),
                             evidence={"words": sorted(a & b)[:8]}, support={"overlap": round(overlap, 3)})

    if graph["dream"].get("rules_version") != RULES_VERSION:
        # New rules: judge every link as new (no hysteresis from links the old rules made).
        report["rules_changed"] = True
        for key in list(accepted):
            spec = accepted[key]
            support = spec.get("support") or {}
            done, failed = support.get("done"), support.get("failed")
            if isinstance(done, int) and isinstance(failed, int) and done < CREATE_RATIO * failed:
                del accepted[key]
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
    # One pass over the edges per reason, not per node: forgetting a thousand nodes on a large
    # graph stays linear.
    by_reason = {}
    for node_id in doomed:
        by_reason.setdefault(reasons[node_id], []).append(node_id)
    for reason, group in sorted(by_reason.items()):
        remove_nodes(graph, group, reason=reason, op="forgotten")
    report.update(archived=dormant, forgotten=len(doomed),
                  forgotten_by_type={kind: sum(1 for node_id in doomed if node_id.startswith(kind + ":"))
                                     for kind in ("insight", "file", "gate")})


def assess(graph, report):
    nodes, edges = graph["nodes"], graph["edges"]
    proven = contested = 0
    current = len(graph["runs"])
    citations = {}
    for edge in edges.values():
        if edge["rel"] == "used_in" and nodes.get(edge["dst"], {}).get("type") == "task":
            citations.setdefault(edge["src"], []).append(nodes[edge["dst"]])
    for node in nodes.values():
        if node["type"] != "insight":
            continue
        done, failed = int(node.get("cited_done", 0)), int(node.get("cited_failed", 0))
        ratio = recency_ratio(citations.get(node["id"], []), node.get("cite_base") or {}, current)
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


def recency_ratio(tasks, folded, current):
    """Share of the evidence that supports the knowledge, each outcome weighted by
    0.5 ** (runs since / VERDICT_HALF_LIFE_RUNS) (Graphify's reflect decay), so three old
    successes do not outweigh two recent failures. Outcomes folded from pruned tasks count at
    the weight of the oldest evidence still live (or two half-lives when none is)."""
    support = against = 0.0
    weights = []
    for task in tasks:
        outcome_value = outcome(task.get("status"))
        if not outcome_value:
            continue
        age = max(0, current - int(task.get("last_seq", current) or 0))
        weight = 0.5 ** (age / VERDICT_HALF_LIFE_RUNS)
        weights.append(weight)
        if outcome_value == "done":
            support += weight
        else:
            against += weight
    floor = min(weights) if weights else 0.25
    support += floor * int(folded.get("done", 0) or 0)
    against += floor * int(folded.get("failed", 0) or 0)
    return support / (support + against) if support + against else 0.0


def propose(graph, report):
    """Groups of overlapping agent knowledge an agent could rewrite as one stronger note."""
    nodes, tick = graph["nodes"], int(graph["tick"])
    eligible = {node_id for node_id, node in nodes.items() if node["type"] == "insight" and not node.get("archived")
                and not node.get("pinned") and node.get("source") not in PROTECTED_SOURCES}
    adjacent = {}
    strength = {}
    for edge in graph["edges"].values():
        if edge["rel"] in UNDIRECTED and edge["rel"] != "contradicts" and edge.get("inferred") \
                and float(edge.get("confidence", 0)) >= 0.6 and edge["src"] in eligible and edge["dst"] in eligible:
            adjacent.setdefault(edge["src"], set()).add(edge["dst"])
            adjacent.setdefault(edge["dst"], set()).add(edge["src"])
            for end in (edge["src"], edge["dst"]):
                strength[end] = strength.get(end, 0) + float(edge["confidence"])
    # Cliques, not connected components: in a chain A~B~C~D the ends need not be related, so
    # every member of a proposal must be linked to every other (grown from the strongest edge).
    pairs = sorted(((float(edge["confidence"]), edge["src"], edge["dst"]) for edge in graph["edges"].values()
                    if edge["rel"] in UNDIRECTED and edge["rel"] != "contradicts" and edge.get("inferred")
                    and float(edge.get("confidence", 0)) >= 0.6 and edge["src"] in eligible and edge["dst"] in eligible),
                   key=lambda item: (-item[0], item[1], item[2]))
    seen, clusters = set(), []
    for _, left, right in pairs:
        if left in seen or right in seen:
            continue
        members = {left, right}
        candidates = sorted((adjacent.get(left, set()) & adjacent.get(right, set())) - seen,
                            key=lambda node_id: (-strength.get(node_id, 0), node_id))
        for candidate in candidates:
            if len(members) >= CLUSTER_MAX:
                break
            if members <= adjacent.get(candidate, set()):
                members.add(candidate)
        seen |= members
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
    # A rejection names its ends by id; after a merge or rename it must follow them, or the
    # link it rejected would be inferred again under the new id.
    rejected = graph["dream"]["rejected"]
    for key in list(rejected):
        src, rel, dst = key.split("|", 2) if key.count("|") >= 2 else (None, None, None)
        if src is None:
            del rejected[key]
            repaired += 1
            continue
        src, rel, dst = normalize_link(resolve(graph, src), rel, resolve(graph, dst))
        fresh = edge_key(src, rel, dst)
        if fresh != key:
            rejected.setdefault(fresh, rejected[key])
            del rejected[key]
            repaired += 1
    if len(rejected) > MAX_REJECTED:
        for key, _ in sorted(rejected.items(), key=lambda item: (str(item[1].get("at", "")), item[0]))[:len(rejected) - MAX_REJECTED]:
            del rejected[key]
    store = getattr(graph, "store", None)
    if store is not None and not getattr(graph, "partial", False):
        repaired += store.integrity()
    report["repaired"] = repaired


def dream(graph, repo=None):
    """One dream. Deterministic given the saved facts; safe to run at any time."""
    started = time.perf_counter()
    before_nodes = set(graph["nodes"])
    before_links = {key for key, edge in graph["edges"].items() if edge.get("inferred")}
    graph["tick"] = int(graph.get("tick", 0)) + 1
    report = {"at": stamp(), "tick": graph["tick"]}
    report["refine"] = {key: value for key, value in refine(graph, repo).items() if key not in ("at",)}
    repair(graph, report)
    infer(graph, report)
    forget(graph, report)
    assess(graph, report)
    propose(graph, report)
    graph["dream"]["rules_version"] = RULES_VERSION
    try:
        from improve_analysis import refresh_communities
        report["communities"] = refresh_communities(graph)
    except Exception as exc:  # noqa: BLE001 - grouping is a view; a failure must not lose the dream.
        report["communities_error"] = f"{type(exc).__name__}: {exc}"
    after_links = {key for key, edge in graph["edges"].items() if edge.get("inferred")}
    report["diff"] = diff_summary(before_nodes, set(graph["nodes"]), before_links, after_links)
    report["nodes"], report["edges"] = len(graph["nodes"]), len(graph["edges"])
    report["inferred_total"] = sum(1 for edge in graph["edges"].values() if edge.get("inferred"))
    report["seconds"] = round(time.perf_counter() - started, 3)
    history = graph["dream"]["history"]
    history.append(report)
    del history[:-MAX_DREAM_LOG]
    graph["dream"].update(last_at=report["at"], last_tick=graph["tick"])
    return report


def diff_summary(nodes_before, nodes_after, links_before, links_after):
    """What one dream changed (Graphify's graph_diff): counts and a few examples of each."""
    def sample(values):
        return sorted(values)[:MAX_DIFF_EXAMPLES]
    added, removed = nodes_after - nodes_before, nodes_before - nodes_after
    new_links, lost_links = links_after - links_before, links_before - links_after
    summary = (f"{len(added)} node(s) added, {len(removed)} removed; {len(new_links)} inferred link(s) added, "
               f"{len(lost_links)} removed")
    return {"summary": summary, "nodes_added": len(added), "nodes_removed": len(removed),
            "links_added": len(new_links), "links_removed": len(lost_links),
            "examples": {"nodes_added": sample(added), "nodes_removed": sample(removed),
                         "links_added": sample(new_links), "links_removed": sample(lost_links)}}


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
