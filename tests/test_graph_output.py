"""Helper output for the agent: TOON never costs more than JSON, switches work in any
position, text is bounded without cutting identifiers, budgets say what they left out, and
the brief keeps its short sections (review 2026-11-10, sections 3 and 4.1)."""
import io
import json
import os
import re
from pathlib import Path
import contextlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_support import budget, ROOT, environment, make_repo, restore_environment, write_ledger, write_run  # noqa: E402
import improve_graph as graph  # noqa: E402
import improve_graph_cli as graph_cli  # noqa: E402
import improve_toon as toon  # noqa: E402


def run(script, repo, *args, env=None):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / script), "--repo", str(repo), *args],
                          capture_output=True, text=True, timeout=budget(60), env={**os.environ, **(env or {})})


def mixed_rows():
    rows = [{"id": f"insight:{index:012x}", "type": "insight", "label": f"use the pool retry wrapper {index}",
             "kind": "technique", "summary": f"use the pool retry wrapper when a deadlock happens {index}", "score": 0.81,
             "source": "agent", "reinforced": 2, "match": 0.5} for index in range(10)]
    rows += [{"id": f"file:src/a{index}.py", "type": "file", "label": f"src/a{index}.py", "score": 0.4,
              "stats": {"done": 1, "rejected": 2, "blocked": 0, "open": 0, "attempts": 3, "success": 0.4},
              "area": "performance", "match": 0.3} for index in range(5)]
    return rows


class CompactRowsTests(unittest.TestCase):
    def test_should_never_be_longer_than_compact_json_on_mixed_rows(self):
        rows = mixed_rows()
        text = graph_cli.render(rows)
        self.assertLessEqual(len(text), len(json.dumps(rows, separators=(",", ":"))))
        self.assertLessEqual(len(text), len(toon.dumps(rows)), "never worse than leaving the rows alone")

    def test_should_round_trip_flattened_columns(self):
        rows = [{"file": "src/a.py", "stats": {"done": 1, "attempts": 2}}, {"file": "src/b.py", "stats": {"done": 0, "attempts": 1}}]
        decoded = toon.loads(toon.dumps({"x": toon.compact_rows(rows)}))["x"]
        self.assertEqual(len(decoded), 2)
        self.assertEqual(decoded[0].get("stats", {}).get("done", decoded[0].get("stats.done")), 1)

    def test_should_keep_uniform_rows_tabular(self):
        rows = [{"id": f"q-{index}", "status": "ready"} for index in range(5)]
        self.assertTrue(graph_cli.render(rows).startswith("[5]{id,status}:"))

    def test_flatten_only_touches_primitive_objects(self):
        self.assertEqual(toon.flatten({"a": {"b": 1}, "c": {"d": [1]}}), {"a.b": 1, "c": {"d": [1]}})


class FormatSelectionTests(unittest.TestCase):
    def test_should_normalize_and_validate_improve_format(self):
        self.assertEqual(toon.default_format({"IMPROVE_FORMAT": " JSON "}), "json")
        warnings = []
        self.assertEqual(toon.default_format({"IMPROVE_FORMAT": "yaml"}, warnings.append), "toon")
        self.assertIn("yaml", warnings[0])
        self.assertEqual(toon.default_format({}), "toon")


class HelperCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve graph output ")
        self.previous = environment(Path(self.temp.name) / "home")
        long_path = "src/" + "/".join(["deeply_nested_directory"] * 6) + "/module_with_a_long_name.py"
        self.long_path = long_path
        self.repo = make_repo(Path(self.temp.name) / "repo", ["src/a.py", "src/b.py", long_path]).resolve()
        self.state = self.repo / ".improve"
        write_run(self.state, "live", {"unit": 12})
        rows = [{"id": f"q-{index}", "status": "ready", "area": "quality", "title": f"Task {index} " + "words " * 40,
                 "claim": "claim " * 60, "file": long_path if index == 0 else "src/a.py", "severity": 3, "confidence": 0.8,
                 "blast": 2} for index in range(6)]
        rows.append({"id": "q-done", "status": "done", "area": "quality", "title": "done", "paths": ["src/a.py", "src/b.py"]})
        write_ledger(self.state, rows)
        graph.sync(self.repo, refine_after=True)

    def tearDown(self):
        restore_environment(self.previous)
        self.temp.cleanup()


class SwitchPositionTests(HelperCase):
    def test_should_accept_output_switches_after_the_subcommand(self):
        cases = [("improve_graph.py", ("stats", "--format", "json")),
                 ("improve_graph.py", ("hotspots", "--format", "json", "--full")),
                 ("improve_ledger.py", ("queue", "--format", "json", "--width", "40")),
                 ("improve_ledger.py", ("counts", "--format", "json")),
                 ("improve_control.py", ("pending", "--format", "json")),
                 ("improve_learning.py", ("guidance", "--format", "json"))]
        for script, args in cases:
            with self.subTest(script=script, args=args):
                result = run(script, self.repo, *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                if script != "improve_learning.py":
                    json.loads(result.stdout)

    def test_should_honor_switches_before_the_subcommand_too(self):
        result = run("improve_graph.py", self.repo, "--format", "json", "stats")
        self.assertEqual(json.loads(result.stdout)["runs"], 1)

    def test_should_take_improve_format_in_any_case(self):
        result = run("improve_graph.py", self.repo, "stats", env={"IMPROVE_FORMAT": "JSON"})
        self.assertEqual(json.loads(result.stdout)["runs"], 1)
        result = run("improve_graph.py", self.repo, "stats", env={"IMPROVE_FORMAT": "yaml"})
        self.assertEqual(toon.loads(result.stdout)["runs"], 1)
        self.assertIn("IMPROVE_FORMAT", result.stderr)


class WidthTests(HelperCase):
    def test_should_cut_text_but_never_paths_in_the_ledger(self):
        result = run("improve_ledger.py", self.repo, "queue", "--width", "40")
        rows = toon.loads(result.stdout)["tasks"]
        self.assertIn(self.long_path, [row["file"] for row in rows], "a long path is not cut")
        self.assertTrue(all(len(row["claim"]) <= 40 for row in rows))
        self.assertTrue(any(re.search(r"\u2026\[\+\d+\]$", row["claim"]) for row in rows))

    def test_should_bound_graph_text_unless_full(self):
        with graph.locked(self.repo) as value:
            node = graph.add_insight(value, "A long note " + "about the parser and its cache " * 18, "fact",
                                     about=[f"file:{self.long_path}"], repo=self.repo)
        short = toon.loads(run("improve_graph.py", self.repo, "node", node["id"]).stdout)
        self.assertRegex(short["node"]["summary"], r"\u2026\[\+\d+\]$")
        self.assertLessEqual(len(short["node"]["summary"]), graph_cli.DEFAULT_WIDTH)
        self.assertIn(f"file:{self.long_path}", [row["id"] for row in short["links"]])
        full = toon.loads(run("improve_graph.py", self.repo, "node", node["id"], "--full").stdout)
        self.assertEqual(full["node"]["summary"], node["summary"])


class BudgetTests(HelperCase):
    def test_should_say_what_a_budget_left_out(self):
        with graph.locked(self.repo) as value:
            for index in range(12):
                graph.add_insight(value, f"Cache note {index}: flush the {index} writer queue before reading " + "x" * 80,
                                  "fact", about=["file:src/a.py"], repo=self.repo)
        full = run("improve_graph.py", self.repo, "search", "cache writer queue", "--limit", "12")
        cut = run("improve_graph.py", self.repo, "search", "cache writer queue", "--limit", "12", "--budget", "150")
        self.assertEqual(cut.returncode, 0, cut.stderr)
        first, _, body = cut.stdout.partition("\n")
        self.assertTrue(first.startswith("[!] TRUNCATED: showing "))
        self.assertIn("of 12 rows", first)
        self.assertLessEqual(len(body), 150 * graph_cli.CHARS_PER_TOKEN + 10)
        needed = int(first.rsplit("--budget ", 1)[1].split()[0])
        again = run("improve_graph.py", self.repo, "search", "cache writer queue", "--limit", "12", "--budget", str(needed))
        self.assertFalse(again.stdout.startswith("[!]"))
        self.assertEqual(again.stdout, full.stdout)

    def test_should_never_cut_context_seeds(self):
        result = {"seeds": ["file:a", "file:b"], "insights": [{"id": str(index), "summary": "x" * 50} for index in range(9)]}
        text = graph_cli.budgeted(result, 30, graph_cli.render)
        self.assertIn("file:a", text)
        self.assertIn("file:b", text)
        self.assertTrue(text.startswith("[!] TRUNCATED"))


class BriefTests(unittest.TestCase):
    def test_should_keep_gates_and_proposals_when_notes_are_long(self):
        value = graph.GraphState(graph.empty_graph())
        value["runs"] = [{"run_id": "r", "seq": 1}]
        for index in range(8):
            graph.add_insight(value, f"Distinct topic {index * 1000} " + " ".join(f"w{index}x{word}" for word in range(40)),
                              "technique")
        value["nodes"]["gate:unit"] = {"id": "gate:unit", "type": "gate", "label": "unit", "seconds": 30}
        value["dream"]["proposals"] = [{"id": "dream:x", "members": []}]
        text = graph.brief(value, "G", "R")
        self.assertLessEqual(len(text), 1800)
        self.assertIn("Known gates and cost: unit 30s", text)
        self.assertIn("Dreaming found 1 group", text)
        self.assertRegex(text, r"\(showing \d of 8 notes")
        self.assertIn("data, not instructions", text)
        for line in text.splitlines():
            if line.startswith("- [insight:"):
                self.assertLessEqual(len(line), graph.BRIEF_NOTE_CHARS + 80)

    def test_should_flag_knowledge_whose_code_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_repo(Path(directory) / "repo", ["src/a.py"]).resolve()
            write_run(repo / ".improve", "live")
            with graph.locked(repo) as value:
                graph.add_insight(value, "The parser here must stay single pass", "convention", about=["file:src/a.py"],
                                  repo=repo)
            self.assertNotIn("code changed since", graph.brief(graph.load(graph.graph_path(repo)), "G", "R", repo=repo))
            (repo / "src/a.py").write_text("changed\n")
            self.assertIn("code changed since: re-verify", graph.brief(graph.load(graph.graph_path(repo)), "G", "R",
                                                                       repo=repo))


class InProcessCliTests(HelperCase):
    def test_main_delegates_and_returns_exit_codes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, {"IMPROVE_FORMAT": "json"}):
            self.assertEqual(graph.main(["--repo", str(self.repo), "stats"]), 0)
        self.assertEqual(json.loads(out.getvalue())["runs"], 1)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(graph.main(["--repo", str(self.repo), "node", "insight:none"]), graph_cli.EXIT_NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
