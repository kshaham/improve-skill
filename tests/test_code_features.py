"""Code-layer features from the second Graphify pass (review 2026-11-12, sections 3.5, 3.6, 6):
change-set impact, test selection, code search and dotted lookup, the last-good-parse and
shrink guards, bounded cycle search, code-only and split module groups, schema upgrades, and
their command lines."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_support import ROOT, budget, load  # noqa: E402
from test_code_resolution import CodeRepoCase  # noqa: E402
import improve_analysis as analysis  # noqa: E402
import improve_code as code  # noqa: E402
import improve_graph as graph  # noqa: E402
import improve_graph_cli as graph_cli  # noqa: E402
import improve_toon as toon  # noqa: E402

PROJECT = {
    "app/__init__.py": "",
    "app/core.py": '''
        """Core helpers."""


        def helper(value):
            """Normalise a value."""
            return value


        def unrelated():
            return 2
        ''',
    "app/service.py": '''
        from app.core import helper


        def handle(value):
            return helper(value)
        ''',
    "app/other.py": '''
        from app.core import unrelated


        def other():
            return unrelated()
        ''',
    "app/upload.py": '''
        class UploadManager:
            """Uploads files with retries."""

            def retry_upload(self, name):
                # WHY: the storage service drops one request in a thousand
                return name

            def cancel(self):
                return None
        ''',
    "tests/conftest.py": '''
        import pytest
        from app.service import handle


        @pytest.fixture
        def handled():
            return handle(1)
        ''',
    "tests/test_service.py": '''
        from app.service import handle


        def test_handle():
            assert handle(1) == 1
        ''',
    "tests/test_fixture.py": '''
        def test_with_fixture(handled):
            assert handled == 1
        ''',
    "tests/test_other.py": '''
        from app.other import other


        def test_other():
            assert other() == 2
        ''',
    "tests/test_upload.py": '''
        from app.upload import UploadManager


        def test_retry_upload():
            assert UploadManager().retry_upload("x") == "x"
        ''',
}


def cli(repo, *args):
    return subprocess.run([sys.executable, str(ROOT / "scripts/improve_graph.py"), "--repo", str(repo), *args],
                          capture_output=True, text=True, timeout=60)


class ChangeSetTests(CodeRepoCase):
    def setUp(self):
        super().setUp()
        self.repo, self.con = self.build(PROJECT)

    def edit(self, path, old, new):
        target = self.repo / path
        target.write_text(target.read_text().replace(old, new))

    def test_should_map_an_edited_function_to_its_callers(self):
        self.edit("app/core.py", "    return value\n", "    return value or 0\n")
        result = code.changed_impact(self.con, self.repo)
        self.assertEqual(result["changed"], [{"file": "app/core.py", "symbols": ["app/core.py::helper"]}])
        files = {row["file"] for row in result["dependents"]}
        self.assertIn("app/service.py", files)
        self.assertNotIn("app/other.py", files, "a caller of another function in the same file is not affected")
        self.assertIn("tests/test_service.py", result["tests"])
        self.assertNotIn("tests/test_other.py", result["tests"])

    def test_should_count_module_level_changes_and_new_files_as_whole_files(self):
        self.edit("app/core.py", '"""Core helpers."""', '"""Core helpers."""\nLIMIT = 3')
        (self.repo / "app/new.py").write_text("from app.other import other\n")
        changed = {row["file"]: row for row in code.changed_impact(self.con, self.repo)["changed"]}
        self.assertTrue(changed["app/core.py"]["whole_file"])
        self.assertTrue(changed["app/new.py"]["whole_file"], "untracked source files are part of the change")

    def test_should_ignore_blank_lines_and_follow_a_deleted_function(self):
        self.edit("app/core.py", "\n\ndef unrelated():\n    return 2\n", "\n\n\n")
        result = code.changed_impact(self.con, self.repo)
        self.assertEqual(result["changed"][0]["symbols"], ["app/core.py::unrelated"])
        self.assertIn("app/other.py", {row["file"] for row in result["dependents"]})

    def test_cli_impact_changed(self):
        graph.sync(self.repo, code_index=True, dream_after=True)  # The dream groups the code-linked files.
        self.edit("app/core.py", "    return value\n", "    return value or 0\n")
        result = json.loads(cli(self.repo, "impact", "--changed", "--format", "json").stdout)
        self.assertEqual(result["rev"], "HEAD")
        self.assertIn("app/service.py", [row["file"] for row in result["dependents"]])
        self.assertTrue(result["groups"], "the module groups the change touches")
        self.assertEqual(cli(self.repo, "impact", "app/core.py", "--changed").returncode, 2)
        self.assertEqual(cli(self.repo, "impact", "--changed", "--evil").returncode, 2)


class TestSelectionTests(CodeRepoCase):
    def setUp(self):
        super().setUp()
        self.repo, self.con = self.build(PROJECT)

    def test_should_list_tests_that_reach_a_helper_and_no_others(self):
        seeds, origins = code.start_seeds(self.con, "app/core.py::helper")
        rows, _ = code.tests_for(self.con, self.repo, seeds, origins)
        files = [row["file"] for row in rows]
        self.assertEqual(set(files), {"tests/test_service.py", "tests/conftest.py", "tests/test_fixture.py"})
        self.assertEqual(files[-1], "tests/test_fixture.py", "the fixture user is furthest away")
        by_file = {row["file"]: row for row in rows}
        self.assertEqual(by_file["tests/test_service.py"]["run"], "python -m pytest tests/test_service.py::test_handle")
        self.assertEqual(by_file["tests/test_fixture.py"]["tests"], ["tests/test_fixture.py::test_with_fixture"])

    def test_unittest_hint_without_pytest(self):
        self.assertEqual(code.run_hint("tests/test_x.py", ["tests/test_x.py::Case.test_one"], {"python": "unittest"}),
                         "python -m unittest discover -s tests -p test_x.py -k test_one")

    def test_cli_tests_for(self):
        self.assertEqual(cli(self.repo, "code", "index").returncode, 0)
        result = json.loads(cli(self.repo, "tests-for", "app/other.py", "--format", "json").stdout)
        self.assertEqual([row["file"] for row in result["tests"]], ["tests/test_other.py"])
        self.assertIn("never replace", result["note"])
        empty = json.loads(cli(self.repo, "tests-for", "app/upload.py::UploadManager.cancel", "--format", "json").stdout)
        self.assertEqual(empty["tests"], [])
        self.assertIn("run the whole suite", empty["note"])


class SearchTests(CodeRepoCase):
    def setUp(self):
        super().setUp()
        self.repo, self.con = self.build(PROJECT)

    def test_should_find_by_identifier_words(self):
        rows, _ = code.search(self.con, "retry upload manager")
        self.assertEqual(rows[0]["id"], "app/upload.py::UploadManager.retry_upload")

    def test_should_demote_tests_unless_asked(self):
        plain = [row["id"] for row in code.search(self.con, "retry upload")[0]]
        self.assertLess(plain.index("app/upload.py::UploadManager.retry_upload"),
                        plain.index("tests/test_upload.py::test_retry_upload"))
        asked = [row["id"] for row in code.search(self.con, "test retry upload")[0]]
        self.assertEqual(asked[0], "tests/test_upload.py::test_retry_upload")

    def test_should_search_docstrings_and_code_notes(self):
        ids = [row["id"] for row in code.search(self.con, "storage drops request")[0]]
        self.assertEqual(ids[0], "app/upload.py:5")
        self.assertIn("app/core.py::helper", [row["id"] for row in code.search(self.con, "normalise")[0]])

    def test_should_suggest_near_misses(self):
        rows, suggestions = code.search(self.con, "uplod")
        self.assertEqual(rows, [])
        self.assertIn("upload", suggestions)

    def test_should_find_a_dotted_module_path(self):
        found = code.find(self.con, "app.upload.UploadManager.retry_upload")
        self.assertEqual([row["id"] for row in found], ["app/upload.py::UploadManager.retry_upload"])

    def test_cli_search_and_find(self):
        self.assertEqual(cli(self.repo, "code", "index").returncode, 0)
        found = toon.loads(cli(self.repo, "code", "search", "retry", "upload").stdout)
        self.assertEqual(found["results"][0]["id"], "app/upload.py::UploadManager.retry_upload")
        missing = cli(self.repo, "code", "search", "uplod")
        self.assertEqual(missing.returncode, 5)
        self.assertIn("did you mean", missing.stderr)
        dotted = toon.loads(cli(self.repo, "code", "find", "app.core.helper").stdout)
        self.assertEqual(dotted["id"], "app/core.py::helper")


class GuardTests(CodeRepoCase):
    def test_should_refuse_to_prune_when_the_listing_shrinks_suddenly(self):
        sources = {f"pkg/m{index}.py": f"def f{index}():\n    return {index}\n" for index in range(12)}
        repo, con = self.build(sources)
        self.git(repo, "rm", "-q", "--cached", *[f"pkg/m{index}.py" for index in range(9)])
        report = code.index(con, repo)
        self.assertIn("HEAD is unchanged", report["shrink_refused"])
        self.assertEqual(code.stats(con)["files"], 12)
        forced = code.index(con, repo, force=True)
        self.assertEqual(forced["removed"], 9)

    def test_cli_symbols_reports_a_stale_parse(self):
        repo, con = self.build(PROJECT)
        self.assertEqual(cli(repo, "code", "index").returncode, 0)
        (repo / "app/core.py").write_text((repo / "app/core.py").read_text() + "\ndef broken(:\n")
        self.assertEqual(cli(repo, "code", "index").returncode, 0)
        symbols = json.loads(cli(repo, "code", "symbols", "app/core.py", "--format", "json").stdout)
        self.assertTrue(symbols["stale_parse"])
        self.assertIn("app/core.py::helper", [row["id"] for row in symbols["symbols"]])
        impact = json.loads(cli(repo, "impact", "app/core.py::helper", "--format", "json").stdout)
        self.assertIn("last good parse", impact["warnings"][0])
        self.assertIn("app/service.py", [row["file"] for row in impact["dependents"]])


class CycleTests(unittest.TestCase):
    def test_dense_import_graph_answers_quickly_and_says_at_least(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        code.ensure_schema(con)
        rows = [(f"file:m{i:02}.py", "imports", f"file:m{j:02}.py", f"m{i:02}.py", f"m{j:02}.py", 1, 1.0, "observed", 0)
                for i in range(30) for j in range(30) if i != j]
        con.executemany("INSERT INTO code_edge(src, rel, dst, src_path, dst_path, line, confidence, tier, deferred) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", rows)
        started = time.monotonic()
        found = code.find_cycles(con, 5, limit=50)
        self.assertLess(time.monotonic() - started, budget(1.0))
        self.assertFalse(found["complete"])
        self.assertEqual(found["found"], 50)
        self.assertTrue(all(row["length"] == 2 for row in found["cycles"]), "shortest first")
        bounded = code.find_cycles(con, 5, limit=10 ** 9, work=5000)
        self.assertFalse(bounded["complete"], "the work budget stops it too")


class ReportTests(CodeRepoCase):
    def test_report_shows_coverage_and_bounded_cycles(self):
        repo, _ = self.build({
            "a.py": "import b\n", "b.py": "import a\n", "c.py": "import d\n", "d.py": "import c\n",
            "tool.rs": "fn main() {}\n",
        })
        graph.sync(repo, code_index=True)
        value = graph_cli.with_code(load(repo), repo)
        try:
            text = analysis.report(value, repo)
        finally:
            value.store.close()
        self.assertIn("Code coverage: indexed: python 4/4; not indexed (unsupported): rust 1.", text)
        self.assertIn("## Import cycles", text)
        limited = json.loads(cli(repo, "cycles", "--limit", "1", "--format", "json").stdout)
        self.assertEqual(len(limited["cycles"]), 1)
        self.assertIn("at least 1", limited["note"])
        stats = json.loads(cli(repo, "code", "stats", "--format", "json").stdout)
        self.assertIn("rust 1", stats["coverage_line"])

    def test_long_test_lists_fall_back_to_the_file(self):
        tests = [f"tests/test_long_module_name.py::TestSomethingLong.test_case_number_{index}" for index in range(5)]
        self.assertEqual(code.run_hint("tests/test_long_module_name.py", tests, {"python": "pytest"}),
                         "python -m pytest tests/test_long_module_name.py")


class CommunityTests(CodeRepoCase):
    def test_should_group_files_the_work_has_never_touched(self):
        repo, _ = self.build({
            "lib/a.py": "from lib.b import f\nfrom lib.c import g\ndef run():\n    return f() + g()\n",
            "lib/b.py": "from lib.c import g\ndef f():\n    return g()\n",
            "lib/c.py": "def g():\n    return 1\n",
            "lib/__init__.py": "",
        })
        graph.sync(repo, code_index=True)
        value = graph_cli.with_code(load(repo), repo)
        try:
            groups = analysis.communities(value)
        finally:
            value.store.close()
        self.assertEqual(groups[0]["members"], ["file:lib/a.py", "file:lib/b.py", "file:lib/c.py"])
        self.assertTrue(groups[0]["label"].startswith("lib/"))

    def test_should_split_a_group_a_hub_glues_together(self):
        nodes, edges = {}, {}
        names = [f"a{index}" for index in range(8)] + [f"b{index}" for index in range(8)] + ["hub"]
        for name in names:
            nodes["file:" + name] = {"id": "file:" + name, "type": "file", "label": name}

        def add(left, right, weight):
            edges[len(edges)] = {"src": "file:" + left, "dst": "file:" + right, "rel": "co_changed", "weight": weight}

        for prefix in "ab":
            for left in range(8):
                for right in range(left + 1, 8):
                    if (left + right) % 3 == 0:
                        add(f"{prefix}{left}", f"{prefix}{right}", 1)
        for name in names[:-1]:
            add("hub", name, 4)
        value = {"nodes": nodes, "edges": edges}
        adjacent = analysis.neighbours(analysis.file_weights(value))
        self.assertEqual(len(analysis.grouped(analysis.propagate(adjacent, analysis.hubs(adjacent)))), 1,
                         "propagation alone makes one giant group")
        groups = analysis.communities(value)
        self.assertGreater(len(groups), 1)
        self.assertTrue(all(group["size"] < len(names) for group in groups))
        self.assertEqual(groups, analysis.communities(value), "deterministic")


class SchemaTests(CodeRepoCase):
    def test_should_rebuild_tables_written_by_an_older_version(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.execute("CREATE TABLE code_file(path TEXT PRIMARY KEY, sha TEXT, size INTEGER, mtime_ns INTEGER, "
                    "indexed_ns INTEGER, lang TEXT, module TEXT, version INTEGER, error TEXT)")
        con.execute("INSERT INTO code_file(path, version) VALUES ('old.py', 2)")
        code.ensure_schema(con)
        self.assertEqual(con.execute("SELECT count(*) FROM code_file").fetchone()[0], 0)
        self.assertIn("stale_parse", [row[1] for row in con.execute("PRAGMA table_info(code_file)")])
        self.assertGreaterEqual(code.CODE_VERSION, 3)

    def test_stats_report_relations_tiers_and_head(self):
        repo, con = self.build(PROJECT)
        stats = code.stats(con)
        self.assertEqual(stats["relations"]["imports"], {"observed": stats["relations"]["imports"]["observed"]})
        self.assertIn("uses", stats["relations"])
        self.assertEqual(stats["head"], self.git(repo, "rev-parse", "HEAD").stdout.strip())
        self.assertFalse(code.freshness(con, repo)["stale"])
        (repo / "x.py").write_text("")
        self.git(repo, "add", "x.py")
        self.git(repo, "commit", "-qm", "more")
        self.assertTrue(code.freshness(con, repo)["stale"])


if __name__ == "__main__":
    unittest.main()
