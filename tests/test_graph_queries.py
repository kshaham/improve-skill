"""Every way of querying the knowledge graph, against a fixture with known answers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_support import budget, ROOT, archive, check_invariants, environment, load, make_repo, restore_environment, write_ledger, write_run  # noqa: E402
import improve_graph as graph  # noqa: E402
import improve_toon as toon  # noqa: E402
import improve_learning as learning  # noqa: E402

TRACKED = ["src/api/db.py", "src/api/auth.py", "src/api/pool.py", "src/ui/cart.tsx", "src/ui/button.tsx",
           "docs/guide.md", "README"]


class RichGraph(unittest.TestCase):
    """r1 (archived) and r2 (live) with known outcomes, gates, blockers, and knowledge."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="improve graph queries ")
        cls.previous = environment(Path(cls.temp.name) / "home")
        cls.repo = make_repo(Path(cls.temp.name) / "repo", TRACKED).resolve()
        archive(cls.repo, "2026-11-01", "r1", [
            dict(id="t1", title="Pool database connections", area="performance", status="rejected",
                 file="src/api/db.py", note="benchmark noise exceeded the gain"),
            dict(id="t2", title="Retry deadlocks", area="resilience", status="rejected", file="src/api/db.py:42",
                 note="tests flaked"),
            dict(id="t3", title="Token expiry", area="security", status="done",
                 paths=["src/api/auth.py", "src/api/db.py"], verification="py.fast passed", commit="abc1234"),
            dict(id="t4", title="Cart empty state", area="ui", status="blocked", file="src/ui/cart.tsx",
                 blocked_reason="Simulator 17 unavailable"),
        ], gates={"py.fast": 12})
        state = cls.repo / ".improve"
        write_run(state, "r2", {"py.fast": 11, "ts.fast": 40})
        write_ledger(state, [
            dict(id="n1", title="Button focus ring", area="ui", status="done",
                 paths=["src/ui/button.tsx", "src/ui/cart.tsx"], verification="ts.fast ok"),
            dict(id="n2", title="Cart labels", area="ui", status="blocked", file="src/ui/cart.tsx",
                 blocked_reason="Simulator 18 unavailable"),
            dict(id="n3", title="Remove legacy module", area="quality", status="done", file="src/old.py"),
            dict(id="n4", title="Size the pool", area="performance", status="ready", file="src/api/pool.py"),
        ])
        learning.add_lesson(cls.repo, "Benchmarks on this machine need ten interleaved runs.", area="performance")
        graph.sync(cls.repo)
        with graph.locked(cls.repo) as value:
            cls.pitfall = graph.add_insight(value, "db.py retry tests flake under parallel pytest; run serially",
                                            "pitfall", about=["src/api/db.py", "area:resilience"], repo=cls.repo)["id"]
            cls.technique = graph.add_insight(value, "Snapshot tests catch spacing regressions in cart screens",
                                              "technique", about=["area:ui"])["id"]
            cls.decision = graph.add_insight(value, "Authentication code is frozen until the audit ends",
                                             "decision", source="user", about=["src/api/auth.py"], repo=cls.repo)["id"]
            graph.rate(value, cls.decision, "pin")
            cls.archived = graph.add_insight(value, "The database pool never needs tuning at all",
                                             "fact", about=["src/api/db.py"], repo=cls.repo)["id"]
            graph.rate(value, cls.archived, "archive")
            graph.refine(value, cls.repo)
        cls.graph = load(cls.repo)

    @classmethod
    def tearDownClass(cls):
        restore_environment(cls.previous)
        cls.temp.cleanup()

    def context(self, *args, **kwargs):
        return graph.context(self.graph, *args, repo=self.repo, **kwargs)


class ContextQueryTests(RichGraph):
    def test_fixture_is_consistent(self):
        check_invariants(self, self.graph, refined=True)

    def test_exact_file_returns_history_knowledge_gates_and_coupling(self):
        result = self.context(["src/api/db.py"])
        self.assertEqual(result["seeds"], ["file:src/api/db.py"])
        self.assertEqual(result["files"][0]["attempts"], 3)
        self.assertEqual(result["files"][0]["done"], 1)
        self.assertEqual({task["id"] for task in result["past_tasks"]},
                         {"task:r1:t1", "task:r1:t2", "task:r1:t3"})
        insight_ids = [row["id"] for row in result["insights"]]
        self.assertEqual(insight_ids[0], self.pitfall, "direct knowledge outranks everything else")
        self.assertNotIn(self.archived, insight_ids, "archived knowledge is never served")
        self.assertEqual([gate["label"] for gate in result["gates"]], ["py.fast"])
        self.assertEqual([row["label"] for row in result["co_changed"]], ["src/api/auth.py"])
        rejected = next(task for task in result["past_tasks"] if task["id"] == "task:r1:t1")
        self.assertEqual(rejected["outcome_note"], "benchmark noise exceeded the gain")

    def test_path_spellings_resolve_to_the_same_file(self):
        expected = self.context(["src/api/db.py"])
        for spelling in ("src/api/db.py:42", "./src/api/db.py", str(self.repo / "src/api/db.py"), " src/api/db.py "):
            with self.subTest(spelling=spelling):
                self.assertEqual(self.context([spelling]), expected)

    def test_unsafe_or_foreign_paths_are_ignored(self):
        for spelling in ("../src/api/db.py", "/etc/passwd", ".improve/graph.json", "src/../../x", "", "a\x00b"):
            with self.subTest(spelling=spelling):
                result = self.context([spelling])
                self.assertEqual((result["seeds"], result["past_tasks"], result["insights"]), ([], [], []))
        self.assertEqual(self.context(["/etc/passwd", "src/api/db.py"])["seeds"], ["file:src/api/db.py"])

    def test_directory_and_new_file_use_neighbors(self):
        directory = self.context(["src/api"])
        self.assertEqual(set(directory["seeds"]), {"file:src/api/db.py", "file:src/api/auth.py", "file:src/api/pool.py"})
        sibling = self.context(["src/api/brand_new.py"])
        self.assertEqual(set(sibling["seeds"]), set(directory["seeds"]))
        self.assertEqual(self.context(["src/api/"])["seeds"], directory["seeds"])
        self.assertEqual(set(self.context(["src"])["seeds"]), set(directory["seeds"]) | {
            "file:src/ui/cart.tsx", "file:src/ui/button.tsx", "file:src/old.py"}, "a parent directory covers all below")
        self.assertEqual(self.context(["src/brand_new.py"])["seeds"], ["file:src/old.py"], "siblings, not cousins")
        self.assertEqual(self.context(["lib/new/deep.py"])["seeds"], [], "a new directory has no history yet")

    def test_area_only_and_unknown_area(self):
        result = self.context(area="ui")
        self.assertEqual(result["seeds"], ["area:ui"])
        self.assertIn(self.technique, [row["id"] for row in result["insights"]])
        self.assertTrue({"task:r2:n1", "task:r2:n2", "task:r1:t4"} <= {task["id"] for task in result["past_tasks"]})
        self.assertEqual({row["label"] for row in result["blockers"]},
                         {"Simulator 18 unavailable"}, "numbers do not split one recurring blocker")
        self.assertEqual(self.context(area="no-such-area")["seeds"], [])
        with self.assertRaises(graph.GraphError):
            self.context()

    def test_text_only_and_combined_queries(self):
        text = self.context(text="flake parallel pytest")
        self.assertEqual(text["seeds"], [])
        self.assertEqual(text["insights"][0]["id"], self.pitfall)
        self.assertEqual(text["past_tasks"], [])
        combined = self.context(["src/ui/cart.tsx"], area="ui", text="spacing snapshot")
        self.assertEqual(combined["seeds"], ["file:src/ui/cart.tsx", "area:ui"])
        self.assertIn(self.technique, [row["id"] for row in combined["insights"]])
        self.assertEqual(self.context(text="the and of"), {**self.context(text="the and of")})

    def test_limits_ordering_and_determinism(self):
        self.assertEqual(len(self.context(area="ui", limit=1)["past_tasks"]), 1)
        self.assertLessEqual(len(self.context(area="ui")["blockers"]), 4)
        self.assertEqual(self.context(["src/api"], area="performance"), self.context(["src/api"], area="performance"))

    def test_stale_file_is_flagged_but_still_answers(self):
        result = self.context(["src/old.py"])
        self.assertTrue(result["files"][0]["stale"])
        self.assertEqual([task["id"] for task in result["past_tasks"]], ["task:r2:n3"])

    def test_seed_count_is_bounded(self):
        value = json.loads(json.dumps(self.graph))
        for index in range(60):
            graph.upsert(value, f"file:pkg/f{index}.py", "file", f"pkg/f{index}.py")
        self.assertEqual(len(graph.context(value, ["pkg"])["seeds"]), 40)


class SearchQueryTests(RichGraph):
    def test_ranking_types_archived_and_limits(self):
        hits = graph.search(self.graph, "Simulator 18 unavailable")
        self.assertEqual(hits[0]["type"], "blocker", "an exact label beats a task mentioning it")
        self.assertTrue(all("match" in row for row in hits))
        self.assertEqual([row["match"] for row in hits], sorted((row["match"] for row in hits), reverse=True))
        self.assertTrue(all(row["type"] == "task" for row in graph.search(self.graph, "cart", types=("task",))))
        self.assertNotIn(self.archived, [row["id"] for row in graph.search(self.graph, "pool tuning")])
        self.assertIn(self.archived, [row["id"] for row in graph.search(self.graph, "pool tuning", include_archived=True)])
        self.assertEqual(len(graph.search(self.graph, "cart", limit=2)), 2)

    def test_exact_label_match_is_boosted(self):
        hits = graph.search(self.graph, "Token expiry")
        self.assertEqual(hits[0]["id"], "task:r1:t3")

    def test_case_punctuation_paths_and_empty_queries(self):
        self.assertEqual(graph.search(self.graph, "CART"), graph.search(self.graph, "cart"))
        self.assertIn("file:src/api/db.py", [row["id"] for row in graph.search(self.graph, "db.py")])
        for empty in ("", "   ", "the a of", "!!! ???", "x"):
            with self.subTest(query=empty):
                self.assertEqual(graph.search(self.graph, empty), [])
        self.assertEqual(graph.search(self.graph, "zzzz-no-match"), [])

    def test_ties_break_deterministically(self):
        self.assertEqual(graph.search(self.graph, "ui"), graph.search(json.loads(json.dumps(self.graph)), "ui"))


class NodeAndPathQueryTests(RichGraph):
    def test_node_detail_links_directions_and_aliases(self):
        detail = graph.node_detail(self.graph, "file:src/api/db.py")
        self.assertEqual(detail["node"]["stats"]["attempts"], 3)
        rels = {(row["rel"], row["direction"]) for row in detail["links"]}
        self.assertIn(("touches", "in"), rels)
        self.assertIn(("about", "in"), rels)
        self.assertIn(("co_changed", "out"), rels | {("co_changed", "out")})
        self.assertEqual(detail["total_links"], len(detail["links"]))
        value = json.loads(json.dumps(self.graph))
        value["aliases"]["insight:old"] = self.pitfall
        self.assertEqual(graph.node_detail(value, "insight:old")["node"]["id"], self.pitfall)
        with self.assertRaises(graph.GraphError):
            graph.node_detail(self.graph, "file:missing.py")

    def test_node_detail_limit_keeps_the_strongest_links(self):
        value = json.loads(json.dumps(self.graph))
        for index in range(50):
            node = graph.upsert(value, f"task:x:{index}", "task", f"t{index}", 1, status="done")
            node["score"] = 0.5 + index / 100
            graph.link(value, node["id"], "touches", "file:src/api/pool.py")
        detail = graph.node_detail(value, "file:src/api/pool.py", limit=10)
        self.assertEqual(len(detail["links"]), 10)
        self.assertEqual(detail["total_links"], 51)
        self.assertEqual({row["id"] for row in detail["links"]}, {f"task:x:{index}" for index in range(40, 50)})

    def test_shortest_path_variants(self):
        steps = graph.shortest_path(self.graph, "file:src/api/auth.py", "area:performance")
        self.assertEqual(steps[0]["from"], "file:src/api/auth.py")
        self.assertEqual(steps[-1]["to"], "area:performance")
        for left, right in zip(steps, steps[1:]):
            self.assertEqual(left["to"], right["from"])
        self.assertEqual(len(graph.shortest_path(self.graph, "file:src/api/db.py", "task:r1:t1")), 1)
        self.assertEqual(graph.shortest_path(self.graph, "area:ui", "area:ui"), [])
        value = json.loads(json.dumps(self.graph))
        graph.upsert(value, "file:island.py", "file", "island.py")
        self.assertEqual(graph.shortest_path(value, "area:ui", "file:island.py"), [])
        chain = ["file:chain0.py"]
        graph.upsert(value, chain[0], "file", "chain0.py")
        for index in range(1, 9):
            chain.append(f"file:chain{index}.py")
            graph.upsert(value, chain[-1], "file", chain[-1])
            graph.link(value, chain[-2], "relates_to", chain[-1])
        self.assertEqual(len(graph.shortest_path(value, chain[0], chain[6])), 6)
        self.assertEqual(graph.shortest_path(value, chain[0], chain[7]), [], "beyond six steps")
        value["aliases"]["file:old0.py"] = chain[0]
        self.assertEqual(len(graph.shortest_path(value, "file:old0.py", chain[2])), 2)
        with self.assertRaises(graph.GraphError):
            graph.shortest_path(self.graph, "area:ui", "area:missing")

    def test_hotspots_threshold_order_and_staleness(self):
        rows = graph.hotspots(self.graph)
        self.assertEqual([row["file"] for row in rows], ["src/api/db.py", "src/ui/cart.tsx"])
        self.assertEqual([row["success"] for row in rows], sorted(row["success"] for row in rows))
        self.assertEqual(len(graph.hotspots(self.graph, limit=1)), 1)
        value = json.loads(json.dumps(self.graph))
        value["nodes"]["file:src/api/db.py"]["stale"] = True
        self.assertNotIn("src/api/db.py", [row["file"] for row in graph.hotspots(value)])


class BriefAndSummaryTests(RichGraph):
    def test_brief_orders_pinned_then_user_then_score_and_skips_noise(self):
        text = graph.brief(self.graph, "graph.py", "/repo")
        knowledge = [line for line in text.splitlines() if line.startswith("- [insight:")]
        self.assertTrue(knowledge[0].startswith(f"- [{self.decision}]"))
        self.assertIn("[pinned, from the user]", knowledge[0])
        self.assertIn("about src/api/auth.py", knowledge[0])
        self.assertNotIn(self.archived, text)
        self.assertNotIn("ten interleaved runs", text, "learning lessons already reach the prompt")
        self.assertIn("src/api/db.py: 1/3 verified", text)
        self.assertIn("Known gates and cost: py.fast 11s, ts.fast 40s", text)
        self.assertIn("context --files PATH... --area AREA", text)

    def test_brief_is_bounded_on_a_line_boundary(self):
        full = graph.brief(self.graph, "graph.py", "/repo")
        full_lines = set(full.splitlines())
        for limit in (50, 200, 400, len(full) - 1, 5000):
            with self.subTest(limit=limit):
                text = graph.brief(self.graph, "graph.py", "/repo", max_chars=limit)
                self.assertLessEqual(len(text), limit)
                # Whole lines only; a smaller budget drops notes, never half a line.
                self.assertTrue(all(line in full_lines or line.startswith(("(showing ", "Knowledge graph: "))
                                    for line in text.splitlines()))
        self.assertEqual(graph.brief(graph.empty_graph(), "g", "r"), "")

    def test_summary_counts_active_knowledge(self):
        summary = graph.summary(self.graph)
        self.assertEqual(summary["insights"], 4, "pitfall, technique, decision, lesson; archived excluded")
        self.assertEqual(summary["runs"], 2)
        self.assertEqual(summary["nodes"], len(self.graph["nodes"]))
        self.assertIsNotNone(summary["last_refine"])


class BoardViewQueryTests(RichGraph):
    def test_default_view_excludes_archived_and_puts_pinned_first(self):
        view = graph.board_view(self.graph)
        ids = [node["id"] for node in view["nodes"]]
        self.assertEqual(ids[0], self.decision)
        self.assertNotIn(self.archived, ids)
        self.assertIn(self.archived, [node["id"] for node in graph.board_view(self.graph, include_archived=True)["nodes"]])
        self.assertEqual(sum(view["counts"].values()), view["total_nodes"])
        self.assertFalse(view["truncated"] and len(ids) == view["total_nodes"])

    def test_limits_are_clamped(self):
        self.assertEqual(len(graph.board_view(self.graph, limit=1)["nodes"]), 10)
        self.assertTrue(graph.board_view(self.graph, limit=1)["truncated"])
        self.assertLessEqual(len(graph.board_view(self.graph, limit=10 ** 6)["nodes"]), 800)
        with self.assertRaises(ValueError):
            graph.board_view(self.graph, limit="many")

    def test_types_query_and_edges_stay_inside_the_selection(self):
        view = graph.board_view(self.graph, types=["file", "area"])
        selected = {node["id"] for node in view["nodes"]}
        self.assertTrue(all(node["type"] in ("file", "area") for node in view["nodes"]))
        self.assertTrue(all(edge["src"] in selected and edge["dst"] in selected for edge in view["edges"]))
        projected = [edge for edge in view["edges"] if edge["rel"] == "via_task"]
        self.assertTrue(projected, "hidden tasks still connect their files and areas")
        direct = {(edge["src"], edge["dst"]) for edge in self.graph["edges"].values()}
        self.assertFalse(any((edge["src"], edge["dst"]) in direct or (edge["dst"], edge["src"]) in direct
                             for edge in projected), "a projected link never duplicates a real one")
        hits = graph.board_view(self.graph, query="simulator")
        ids = {node["id"] for node in hits["nodes"]}
        blocker = next(node_id for node_id in ids if node_id.startswith("blocker:"))
        self.assertTrue({"task:r2:n2", "task:r1:t4"} <= ids, f"neighbors of {blocker} are shown with it")

    def test_focus_depth_grows_monotonically_and_clamps(self):
        sizes = [len(graph.board_view(self.graph, focus="file:src/api/db.py", depth=depth)["nodes"]) for depth in (1, 2, 3)]
        self.assertEqual(sizes, sorted(sizes))
        self.assertLess(sizes[0], sizes[2])
        self.assertEqual(len(graph.board_view(self.graph, focus="file:src/api/db.py", depth=0)["nodes"]), sizes[0])
        self.assertEqual(len(graph.board_view(self.graph, focus="file:src/api/db.py", depth=99)["nodes"]), sizes[2])
        self.assertLessEqual(len(graph.board_view(self.graph, focus="area:ui", depth=3, limit=10)["nodes"]), 10)
        value = json.loads(json.dumps(self.graph))
        value["aliases"]["file:renamed.py"] = "file:src/api/db.py"
        self.assertEqual(graph.board_view(value, focus="file:renamed.py")["focus"], "file:src/api/db.py")
        with self.assertRaises(graph.GraphError):
            graph.board_view(self.graph, focus="file:gone.py")

    def test_every_shown_type_keeps_a_share_when_one_type_dominates(self):
        value = json.loads(json.dumps(self.graph))
        for index in range(300):
            node = graph.upsert(value, f"insight:bulk{index}", "insight", f"bulk {index}", 2, kind="fact")
            node["score"] = 0.99
        view = graph.board_view(value, limit=40)
        kinds = {node["type"] for node in view["nodes"]}
        self.assertTrue({"insight", "file", "area", "task", "gate", "blocker"} <= kinds, kinds)
        self.assertEqual(len(view["nodes"]), 40)
        self.assertIn(self.decision, [node["id"] for node in view["nodes"]], "pinned nodes are always shown")
        self.assertGreater(sum(node["type"] == "insight" for node in view["nodes"]), 20, "strength still decides the rest")
        tasks = graph.board_view(value, limit=40, types=["insight", "task"])
        self.assertTrue(any(node["type"] == "task" for node in tasks["nodes"]), "switching a type on shows it")

    def test_projected_links_respect_fanout(self):
        value = graph.empty_graph()
        graph.upsert(value, "task:r:1", "task", "hub", 1)
        for index in range(20):
            graph.upsert(value, f"file:f{index:02d}.py", "file", f"f{index}.py", 1)
            graph.link(value, "task:r:1", "touches", f"file:f{index:02d}.py")
        selected = {f"file:f{index:02d}.py" for index in range(20)}
        self.assertEqual(len(graph.projected_edges(value, selected)), 8 * 7 // 2)


class WriteApiTests(RichGraph):
    def setUp(self):
        self.value = json.loads(json.dumps(self.graph))

    def test_target_id_variants(self):
        value = self.value
        self.assertEqual(graph.target_id(value, self.pitfall), self.pitfall)
        self.assertEqual(graph.target_id(value, "area:ux-polish"), "area:ux-polish")
        self.assertEqual(graph.target_id(value, "file:new/thing.py"), "file:new/thing.py")
        self.assertEqual(graph.target_id(value, "lib/new.ts", self.repo), "file:lib/new.ts")
        self.assertEqual(graph.target_id(value, str(self.repo / "src/ui/cart.tsx"), self.repo), "file:src/ui/cart.tsx")
        value["aliases"]["insight:merged"] = self.pitfall
        self.assertEqual(graph.target_id(value, "insight:merged"), self.pitfall)
        for bad in ("area:Bad Name", "nonsense", "file:../x.py", "gate:not-there", "/etc/passwd"):
            with self.subTest(bad=bad), self.assertRaises(graph.GraphError):
                graph.target_id(value, bad, self.repo)

    def test_link_rules(self):
        value = self.value
        with self.assertRaises(graph.GraphError):
            graph.link(value, self.pitfall, "likes", "area:ui")
        self.assertIsNone(graph.link(value, "area:ui", "relates_to", "area:ui"))
        self.assertIsNone(graph.link(value, "area:ui", "relates_to", "area:missing"))
        edge = graph.link(value, "file:src/ui/cart.tsx", "co_changed", "file:src/api/db.py")
        self.assertLess(edge["src"], edge["dst"])
        again = graph.link(value, "area:ui", "relates_to", "area:security", weight=3)
        self.assertIs(graph.link(value, "area:ui", "relates_to", "area:security"), again)
        self.assertEqual(again["weight"], 3)

    def test_insight_dedup_kinds_and_normalization(self):
        value = self.value
        same = graph.add_insight(value, "  DB.PY retry tests flake under   parallel pytest; run serially!! ", "pitfall")
        self.assertEqual(same["id"], self.pitfall)
        identical = graph.add_insight(value, "db.py retry tests flake under parallel pytest; run serially", "fact")
        self.assertEqual((identical["id"], identical["kind"]), (self.pitfall, "pitfall"),
                         "an identical sentence is the same knowledge whatever kind it is filed under")
        paraphrase = graph.add_insight(value, "db.py retry tests flake under parallel pytest, run them serially", "fact")
        self.assertNotEqual(paraphrase["id"], self.pitfall, "paraphrases only merge within one kind")
        long = graph.add_insight(value, "word " * 400, "fact")
        self.assertLessEqual(len(long["summary"]), graph.MAX_SUMMARY)
        self.assertLessEqual(len(long["label"]), 120)
        revived = graph.add_insight(value, "The database pool never needs tuning at all", "fact")
        self.assertEqual(revived["id"], self.archived)
        self.assertNotIn("archived", revived, "repeating archived knowledge revives it")
        quiet = graph.add_insight(value, "Snapshot tests catch spacing regressions in cart screens", "technique",
                                  source="user", reinforce=False)
        self.assertEqual((quiet["reinforced"], quiet["source"]), (1, "user"))

    def test_rating_state_machine(self):
        value, node_id = self.value, self.technique
        expect = [("useful", {"useful": 1}), ("wrong", {"wrong": 1}), ("pin", {"pinned": True}),
                  ("archive", {"pinned": False, "archived": "archived by agent"}), ("pin", {"pinned": True}),
                  ("unpin", {"pinned": False}), ("archive", {}), ("restore", {"restored_seq": 2}),
                  ("useful", {"useful": 2})]
        for verdict, fields in expect:
            node = graph.rate(value, node_id, verdict)
            for key, wanted in fields.items():
                self.assertEqual(node.get(key), wanted, (verdict, key))
            if verdict in ("pin", "restore", "useful"):
                self.assertNotIn("archived", node, verdict)
        self.assertEqual(graph.rate(value, node_id, "wrong", source="user")["archived"], "marked wrong by the user")
        with self.assertRaises(graph.GraphError):
            graph.rate(value, "insight:missing", "useful")
        with self.assertRaises(graph.GraphError):
            graph.rate(value, node_id, "maybe")


class CommandLineQueryTests(RichGraph):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/improve_graph.py"), "--repo", str(self.repo), *args],
                              capture_output=True, text=True, env=os.environ, timeout=budget(30))

    def test_every_read_command(self):
        cases = {
            ("context", "--files", "src/api/db.py"): "Past tasks here:",
            ("context", "--files", "unknown/x.py"): "The graph has nothing on these yet.",
            ("context", "--area", "ui", "--limit", "1"): "Past tasks here:",
            ("brief",): "Top knowledge",
            ("brief", "--max-chars", "60"): "Knowledge graph:",
            ("path", "area:ui", "area:ui"): "No connection within six steps",
        }
        for args, expected in cases.items():
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)
        as_json = json.loads(self.run_cli("context", "--files", "src/api/db.py", "--json").stdout)
        self.assertEqual(as_json["seeds"], ["file:src/api/db.py"])
        self.assertTrue(all(row["type"] == "blocker" for row in
                            toon.loads(self.run_cli("search", "simulator", "--type", "blocker").stdout)))
        self.assertEqual(len(toon.loads(self.run_cli("search", "cart", "--limit", "1").stdout)), 1)
        self.assertEqual(toon.loads(self.run_cli("node", "area:ui").stdout)["node"]["id"], "area:ui")
        self.assertEqual(toon.loads(self.run_cli("path", "file:src/api/db.py", "task:r1:t1").stdout)[0]["to"], "task:r1:t1")
        self.assertEqual(toon.loads(self.run_cli("hotspots", "--limit", "1").stdout)[0]["file"], "src/api/db.py")
        self.assertEqual(toon.loads(self.run_cli("stats").stdout)["runs"], 2)

    def test_errors_exit_two_without_tracebacks(self):
        missing = self.run_cli("node", "file:none.py")
        self.assertEqual(missing.returncode, 5, "nothing by that name has its own exit code")
        self.assertNotIn("Traceback", missing.stderr)
        for args in (("context",), ("path", "area:ui", "area:none"),
                     ("search", "x", "--type", "planet"), ("rate", "insight:none", "useful"),
                     ("note", "--text", "short"), ("link", "area:ui", "likes", "area:security"), ()):
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)

    def test_empty_repository_reads_are_friendly(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_repo(Path(directory), [])
            run = lambda *args: subprocess.run([sys.executable, str(ROOT / "scripts/improve_graph.py"), "--repo",
                                                str(repo), *args], capture_output=True, text=True, timeout=budget(30))
            self.assertEqual(run("brief").stdout.strip(), "The knowledge graph is empty.")
            self.assertEqual(toon.loads(run("stats").stdout)["nodes"], 0)
            self.assertEqual(toon.loads(run("hotspots").stdout), [])
            self.assertEqual(toon.loads(run("search", "anything").stdout), [])
            self.assertFalse(graph.graph_path(repo).exists(), "reads never create the store")


if __name__ == "__main__":
    unittest.main()
