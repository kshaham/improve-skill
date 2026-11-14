"""Dreaming: every inference rule, forgetting on the dream clock, exact restore and recall,
verdicts, consolidation, and a 72-hour run graded against a known ground truth.

IMPROVE_GRAPH_DREAM_SOAK_CYCLES (default 432: 72 hours of 10-minute cycles) scales the soak.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_support import (ROOT, World, budget, check_invariants, environment, load, make_repo,  # noqa: E402
                           restore_environment, slow, untrack, write_ledger, write_run)
import improve_dream as dreaming  # noqa: E402
import improve_graph as graph  # noqa: E402
import improve_toon as toon  # noqa: E402
import improve_learning as learning  # noqa: E402

SOAK_CYCLES = int(os.environ.get("IMPROVE_GRAPH_DREAM_SOAK_CYCLES", "432"))
DREAM_EVERY = 18  # Three hours of ten-minute cycles.
RULES = {"cited-on-file", "cited-together", "blocked-on-file", "shared-wording", "via-co-change", "files-in-area"}


def check_dream_invariants(test, value):
    """What every inferred link and every dream product must satisfy."""
    nodes, edges = value["nodes"], value["edges"]
    problems = []
    asserted = {(edge["src"], edge["rel"], edge["dst"]) for edge in edges.values() if not edge.get("inferred")}
    for key, edge in edges.items():
        if not edge.get("inferred"):
            continue
        confidence = edge.get("confidence")
        if edge["rel"] not in graph.DREAM_RELATIONS or edge.get("rule") not in RULES:
            problems.append(f"{key}: unknown rule {edge.get('rule')}")
        if not isinstance(confidence, (int, float)) or not 0.45 <= confidence <= 1:
            problems.append(f"{key}: confidence {confidence}")
        if edge.get("generation") not in (1, 2) or not edge.get("evidence"):
            problems.append(f"{key}: generation or evidence missing")
        if key in value["dream"]["rejected"]:
            problems.append(f"{key}: rejected but present")
        if (edge["src"], edge["rel"], edge["dst"]) in asserted:
            problems.append(f"{key}: duplicates a recorded link")
        for end in (edge["src"], edge["dst"]):
            if nodes[end]["type"] == "insight" and nodes[end].get("archived"):
                problems.append(f"{key}: archived endpoint {end}")
        for via in (edge.get("evidence") or {}).get("via", []):
            source = edges.get(via)
            if source is not None and source.get("generation") == 2:
                problems.append(f"{key}: built on a second-generation link")
    for proposal in value["dream"]["proposals"]:
        for member in proposal["members"]:
            node = nodes.get(member)
            if node is None or node.get("archived") or node.get("pinned") or node.get("source") in ("user", "learning"):
                problems.append(f"proposal {proposal['id']} has ineligible member {member}")
    for gap in value["dream"]["gaps"]:
        if gap["id"] not in nodes:
            problems.append(f"gap {gap['id']} is not a node")
    if len(value["dream"]["history"]) > dreaming.MAX_DREAM_LOG or len(value["dream"]["dropped"]) > dreaming.MAX_DROPPED:
        problems.append("dream logs are unbounded")
    test.assertEqual(problems, [])


class DreamCase(unittest.TestCase):
    files = ["src/a.py", "src/b.py", "src/c.py", "src/d.py", "ui/x.ts", "ui/y.ts", "ui/z.ts"]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve dream ")
        self.previous = environment(Path(self.temp.name) / "home")
        self.repo = make_repo(Path(self.temp.name) / "repo", self.files).resolve()
        self.state = self.repo / ".improve"
        write_run(self.state, "live", {"py.fast": 4})
        self.rows = []

    def tearDown(self):
        restore_environment(self.previous)
        self.temp.cleanup()

    def note(self, text, kind="pitfall", about=(), source="agent"):
        with graph.locked(self.repo) as value:
            return graph.add_insight(value, text, kind, source, about, repo=self.repo)["id"]

    def task(self, task_id, status="done", paths=("src/a.py",), refs=(), area="quality", **extra):
        self.rows.append(dict(id=task_id, title=f"Task {task_id}", status=status, paths=list(paths),
                              graph_refs=list(refs), area=area, **extra))

    def dream(self):
        write_ledger(self.state, self.rows)
        return graph.sync(self.repo, dream_after=True)["dream"]

    def edge(self, src, rel, dst):
        src, rel, dst = dreaming.normalize_link(src, rel, dst)
        return load(self.repo)["edges"].get(graph.edge_key(src, rel, dst))

    def dropped(self, src, rel, dst):
        key = graph.edge_key(*dreaming.normalize_link(src, rel, dst))
        return [row["reason"] for row in load(self.repo)["dream"]["dropped"] if row["key"] == key]


class InferenceRuleTests(DreamCase):
    def test_applies_needs_two_successes(self):
        tip = self.note("Seed the cache before timing the importer")
        self.task("t1", refs=[tip])
        self.dream()
        self.assertIsNone(self.edge(tip, "applies_to", "file:src/a.py"), "one success is not a pattern")
        self.task("t2", refs=[tip])
        report = self.dream()
        edge = self.edge(tip, "applies_to", "file:src/a.py")
        self.assertEqual((edge["rule"], edge["generation"], edge["confidence"]), ("cited-on-file", 1, round(2 / 3, 3)))
        self.assertEqual(edge["support"], {"done": 2, "failed": 0})
        self.assertEqual(set(edge["evidence"]["tasks"]), {"task:live:t1", "task:live:t2"})
        self.assertEqual(report["inferred"]["new"], 1)
        check_dream_invariants(self, load(self.repo))

    def test_noise_citations_below_a_real_share_of_the_file_are_ignored(self):
        main = self.note("The cache warmer must run before the importer starts")
        noise = self.note("Thumbnail sizes come from the theme manifest")
        for index in range(14):
            self.task(f"m{index}", refs=[main], paths=["src/b.py"])
        self.task("n1", refs=[noise], paths=["src/b.py"])
        self.task("n2", refs=[noise], paths=["src/b.py"])
        self.dream()
        self.assertIsNotNone(self.edge(main, "applies_to", "file:src/b.py"))
        self.assertIsNone(self.edge(noise, "applies_to", "file:src/b.py"), "2 of 16 cited tasks is noise, not a pattern")

    def test_blocked_is_neutral_rejected_contradicts_with_hysteresis(self):
        tip = self.note("Retry uploads with jitter")
        self.task("t1", refs=[tip])
        self.task("t2", refs=[tip])
        self.dream()
        self.task("b1", status="blocked", refs=[tip], blocked_reason="Simulator down")
        self.task("b2", status="blocked", refs=[tip], blocked_reason="Simulator down")
        self.dream()
        self.assertEqual(self.edge(tip, "applies_to", "file:src/a.py")["support"], {"done": 2, "failed": 0},
                         "a blocker says nothing about whether the knowledge was right")
        self.task("t3", refs=[tip])
        self.task("r1", status="rejected", refs=[tip])
        self.task("r2", status="rejected", refs=[tip])
        self.dream()
        self.assertEqual(self.edge(tip, "applies_to", "file:src/a.py")["support"], {"done": 3, "failed": 2},
                         "kept: 3 >= 1.25 x 2, although 3 successes against 2 failures could not create it")
        self.task("r3", status="rejected", refs=[tip])
        self.dream()
        self.assertIsNone(self.edge(tip, "applies_to", "file:src/a.py"))
        self.assertEqual(self.dropped(tip, "applies_to", "file:src/a.py"), ["contradicted by later outcomes"])
        for index in range(3):
            self.task(f"d{index}", refs=[tip])
        self.dream()
        self.assertEqual(self.edge(tip, "applies_to", "file:src/a.py")["support"], {"done": 6, "failed": 3})
        fresh = self.note("Pin the locale in snapshot tests")
        for index, status in enumerate(("done", "done", "done", "rejected", "rejected")):
            self.task(f"f{index}", status=status, refs=[fresh], paths=["src/c.py"])
        self.dream()
        self.assertIsNone(self.edge(fresh, "applies_to", "file:src/c.py"), "creating needs 2 x failures: 3 < 4")

    def test_recorded_links_are_never_duplicated_by_inference(self):
        tip = self.note("Import order matters in the plugin loader", about=["src/a.py"])
        self.task("t1", refs=[tip])
        self.task("t2", refs=[tip])
        self.dream()
        self.assertIsNone(self.edge(tip, "applies_to", "file:src/a.py"))
        self.assertIsNotNone(self.edge(tip, "about", "file:src/a.py"))

    def test_archiving_knowledge_drops_its_inferred_links(self):
        tip = self.note("Run the migration twice to prove idempotency")
        self.task("t1", refs=[tip])
        self.task("t2", refs=[tip])
        self.dream()
        with graph.locked(self.repo) as value:
            graph.rate(value, tip, "archive")
        self.dream()
        self.assertIsNone(self.edge(tip, "applies_to", "file:src/a.py"))
        self.assertEqual(self.dropped(tip, "applies_to", "file:src/a.py"), ["endpoint archived or removed"])

    def test_evidence_from_pruned_tasks_is_retained_and_still_contradictable(self):
        tip = self.note("Warm the search index before load tests")
        self.task("a1", refs=[tip])
        self.task("a2", refs=[tip])
        self.dream()
        with mock.patch.object(graph, "MAX_TASKS", 4):
            for index in range(6):
                self.task(f"z{index}", paths=["src/d.py"])
            self.dream()
            nodes = load(self.repo)["nodes"]
            self.assertNotIn("task:live:a1", nodes, "the evidence tasks were pruned")
            edge = self.edge(tip, "applies_to", "file:src/a.py")
            self.assertEqual(edge["evidence"]["retained"], {"done": 2, "failed": 0})
            self.assertEqual(edge["support"], {"done": 2, "failed": 0}, "age alone never erases a relationship")
            self.task("zr1", status="rejected", refs=[tip])
            self.dream()
            self.assertEqual(self.edge(tip, "applies_to", "file:src/a.py")["support"], {"done": 2, "failed": 1})
            self.task("zr2", status="rejected", refs=[tip])
            self.dream()
            self.assertIsNone(self.edge(tip, "applies_to", "file:src/a.py"))

    def test_second_generation_through_strong_co_change_but_never_chained(self):
        tip = self.note("The ledger writer must hold the file lock", about=["src/a.py"])
        for index in range(4):
            self.task(f"ab{index}", paths=["src/a.py", "src/b.py"])
            self.task(f"bc{index}", paths=["src/b.py", "src/c.py"])
        self.dream()
        edge = self.edge(tip, "applies_to", "file:src/b.py")
        self.assertEqual((edge["rule"], edge["generation"]), ("via-co-change", 2))
        self.assertEqual(edge["confidence"], 0.8)
        self.assertEqual(edge["evidence"]["via"], [graph.edge_key(tip, "about", "file:src/a.py"),
                                                   graph.edge_key("file:src/a.py", "co_changed", "file:src/b.py")])
        self.assertIsNone(self.edge(tip, "applies_to", "file:src/c.py"), "never built on a second-generation link")
        for index in range(2):
            self.task(f"no{index}", status="rejected", refs=[tip], paths=["src/b.py"])
        self.dream()
        self.assertIsNone(self.edge(tip, "applies_to", "file:src/b.py"), "outcomes on that file contradict it")
        check_dream_invariants(self, load(self.repo))

    def test_area_needs_three_files_and_a_majority(self):
        for path in ("src/a.py", "src/b.py", "src/c.py"):
            self.task("p-" + path, paths=[path], area="performance")
        for path in ("ui/x.ts", "ui/y.ts", "ui/z.ts"):
            self.task("u-" + path, paths=[path], area="ui")
        tip = self.note("Profile with the release build only", about=["src/a.py", "src/b.py", "src/c.py"])
        self.dream()
        self.assertEqual(self.edge(tip, "applies_to", "area:performance")["confidence"], 1.0)
        mixed = self.note("Batch writes behind a single transaction",
                          about=["src/a.py", "src/b.py", "src/c.py", "ui/x.ts", "ui/y.ts"])
        self.dream()
        self.assertEqual(self.edge(mixed, "applies_to", "area:performance")["confidence"], 0.6)
        spread = self.note("Log the request id on every error path",
                           about=["src/a.py", "src/b.py", "src/c.py", "ui/x.ts", "ui/y.ts", "ui/z.ts"])
        self.dream()
        self.assertIsNone(self.edge(spread, "applies_to", "area:performance"), "3 of 6 is not a majority")
        two = self.note("Keep fixtures beside their tests", about=["src/a.py", "src/b.py"])
        self.dream()
        self.assertIsNone(self.edge(two, "applies_to", "area:performance"), "two files are too few")

    def test_co_citation_needs_overlap(self):
        left = self.note("Mock the clock in scheduler tests")
        right = self.note("Freeze time zones in scheduler fixtures", kind="technique")
        self.task("t1", refs=[left, right])
        self.task("t2", refs=[left, right])
        for index in range(10):
            self.task(f"r{index}", refs=[right], paths=["src/b.py"])
        busy_one = self.note("Use the staging bucket for media tests")
        busy_two = self.note("Rotate API keys through the vault helper", kind="technique")
        for index in range(20):
            self.task(f"x{index}", refs=[busy_one], paths=["src/c.py"])
            self.task(f"y{index}", refs=[busy_two], paths=["src/d.py"])
        self.task("xy1", refs=[busy_one, busy_two], paths=["ui/x.ts"])
        self.task("xy2", refs=[busy_one, busy_two], paths=["ui/x.ts"])
        self.dream()
        self.assertEqual(self.edge(left, "co_cited", right)["rule"], "cited-together")
        self.assertIsNone(self.edge(busy_one, "co_cited", busy_two), "2 of 22 citations is coincidence")

    def test_similar_wording_needs_a_shared_file_and_stays_short_of_a_merge(self):
        one = self.note("The upload worker retries need jitter to avoid thundering herds", about=["src/a.py"])
        two = self.note("Upload worker retries without jitter cause thundering herds", kind="technique",
                        about=["src/a.py"])
        far = self.note("Upload worker retries without jitter cause thundering herds today", kind="fact",
                        about=["src/b.py"])
        self.dream()
        edge = self.edge(one, "similar_to", two)
        self.assertEqual(edge["rule"], "shared-wording")
        self.assertIn("jitter", edge["evidence"]["words"])
        self.assertIsNone(self.edge(one, "similar_to", far), "same wording, different subject")
        twin = self.note("The upload worker retries need jitter to avoid the thundering herds", about=["src/a.py"])
        self.assertEqual(twin, one, "a near-duplicate merges instead of linking")

    def test_blockers_that_keep_stopping_a_file_affect_it(self):
        self.task("b1", status="blocked", paths=["ui/x.ts"], blocked_reason="Simulator 17 unavailable")
        self.dream()
        blocker = next(key for key in load(self.repo)["nodes"] if key.startswith("blocker:"))
        self.assertIsNone(self.edge(blocker, "affects", "file:ui/x.ts"))
        self.task("b2", status="blocked", paths=["ui/x.ts"], blocked_reason="Simulator 18 unavailable")
        self.dream()
        self.assertEqual(self.edge(blocker, "affects", "file:ui/x.ts")["support"], {"done": 2, "failed": 0})

    def test_rejected_links_are_removed_and_never_inferred_again(self):
        one = self.note("Cache keys include the tenant id", about=["src/a.py"])
        two = self.note("Cache keys without the tenant id leak data", kind="technique", about=["src/a.py"])
        self.dream()
        self.assertIsNotNone(self.edge(one, "similar_to", two))
        with graph.locked(self.repo) as value:
            result = dreaming.reject_link(value, two, "similar_to", one, reason="different concerns")
        self.assertTrue(result["removed"])
        for _ in range(3):
            self.dream()
        self.assertIsNone(self.edge(one, "similar_to", two))
        self.assertEqual(load(self.repo)["dream"]["rejected"][result["key"]]["reason"], "different concerns")
        cited = self.note("Bump the cache version after schema edits")
        self.task("t1", refs=[cited], paths=["src/b.py"])
        self.task("t2", refs=[cited], paths=["src/b.py"])
        self.dream()
        with graph.locked(self.repo) as value:
            dreaming.reject_link(value, cited, "applies_to", "file:src/b.py", reason="coincidence")
        self.task("t3", refs=[cited], paths=["src/b.py"])
        for _ in range(2):
            self.dream()
        self.assertIsNone(self.edge(cited, "applies_to", "file:src/b.py"), "more evidence does not override a rejection")
        with graph.locked(self.repo) as value:
            with self.assertRaises(graph.GraphError):
                dreaming.reject_link(value, one, "about", "file:src/a.py")
            with self.assertRaises(graph.GraphError):
                dreaming.reject_link(value, one, "applies_to", "file:missing.py")
            with self.assertRaises(graph.GraphError):
                graph.link(value, one, "similar_to", two) and dreaming.reject_link(value, one, "similar_to", two)

    def test_dreaming_twice_on_unchanged_facts_changes_nothing(self):
        tip = self.note("Seed the cache before timing the importer")
        other = self.note("Importer timing needs a seeded cache first", kind="technique")
        for index in range(3):
            self.task(f"t{index}", refs=[tip, other], paths=["src/a.py", "src/b.py"])
        self.dream()
        first = {key: edge["confidence"] for key, edge in load(self.repo)["edges"].items() if edge.get("inferred")}
        report = self.dream()
        second = {key: edge["confidence"] for key, edge in load(self.repo)["edges"].items() if edge.get("inferred")}
        self.assertEqual(first, second)
        self.assertEqual((report["inferred"]["new"], report["inferred"]["dropped"]), (0, 0))
        self.assertGreater(len(first), 3)

    def test_context_finds_knowledge_through_inferred_links_after_its_evidence_ages_out(self):
        learned = self.note("Compaction must skip open transactions")
        self.task("a1", refs=[learned], paths=["src/a.py"])
        self.task("a2", refs=[learned], paths=["src/a.py"])
        self.dream()
        with mock.patch.object(graph, "MAX_TASKS", 2):
            self.task("z1", paths=["src/b.py"])
            self.task("z2", paths=["src/b.py"])
            self.dream()
        value = load(self.repo)
        self.assertNotIn("task:live:a1", value["nodes"], "the citing tasks were pruned")
        self.assertIsNotNone(self.edge(learned, "applies_to", "file:src/a.py"), "the relationship outlives them")
        found = [row["id"] for row in graph.context(value, ["src/a.py"], repo=self.repo)["insights"]]
        self.assertEqual(found, [learned], "the inferred link is now the only path, and it is enough")
        self.rows = [row for row in self.rows if row["id"].startswith("z")] + [
            dict(row, graph_refs=[]) for row in self.rows if row["id"].startswith("a")]

    def test_context_weighs_inferred_links_by_confidence_and_follows_related_knowledge(self):
        value = graph.fill_defaults(graph.GraphState(graph.empty_graph()))
        graph.upsert(value, "file:src/a.py", "file", "src/a.py")
        for node_id in ("insight:recorded", "insight:sure", "insight:unsure", "insight:partner"):
            graph.upsert(value, node_id, "insight", node_id, summary=node_id, kind="fact", score=0.5)
        graph.link(value, "insight:recorded", "about", "file:src/a.py")
        for node_id, confidence in (("insight:sure", 0.9), ("insight:unsure", 0.5)):
            value["edges"][graph.edge_key(node_id, "applies_to", "file:src/a.py")] = dict(
                src=node_id, rel="applies_to", dst="file:src/a.py", inferred=True, confidence=confidence)
        value["edges"][graph.edge_key("insight:partner", "co_cited", "insight:sure")] = dict(
            src="insight:partner", rel="co_cited", dst="insight:sure", inferred=True, confidence=0.8)
        found = [row["id"] for row in graph.context(value, ["src/a.py"])["insights"]]
        self.assertEqual(found, ["insight:recorded", "insight:sure", "insight:unsure", "insight:partner"])


class ForgettingTests(DreamCase):
    def test_unused_notes_go_dormant_then_are_forgotten_within_one_run(self):
        junk = self.note("A guess about the renderer that nobody needed")
        for tick in range(1, dreaming.DORMANT_TICKS):
            self.dream()
        self.assertFalse(load(self.repo)["nodes"][junk].get("archived"))
        self.dream()
        node = load(self.repo)["nodes"][junk]
        self.assertTrue(node["dormant"])
        self.assertEqual(node["archived"], f"dormant: unused for {dreaming.DORMANT_TICKS} dreams")
        self.assertNotIn(junk, graph.brief(load(self.repo), "g", "r"), "dormant knowledge leaves the brief at once")
        for _ in range(dreaming.FORGET_TICKS):
            self.dream()
        value = load(self.repo)
        self.assertNotIn(junk, value["nodes"])
        self.assertEqual(len(value["runs"]), 1, "all of this happened inside one run")
        row = graph.history(self.repo, node_id=junk)["rows"][0]
        self.assertEqual(row["op"], "forgotten")
        self.assertIn("dormant", row["reason"])

    def test_valuable_and_protected_knowledge_is_never_cleaned_up(self):
        cited = self.note("Cited knowledge proves itself")
        self.task("t1", refs=[cited])
        reinforced = self.note("Reinforced knowledge is worth keeping")
        self.note("Reinforced knowledge is worth keeping!")
        useful = self.note("Useful knowledge was marked by an agent")
        pinned = self.note("Pinned knowledge is kept on purpose")
        from_user = self.note("Knowledge from the user is never auto-forgotten", source="user")
        learning.add_lesson(self.repo, "Lessons are managed by the learning store.")
        with graph.locked(self.repo) as value:
            graph.rate(value, useful, "useful")
            graph.rate(value, pinned, "pin")
        for _ in range(dreaming.DORMANT_TICKS + dreaming.FORGET_TICKS + 2):
            self.dream()
        value = load(self.repo)
        for node_id in (cited, reinforced, useful, pinned, from_user):
            self.assertIn(node_id, value["nodes"])
            self.assertFalse(value["nodes"][node_id].get("archived"), node_id)
        self.assertTrue(any(node.get("source") == "learning" and not node.get("archived")
                            for node in value["nodes"].values()))

    def test_deleted_files_and_unused_gates_are_forgotten_only_when_unreferenced(self):
        self.task("a", paths=["src/c.py"])
        self.task("b", paths=["src/d.py"])
        write_run(self.state, "live", {"py.fast": 4, "old.gate": 30})
        self.dream()
        write_run(self.state, "live", {"py.fast": 4})
        untrack(self.repo, "src/c.py", "src/d.py")
        self.rows = [row for row in self.rows if row["id"] != "a"]
        for _ in range(max(dreaming.STALE_FILE_TICKS, dreaming.GATE_TICKS) + 1):
            self.dream()
        value = load(self.repo)
        self.assertNotIn("file:src/c.py", value["nodes"], "deleted and nothing refers to it")
        self.assertIn("file:src/d.py", value["nodes"], "deleted but a task still refers to it")
        self.assertTrue(value["nodes"]["file:src/d.py"]["stale"])
        self.assertNotIn("gate:old.gate", value["nodes"])
        self.assertIn("gate:py.fast", value["nodes"])
        reasons = {row["id"]: row["reason"] for row in graph.history(self.repo)["rows"]}
        self.assertIn("deleted from the repository", reasons["file:src/c.py"])
        self.assertIn("no task used this gate", reasons["gate:old.gate"])


class RestoreAndRecallTests(DreamCase):
    def forget(self, node_id, reason="test cleanup"):
        with graph.locked(self.repo) as value:
            graph.remove_node(value, node_id, reason=reason, op="forgotten")

    def test_restore_brings_back_fields_links_and_history_state(self):
        tip = self.note("Run fixtures serially in the payments suite", about=["src/a.py", "src/b.py"])
        self.task("t1", refs=[tip])
        self.dream()
        before = load(self.repo)
        node_before = before["nodes"][tip]
        links_before = {key for key, edge in before["edges"].items() if tip in (edge["src"], edge["dst"])}
        self.forget(tip)
        self.assertNotIn(tip, load(self.repo)["nodes"])
        self.assertEqual(graph.history(self.repo)["total"], 1)
        with graph.locked(self.repo) as value:
            graph.restore_node(value, tip, "needed again")
        value = load(self.repo)
        node = value["nodes"][tip]
        volatile = {"restored", "restored_seq", "protected_until_tick", "last_tick"}
        self.assertEqual({k: v for k, v in node.items() if k not in volatile},
                         {k: v for k, v in node_before.items() if k not in volatile})
        self.assertEqual(node["restored"]["reason"], "needed again")
        self.assertEqual({key for key, edge in value["edges"].items() if tip in (edge["src"], edge["dst"])}, links_before)
        self.assertEqual(graph.history(self.repo)["total"], 0)
        self.assertEqual(graph.history(self.repo, include_restored=True)["rows"][0]["restored_at"] is not None, True)
        with graph.locked(self.repo) as value, self.assertRaises(graph.GraphError):
            graph.restore_node(value, tip)
        with graph.locked(self.repo) as value, self.assertRaises(graph.GraphError):
            graph.restore_node(value, "insight:never-existed")

    def test_restoring_a_pruned_task_reverses_its_folded_counts_exactly(self):
        tip = self.note("Hold the cache lock while evicting")
        for index in range(6):
            self.task(f"a{index}", status=("done", "rejected")[index % 2], paths=["src/a.py"], refs=[tip])
        self.dream()
        expected_file = load(self.repo)["nodes"]["file:src/a.py"]["stats"]
        expected_cited = (load(self.repo)["nodes"][tip]["cited_done"], load(self.repo)["nodes"][tip]["cited_failed"])
        with mock.patch.object(graph, "MAX_TASKS", 3):
            self.dream()
            value = load(self.repo)
            self.assertEqual(value["nodes"]["file:src/a.py"]["stats"], expected_file, "pruning conserves counts")
            self.assertEqual((value["nodes"][tip]["cited_done"], value["nodes"][tip]["cited_failed"]), expected_cited)
            pruned = [row["id"] for row in graph.history(self.repo)["rows"] if row["id"].startswith("task:")]
            self.assertEqual(len(pruned), 3)
            with graph.locked(self.repo) as value:
                for task_id in pruned:
                    graph.restore_node(value, task_id, "audit")
            self.dream()
            value = load(self.repo)
            self.assertTrue(set(pruned) <= set(value["nodes"]), "restored tasks are protected from re-pruning")
            self.assertEqual(value["nodes"]["file:src/a.py"]["stats"], expected_file, "no double counting")
            self.assertEqual((value["nodes"][tip]["cited_done"], value["nodes"][tip]["cited_failed"]), expected_cited)
            absent = {node_id.rsplit(":", 1)[1] for node_id in (f"task:live:a{index}" for index in range(6))
                      if node_id not in value["nodes"]}
            self.assertEqual(len(absent), 3, "others were pruned in their place")
            self.assertEqual(set(value["runs"][0].get("pruned")), absent, "the prune list matches what is gone")

    def test_restoring_a_merged_note_undoes_the_merge(self):
        weaker = self.note("Snapshot tests need the locale pinned to en-US")
        self.note("Snapshot tests need the locale pinned to en-US!")  # Reinforced once.
        with graph.locked(self.repo) as value:
            stronger = graph.upsert(value, "insight:twin", "insight", "twin", 1,
                                    summary="Snapshot tests need locale pinned to en-US always", kind="pitfall",
                                    reinforced=3, useful=1, wrong=0)["id"]
        self.dream()
        value = load(self.repo)
        self.assertEqual(graph.resolve(value, weaker), stronger, "the better-supported note is kept")
        self.assertEqual((value["nodes"][stronger]["reinforced"], value["nodes"][stronger]["useful"]), (5, 1))
        self.assertEqual(len(value["nodes"][stronger]["merged_from"]), 1)
        with graph.locked(self.repo) as value:
            graph.restore_node(value, weaker, "they were different after all")
        value = load(self.repo)
        self.assertNotIn(weaker, value["aliases"])
        self.assertEqual((value["nodes"][stronger]["reinforced"], value["nodes"][stronger]["useful"]), (3, 1))
        self.assertEqual(value["nodes"][stronger].get("merged_from", []), [])
        self.assertEqual(value["nodes"][weaker]["reinforced"], 2)
        self.assertEqual(value["nodes"][weaker]["restored"]["from_op"], "merged")

    def test_citing_forgotten_knowledge_brings_it_back(self):
        tip = self.note("Regenerate the API client after schema edits")
        self.forget(tip)
        self.task("t1", refs=[tip])
        self.dream()
        node = load(self.repo)["nodes"][tip]
        self.assertEqual(node["restored"]["by"], "recall")
        self.assertEqual(node["restored"]["reason"], "cited by task t1")
        self.assertEqual(node["cited_done"], 1)

    def test_writing_forgotten_knowledge_again_brings_back_the_original(self):
        tip = self.note("Regenerate the API client after schema edits")
        self.forget(tip)
        self.assertEqual(self.note("Regenerate the API client after schema edits!"), tip)
        self.forget(tip)
        self.assertEqual(self.note("Regenerate the API client after any schema edits"), tip, "a close paraphrase too")
        self.assertEqual(load(self.repo)["nodes"][tip]["restored"]["reason"], "the same knowledge was recorded again")

    def test_returning_work_brings_back_a_forgotten_file_with_its_history(self):
        with mock.patch.object(graph, "MAX_TASKS", 1):
            for index in range(4):
                self.task(f"c{index}", paths=["src/c.py"])
            self.task("other", paths=["src/a.py"])
            self.dream()
        self.rows = [row for row in self.rows if not row["id"].startswith("c")]
        untrack(self.repo, "src/c.py")
        for _ in range(dreaming.STALE_FILE_TICKS + 1):
            self.dream()
        self.assertNotIn("file:src/c.py", load(self.repo)["nodes"])
        (self.repo / "src/c.py").write_text("back\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "src/c.py"], check=True)
        self.task("again", paths=["src/c.py"])
        self.dream()
        stats = load(self.repo)["nodes"]["file:src/c.py"]["stats"]
        self.assertEqual(stats["done"], 5, "four remembered outcomes plus the new one")

    def test_restore_from_the_command_line_uses_a_partial_session(self):
        tip = self.note("Purge the CDN after asset renames", about=["ui/x.ts"])
        self.forget(tip)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/improve_graph.py"), "--repo", str(self.repo),
                                 "restore", tip, "--reason", "the CDN bug came back"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(toon.loads(result.stdout)["restored"]["reason"], "the CDN bug came back")
        self.assertIn(graph.edge_key(tip, "about", "file:ui/x.ts"), load(self.repo)["edges"])
        listing = subprocess.run([sys.executable, str(ROOT / "scripts/improve_graph.py"), "--repo", str(self.repo),
                                  "history", "--all", "--query", "cdn"], capture_output=True, text=True)
        self.assertEqual(toon.loads(listing.stdout)["rows"][0]["id"], tip)


class AssessAndConsolidateTests(DreamCase):
    def test_proven_contested_and_knowledge_gaps(self):
        proven = self.note("Retry the flaky upload once")
        contested = self.note("Disable the CSS minifier in tests")
        for index in range(3):
            self.task(f"p{index}", refs=[proven])
        for index, status in enumerate(("done", "done", "rejected", "rejected")):
            self.task(f"c{index}", status=status, refs=[contested], paths=["src/b.py"])
        for index in range(3):
            self.task(f"g{index}", status="rejected", paths=["ui/z.ts"])
        self.dream()
        value = load(self.repo)
        self.assertEqual(value["nodes"][proven]["verdict"], "proven")
        self.assertEqual(value["nodes"][contested]["verdict"], "contested")
        self.assertEqual([gap["file"] for gap in value["dream"]["gaps"]], ["ui/z.ts"])
        text = graph.brief(value, "graph.py", "/repo")
        self.assertIn("proven", text)
        self.assertIn("contested: verify before relying on it", text)
        self.assertIn("ui/z.ts: 0/3 verified", text)
        self.note("The z view crashes without its feature flag", about=["ui/z.ts"])
        self.dream()
        self.assertEqual(load(self.repo)["dream"]["gaps"], [], "a gap closes once something is known")

    def test_consolidation_proposals_and_their_safe_application(self):
        one = self.note("Upload retries need jitter to avoid thundering herds", about=["src/a.py"])
        two = self.note("Upload retries without jitter cause thundering herds", kind="technique", about=["src/a.py"])
        three = self.note("Thundering herds come from upload retries lacking jitter", kind="fact", about=["src/a.py"])
        mine = self.note("Upload retries lacking jitter make thundering herds", kind="decision", about=["src/a.py"],
                         source="user")
        self.dream()
        proposals = load(self.repo)["dream"]["proposals"]
        self.assertEqual(len(proposals), 1)
        members = proposals[0]["members"]
        self.assertEqual(set(members), {one, two, three}, "user knowledge is never offered for consolidation")
        with graph.locked(self.repo) as value:
            graph.rate(value, three, "pin")
        with graph.locked(self.repo) as value, self.assertRaises(graph.GraphError):
            dreaming.consolidate(value, "dream:unknown", "Anything at all here", "technique")
        with graph.locked(self.repo) as value:
            summary = dreaming.consolidate(value, proposals[0]["id"],
                                           "Always add jitter to upload retries; without it they cause thundering herds",
                                           "technique", consume=[one, two, three, mine, "insight:outsider"])
        value = load(self.repo)
        self.assertEqual(set(summary["consolidated_from"]), {one, two}, "pinned, user, and outside ids are refused")
        self.assertIn(three, value["nodes"])
        self.assertIn(mine, value["nodes"])
        self.assertEqual(graph.resolve(value, one), summary["id"])
        self.assertEqual(value["dream"]["proposals"], [])
        rows = {row["id"]: row for row in graph.history(self.repo)["rows"]}
        self.assertEqual(rows[one]["op"], "consolidated")
        self.assertEqual(rows[one]["superseded_by"], summary["id"])
        self.assertIn(graph.edge_key(summary["id"], "about", "file:src/a.py"), value["edges"])
        self.task("t1", refs=[one])
        self.dream()
        self.assertEqual(load(self.repo)["nodes"][summary["id"]]["cited_done"], 1, "old ids credit the consolidated note")


@slow
class SeventyTwoHourDreamTests(unittest.TestCase):
    """A 72-hour run in a world with a known truth, checked at every dream."""

    def test_dreaming_through_a_long_run_stays_accurate_clean_and_fast(self):
        with tempfile.TemporaryDirectory(prefix="improve dream soak ") as directory:
            previous = environment(Path(directory) / "home")
            try:
                self.soak(Path(directory))
            finally:
                restore_environment(previous)

    def soak(self, directory):
        world = World(directory / "repo", seed=17)
        junk, timings, dream_times, checkpoints = {}, [], [], 0
        for cycle in range(SOAK_CYCLES):
            world.advance()
            if cycle % 12 == 0:
                with graph.locked(world.repo, partial=True) as value:
                    node = graph.add_insight(value, f"Speculative note {cycle} about subsystem q{cycle} z{cycle}", "fact")
                    junk[node["id"]] = int(value.get("tick", 0))
            dreaming_now = cycle % DREAM_EVERY == DREAM_EVERY - 1
            started = time.perf_counter()
            result = graph.sync(world.repo, refine_after=cycle % 3 == 2, dream_after=dreaming_now)
            (dream_times if dreaming_now else timings).append(time.perf_counter() - started)
            if not dreaming_now:
                continue
            checkpoints += 1
            value = load(world.repo)
            self.assertEqual(value["tick"], checkpoints)
            check_invariants(self, value, refined=True)
            check_dream_invariants(self, value)
            precision, counts, recall = world.grade(value)
            if cycle >= 71:
                for rule, score in precision.items():
                    if counts[rule] >= 10:
                        self.assertGreaterEqual(score, 0.95, f"{rule} at cycle {cycle + 1}: {counts[rule]} links")
                self.assertGreaterEqual(recall, 0.55, f"recall at cycle {cycle + 1}")
            self.assertEqual(result["dream"]["tick"], checkpoints)
        value = load(world.repo)
        lifetime = dreaming.DORMANT_TICKS + dreaming.FORGET_TICKS
        old_junk = [node_id for node_id, tick in junk.items() if value["tick"] - tick > lifetime]
        young = [node_id for node_id, tick in junk.items() if value["tick"] - tick < dreaming.DORMANT_TICKS]
        self.assertTrue(old_junk, "the run was long enough to forget something")
        for node_id in old_junk:
            self.assertNotIn(node_id, value["nodes"], "unused notes are cleaned up within the run")
        for node_id in young:
            self.assertIn(node_id, value["nodes"])
        forgotten = {row["id"] for row in graph.history(world.repo, 500, types=["insight"])["rows"]}
        self.assertTrue(set(old_junk) <= forgotten, "every cleaned-up note is in the history")
        for module_insights in world.insights.values():
            for node_id in module_insights:
                target = graph.resolve(value, node_id)
                self.assertIn(target, value["nodes"], "valuable knowledge is never cleaned up")
                self.assertFalse(value["nodes"][target].get("archived"))
        self.assertTrue(any(row.get("pruned") for row in value["runs"]), "the run was long enough to prune")
        with graph.locked(world.repo) as session:
            graph.restore_node(session, old_junk[0], "audit")
        self.assertIn(old_junk[0], load(world.repo)["nodes"])
        timings.sort()
        self.assertLess(timings[int(len(timings) * 0.95)], budget(1.0), "95th percentile cycle sync")
        self.assertLess(max(dream_times), budget(5.0), "slowest dream")


if __name__ == "__main__":
    unittest.main()
