"""Query quality and whole-graph analysis: context weighting, resolved blockers, retrieval,
code fingerprints, paths that avoid hubs, contradictions, clique proposals, module groups,
surprises, questions, reports, and export (review 2026-11-10, sections 4 and 8)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_support import budget, ROOT, environment, load, make_repo, restore_environment, write_ledger, write_run  # noqa: E402
import improve_analysis as analysis  # noqa: E402
import improve_dream as dreaming  # noqa: E402
import improve_graph as graph  # noqa: E402
import improve_graph_journal as journal  # noqa: E402
import improve_toon as toon  # noqa: E402


def cli(repo, *args):
    return subprocess.run([sys.executable, str(ROOT / "scripts/improve_graph.py"), "--repo", str(repo), *args],
                          capture_output=True, text=True, timeout=budget(60), env=dict(os.environ))


class AnalysisCase(unittest.TestCase):
    files = ["src/a.py", "src/b.py", "src/c.py", "src/d.py", "lib/x.py", "lib/y.py", "docs/z.md"]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve graph analysis ")
        self.previous = environment(Path(self.temp.name) / "home")
        self.repo = make_repo(Path(self.temp.name) / "repo", self.files).resolve()
        self.state = self.repo / ".improve"
        write_run(self.state, "live", {"test": 3})

    def tearDown(self):
        restore_environment(self.previous)
        self.temp.cleanup()

    def note(self, text, kind="fact", about=(), source="agent"):
        with graph.locked(self.repo) as value:
            return graph.add_insight(value, text, kind, source, list(about), repo=self.repo)["id"]


class ContextQualityTests(AnalysisCase):
    def test_should_rank_knowledge_by_the_outcome_of_the_tasks_that_cited_it(self):
        good = self.note("Use the bulk loader for the import pipeline")
        bad = self.note("Disable the index rebuild while importing rows")
        rows = [{"id": f"g{index}", "status": "done", "area": "quality", "title": "g", "paths": ["src/a.py"],
                 "graph_refs": [good], "completed_at": f"2026-11-0{index + 1}T00:00:00Z"} for index in range(2)]
        rows += [{"id": f"b{index}", "status": "rejected", "area": "quality", "title": "b", "paths": ["src/a.py"],
                  "graph_refs": [bad], "completed_at": f"2026-11-0{index + 1}T00:00:00Z"} for index in range(2)]
        write_ledger(self.state, rows)
        graph.sync(self.repo)
        value = load(self.repo)
        for node_id in (good, bad):
            value["nodes"][node_id]["score"] = 0.5  # Same standing, so only the outcome weighting differs.
        ranked = [row["id"] for row in graph.context(value, ["src/a.py"], repo=self.repo)["insights"]]
        self.assertLess(ranked.index(good), ranked.index(bad))

    def test_should_mark_a_blocker_resolved_once_later_work_succeeded(self):
        rows = [{"id": "t1", "status": "blocked", "area": "quality", "title": "blocked", "paths": ["src/a.py"],
                 "blocked_reason": "simulator was not available", "blocked_at": "2026-11-01T00:00:00Z"},
                {"id": "t2", "status": "done", "area": "quality", "title": "later", "paths": ["src/a.py"],
                 "completed_at": "2026-11-05T00:00:00Z"},
                {"id": "t3", "status": "blocked", "area": "quality", "title": "still", "paths": ["src/b.py"],
                 "blocked_reason": "license server unreachable", "blocked_at": "2026-11-06T00:00:00Z"}]
        write_ledger(self.state, rows)
        graph.sync(self.repo)
        result = graph.context(load(self.repo), ["src/a.py"], repo=self.repo)
        self.assertTrue(result["blockers"][0].get("resolved"))
        other = graph.context(load(self.repo), ["src/b.py"], repo=self.repo)
        self.assertFalse(other["blockers"][0].get("resolved"))
        text = cli(self.repo, "context", "--files", "src/a.py").stdout
        self.assertIn("(resolved: later work here succeeded)", text)

    def test_should_leave_out_the_task_being_worked_on(self):
        write_ledger(self.state, [{"id": "mine", "status": "in_progress", "area": "quality", "title": "Mine",
                                   "paths": ["src/a.py"]},
                                  {"id": "old", "status": "done", "area": "quality", "title": "Old", "paths": ["src/a.py"]}])
        graph.sync(self.repo)
        titles = [row["label"] for row in graph.context(load(self.repo), ["src/a.py"], exclude="mine")["past_tasks"]]
        self.assertEqual(titles, ["Old"])
        structured = json.loads(cli(self.repo, "context", "--files", "src/a.py", "--exclude", "mine", "--json").stdout)
        self.assertEqual([row["label"] for row in structured["past_tasks"]], ["Old"])

    def test_should_flag_knowledge_whose_file_changed_and_clear_it_when_proven_again(self):
        note = self.note("The parser in this file must stay single pass", "convention", about=["file:src/a.py"])
        self.assertNotIn("code_changed", graph.context(load(self.repo), ["src/a.py"], repo=self.repo)["insights"][0])
        (self.repo / "src/a.py").write_text("rewritten\n")
        flagged = graph.context(load(self.repo), ["src/a.py"], repo=self.repo)["insights"][0]
        self.assertEqual(flagged["code_changed"], ["src/a.py"])
        self.assertIn("code changed since: re-verify", cli(self.repo, "context", "--files", "src/a.py").stdout)
        write_ledger(self.state, [{"id": "t1", "status": "done", "area": "quality", "title": "proof", "paths": ["src/a.py"],
                                   "graph_refs": [note], "completed_at": "2026-11-09T00:00:00Z"}])
        graph.sync(self.repo)
        self.assertNotIn("code_changed", graph.context(load(self.repo), ["src/a.py"], repo=self.repo)["insights"][0])

    def test_should_keep_retrieved_knowledge_from_going_dormant(self):
        read = self.note("Knowledge that the agent keeps reading before tasks")
        unread = self.note("Knowledge that nothing ever reads again here")
        for _ in range(dreaming.DORMANT_TICKS + 1):
            journal.record_reads(self.repo, [read], ["src/a.py"])
            graph.sync(self.repo, dream_after=True)
            self.clear_read_rows()
        nodes = load(self.repo)["nodes"]
        self.assertFalse(nodes[read].get("archived"), "read and followed: not dormant")
        self.assertGreaterEqual(nodes[read]["retrieved"], dreaming.DORMANT_TICKS)
        self.assertTrue(nodes[unread].get("dormant"))

    def clear_read_rows(self):
        # Each read row is applied once (by time); a later row in the same second must still count.
        with graph.locked(self.repo) as value:
            value["reads_applied_at"] = 0
        (self.state / journal.READS).write_text("")

    def test_cli_reads_are_recorded(self):
        note = self.note("Recorded reads include this note about the parser", about=["file:src/a.py"])
        cli(self.repo, "context", "--files", "src/a.py")
        rows = journal.read_lines(self.state / journal.READS)
        self.assertIn(note, rows[-1]["ids"])
        self.assertIn("src/a.py", rows[-1]["files"])


class PathTests(AnalysisCase):
    def test_should_route_through_work_not_through_the_run_hub(self):
        write_ledger(self.state, [{"id": "t1", "status": "done", "area": "quality", "title": "one", "paths": ["src/a.py"]},
                                  {"id": "t2", "status": "done", "area": "quality", "title": "two", "paths": ["src/b.py"]},
                                  {"id": "t3", "status": "done", "area": "quality", "title": "both",
                                   "paths": ["src/b.py", "src/c.py"]}])
        graph.sync(self.repo)
        value = load(self.repo)
        steps = graph.shortest_path(value, "file:src/a.py", "file:src/b.py")
        self.assertEqual(steps, [], "only a shared run or area connects them, which says nothing")
        hub = graph.shortest_path(value, "file:src/a.py", "file:src/b.py", include_hubs=True)
        self.assertIn(hub[1]["rel"], graph.HUB_RELATIONS)
        steps = graph.shortest_path(value, "file:src/b.py", "file:src/c.py")
        self.assertEqual([step["rel"] for step in steps], ["touches", "touches"])
        self.assertTrue(all(step["tier"] == "observed" for step in steps))
        self.assertEqual(sum(step["weakest"] for step in steps), 1)
        only = graph.shortest_path(value, "file:src/b.py", "file:src/c.py", via=["co_changed"])
        self.assertTrue(all(step["rel"] == "co_changed" for step in only))

    def test_should_name_the_weakest_inferred_step(self):
        value = graph.GraphState(graph.empty_graph())
        for node_id in ("file:a", "file:b", "insight:i"):
            graph.upsert(value, node_id, node_id.split(":")[0], node_id)
        graph.link(value, "insight:i", "about", "file:a")
        graph.link(value, "insight:i", "applies_to", "file:b", inferred=True, confidence=0.55, rule="cited-on-file")
        steps = graph.shortest_path(value, "file:a", "file:b")
        weakest = next(step for step in steps if step["weakest"])
        self.assertEqual((weakest["tier"], weakest["confidence"]), ("inferred", 0.55))


class DreamAnalysisTests(AnalysisCase):
    def test_should_infer_contradictions_and_ask_about_them(self):
        left = self.note("Always run the payment migrations before the seed data loads in staging", "technique")
        right = self.note("Never run the payment migrations before the seed data loads in staging", "technique")
        with graph.locked(self.repo) as value:
            dreaming.dream(value, self.repo)
        value = load(self.repo)
        key = graph.edge_key(*sorted((left, right))[:1], "contradicts", sorted((left, right))[1])
        self.assertIn(key, value["edges"])
        self.assertEqual(value["edges"][key]["rule"], "opposite-wording")
        row = graph.context(value, text="payment migrations seed", repo=self.repo)["insights"][0]
        self.assertTrue(row["contradicted_by"])
        kinds = [question["kind"] for question in analysis.suggested_questions(value, self.repo)]
        self.assertIn("contradiction", kinds)
        self.assertIn("contradict each other", graph.brief(value, "G", "R"))
        # A user can reject it like any inferred link.
        with graph.locked(self.repo) as value:
            dreaming.reject_link(value, left, "contradicts", right)
            dreaming.dream(value, self.repo)
        self.assertNotIn(key, load(self.repo)["edges"])

    def test_should_propose_only_cliques(self):
        value = graph.GraphState(graph.empty_graph())
        chain = [f"insight:{letter}" for letter in "abcd"]
        for node_id in chain:
            graph.upsert(value, node_id, "insight", node_id, kind="fact", summary=node_id, source="agent")
        for left, right in zip(chain, chain[1:]):
            graph.link(value, left, "similar_to", right, inferred=True, confidence=0.9)
        report = {}
        dreaming.propose(value, report)
        for proposal in value["dream"]["proposals"]:
            members = proposal["members"]
            for index, left in enumerate(members):
                for right in members[index + 1:]:
                    self.assertTrue(any(edge["rel"] == "similar_to" and {edge["src"], edge["dst"]} == {left, right}
                                        for edge in value["edges"].values()), "every member links every other")
        graph.link(value, "insight:a", "similar_to", "insight:c", inferred=True, confidence=0.9)
        dreaming.propose(value, report)
        self.assertIn(["insight:a", "insight:b", "insight:c"], [row["members"] for row in value["dream"]["proposals"]])

    def test_should_group_files_deterministically_with_stable_ids(self):
        rows = []
        for index in range(4):
            rows.append({"id": f"s{index}", "status": "done", "area": "quality", "title": "s",
                         "paths": ["src/a.py", "src/b.py", "src/c.py"]})
            rows.append({"id": f"l{index}", "status": "done", "area": "quality", "title": "l", "paths": ["lib/x.py", "lib/y.py"]})
        rows.append({"id": "bridge", "status": "done", "area": "quality", "title": "bridge", "paths": ["src/c.py", "lib/x.py"]})
        write_ledger(self.state, rows)
        graph.sync(self.repo, dream_after=True)
        first = load(self.repo)["communities"]["groups"]
        self.assertEqual(sorted(sorted(group["members"]) for group in first),
                         [["file:lib/x.py", "file:lib/y.py"], ["file:src/a.py", "file:src/b.py", "file:src/c.py"]])
        self.assertTrue(all(0 < group["cohesion"] <= 1 for group in first))
        with graph.locked(self.repo) as value:
            dreaming.dream(value, self.repo)
        second = load(self.repo)["communities"]["groups"]
        self.assertEqual({group["id"] for group in first}, {group["id"] for group in second})
        self.assertEqual(analysis.communities(load(self.repo)), analysis.communities(load(self.repo)))
        groups = json.loads(cli(self.repo, "communities", "--format", "json").stdout)
        self.assertEqual(len(groups), 2)
        context = graph.context(load(self.repo), ["src/a.py"], repo=self.repo)
        self.assertEqual(context["groups"][0]["size"], 3)
        surprises = analysis.surprises(load(self.repo))
        self.assertTrue(any("crosses lib/ and src/" in row["why"] and "observed" in row["why"] for row in surprises))
        bridges = analysis.bridges(load(self.repo))
        self.assertIn(bridges[0]["file"], ("src/c.py", "lib/x.py"))


class ReportAndExportTests(AnalysisCase):
    def setUp(self):
        super().setUp()
        self.note("The parser must stay single pass", "convention", about=["file:src/a.py"])
        write_ledger(self.state, [{"id": f"t{index}", "status": "rejected" if index % 2 else "done", "area": "quality",
                                   "title": f"t{index}", "paths": ["src/a.py", "src/b.py"]} for index in range(6)])
        graph.sync(self.repo, dream_after=True)

    def test_report_has_every_section_with_content(self):
        result = cli(self.repo, "report")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = result.stdout
        self.assertTrue(text.startswith("# Knowledge graph report"))
        for heading in ("## Files that resist change", "## Most connected files", "Links: recorded"):
            self.assertIn(heading, text)
        out = Path(self.temp.name) / "report.md"
        self.assertEqual(cli(self.repo, "report", "--out", str(out)).returncode, 0)
        self.assertEqual(out.read_text(), text)

    def test_export_graphml_and_json_are_complete_and_parse(self):
        value = load(self.repo)
        root = ElementTree.fromstring(cli(self.repo, "export", "--as", "graphml").stdout)
        namespace = "{http://graphml.graphdrawing.org/xmlns}"
        self.assertEqual(len(root.findall(f"{namespace}graph/{namespace}node")), len(value["nodes"]))
        self.assertEqual(len(root.findall(f"{namespace}graph/{namespace}edge")), len(value["edges"]))
        exported = json.loads(cli(self.repo, "export", "--as", "json").stdout)
        self.assertEqual(len(exported["nodes"]), len(value["nodes"]))
        self.assertIn("# Knowledge graph report", cli(self.repo, "export", "--as", "markdown").stdout)

    def test_questions_interleave_kinds(self):
        result = json.loads(cli(self.repo, "questions", "--format", "json").stdout)
        self.assertIsInstance(result, list)

    def test_node_links_carry_tiers(self):
        detail = toon.loads(cli(self.repo, "node", "file:src/a.py").stdout)
        tiers = {row["rel"]: row["tier"] for row in detail["links"]}
        self.assertEqual(tiers.get("touches"), "observed")
        self.assertEqual(tiers.get("about"), "recorded")
        shares = analysis.tiers(load(self.repo))
        self.assertAlmostEqual(sum(row["share"] for row in shares.values()), 1.0, places=2)


if __name__ == "__main__":
    unittest.main()
