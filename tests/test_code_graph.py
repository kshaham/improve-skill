"""The code layer: symbols, imports, calls that never guess, code notes with secrets removed,
incremental indexing, impact, cycles, and its place in `context` and `sync`."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_support import GIT_IDENTITY, ROOT, environment, make_repo, restore_environment, write_run  # noqa: E402
import improve_code as code  # noqa: E402
import improve_graph as graph  # noqa: E402
import improve_toon as toon  # noqa: E402

SOURCES = {
    "pkg/__init__.py": "",
    "pkg/core.py": '''
        """Core engine."""


        def helper():
            """Help everyone."""
            return 1


        class Engine:
            def run(self):
                # WHY: one pass keeps the parser linear on huge inputs
                return self.step()

            def step(self):
                # NOTE: the staging token is sk-abcdefghijklmnopqrstuvwxyz123456, never log it
                return helper()
        ''',
    "pkg/util.py": '''
        def fmt(value):
            return str(value)


        def unique_name():
            return 2
        ''',
    "pkg/api.py": '''
        from .core import Engine, helper
        import pkg.util as util
        import pkg.lazy


        def handler():
            helper()
            Engine().run()
            return util.fmt(1)
        ''',
    "pkg/lazy.py": '''
        def later():
            import pkg.api
            return pkg.api.handler()
        ''',
    "pkg/cyc_a.py": "import pkg.cyc_b\n",
    "pkg/cyc_b.py": "from pkg import cyc_a\n",
    "pkg/one.py": "def dup():\n    return 1\n",
    "pkg/two.py": "def dup():\n    return 2\n",
    "pkg/caller.py": "def go():\n    return dup()\n",
    "pkg/broken.py": "def broken(:\n",
    "tests/test_core.py": '''
        from pkg.core import helper


        def test_helper():
            assert helper() == 1
            assert unique_name() == 2
        ''',
    "web/app.ts": "import { x } from './lib';\nimport './styles';\nexport function main() { return x(); }\n",
    "web/lib.ts": "// WHY: keep this pure so the worker can import it\nexport function x() { return 1; }\n",
    "web/styles.css": "body {}\n",
}


class CodeCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve code graph ")
        self.previous = environment(Path(self.temp.name) / "home")
        self.repo = make_repo(Path(self.temp.name) / "repo", list(SOURCES)).resolve()
        for name, text in SOURCES.items():
            (self.repo / name).write_text(textwrap.dedent(text).lstrip("\n"))
        self.git("add", "-A")
        self.git("commit", "-qm", "sources")
        write_run(self.repo / ".improve", "live", {"test": 3})
        self.con = sqlite3.connect(":memory:")
        self.report = code.index(self.con, self.repo, max_seconds=60)

    def tearDown(self):
        self.con.close()
        restore_environment(self.previous)
        self.temp.cleanup()

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *GIT_IDENTITY, *args], check=True, capture_output=True)

    def edges(self, rel):
        return {(src, dst): (confidence, tier) for src, dst, confidence, tier in self.con.execute(
            "SELECT src, dst, confidence, tier FROM code_edge WHERE rel=?", (rel,))}

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/improve_graph.py"), "--repo", str(self.repo), *args],
                              capture_output=True, text=True, timeout=60)


class ExtractionTests(CodeCase):
    def test_should_index_symbols_with_kinds_lines_and_docstrings(self):
        symbols = {row["id"]: row for row in code.symbols_of(self.con, "pkg/core.py")}
        self.assertEqual(symbols["pkg/core.py::helper"]["kind"], "function")
        self.assertEqual(symbols["pkg/core.py::Engine"]["kind"], "class")
        self.assertEqual(symbols["pkg/core.py::Engine.run"]["kind"], "method")
        self.assertEqual(symbols["pkg/core.py::helper"]["doc"], "Help everyone.")
        self.assertEqual(symbols["pkg/core.py::helper"]["line"], 4)
        script = {row["id"] for row in code.symbols_of(self.con, "web/lib.ts")}
        self.assertIn("web/lib.ts::x", script)

    def test_should_resolve_imports_relative_absolute_and_script(self):
        imports = self.edges("imports")
        for src, dst in (("pkg/api.py", "pkg/core.py"), ("pkg/api.py", "pkg/util.py"), ("tests/test_core.py", "pkg/core.py"),
                         ("pkg/cyc_b.py", "pkg/cyc_a.py"), ("web/app.ts", "web/lib.ts")):
            self.assertIn(("file:" + src, "file:" + dst), imports)
        self.assertFalse(any(dst.endswith("styles") for _, dst in imports), "an unresolved import is not invented")

    def test_should_resolve_calls_without_guessing(self):
        calls = self.edges("calls")
        self.assertEqual(calls[("pkg/api.py::handler", "pkg/core.py::helper")], (1.0, "observed"))
        self.assertEqual(calls[("pkg/api.py::handler", "pkg/core.py::Engine")], (1.0, "observed"))
        self.assertEqual(calls[("pkg/api.py::handler", "pkg/util.py::fmt")], (1.0, "observed"))
        self.assertEqual(calls[("pkg/core.py::Engine.run", "pkg/core.py::Engine.step")], (1.0, "observed"))
        self.assertEqual(calls[("pkg/core.py::Engine.step", "pkg/core.py::helper")], (1.0, "observed"))
        self.assertEqual(calls[("tests/test_core.py::test_helper", "pkg/util.py::unique_name")], (0.85, "inferred"))
        self.assertFalse(any(src == "pkg/caller.py::go" for src, _ in calls), "two `dup` definitions: no guess")

    def test_should_keep_code_notes_and_redact_secrets(self):
        notes = code.notes_for(self.con, ["pkg/core.py", "web/lib.ts"])
        tags = {(row["path"], row["tag"]) for row in notes}
        self.assertIn(("pkg/core.py", "WHY"), tags)
        self.assertIn(("web/lib.ts", "WHY"), tags)
        text = " ".join(row["text"] for row in notes)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", text)
        self.assertIn("[redacted]", text)
        self.assertEqual(notes[0]["tag"], "WHY", "rationale ranks before notes")

    def test_should_survive_a_file_that_does_not_parse(self):
        self.assertEqual(self.report["errors"], 1)
        error = self.con.execute("SELECT error FROM code_file WHERE path='pkg/broken.py'").fetchone()[0]
        self.assertIn("SyntaxError", error)

    def test_redact_patterns(self):
        for secret in ("AKIAABCDEFGHIJKLMNOP", "ghp_" + "a" * 36, "Bearer abcdefghijklmnop.qrs",
                       "https://user:hunter22@example.com/x", "api_key = 1234567890abcdef"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret.split()[-1].split("@")[0][-8:], code.redact(f"token {secret} end"))


class IncrementalTests(CodeCase):
    def test_should_skip_unchanged_files_and_reread_changed_ones(self):
        again = code.index(self.con, self.repo, max_seconds=60)
        self.assertEqual(again["parsed"], 0)
        self.assertEqual(again["unchanged"], again["files"] - again["skipped"])
        time.sleep(0.01)
        (self.repo / "pkg/util.py").write_text("def fmt(value):\n    return repr(value)\n\n\ndef added():\n    return 3\n")
        changed = code.index(self.con, self.repo, max_seconds=60)
        self.assertEqual(changed["parsed"], 1)
        self.assertIn("pkg/util.py::added", {row["id"] for row in code.symbols_of(self.con, "pkg/util.py")})
        self.assertNotIn(("tests/test_core.py::test_helper", "pkg/util.py::unique_name"), self.edges("calls"),
                         "edges are re-resolved after a change")

    def test_should_trust_a_stat_match_only_when_indexed_well_after_the_change(self):
        row = self.con.execute("SELECT indexed_ns, mtime_ns FROM code_file WHERE path='pkg/core.py'").fetchone()
        self.assertLess(row[0] - row[1], code.RACY_NS, "a just-written file is racily clean")
        self.con.execute("UPDATE code_file SET indexed_ns = mtime_ns + ? WHERE path='pkg/core.py'", (code.RACY_NS,))
        path = self.repo / "pkg/core.py"
        info = path.stat()
        same_size = path.read_text().replace("Help everyone.", "Help everybody")
        path.write_text(same_size)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(code.index(self.con, self.repo)["parsed"], 0, "trusted: size and mtime match an old index")
        self.con.execute("UPDATE code_file SET indexed_ns = mtime_ns WHERE path='pkg/core.py'")
        self.assertEqual(code.index(self.con, self.repo)["parsed"], 1, "racy: the content hash decides")

    def test_should_forget_deleted_files(self):
        self.git("rm", "-q", "pkg/two.py")
        self.git("commit", "-qm", "remove")
        report = code.index(self.con, self.repo)
        self.assertEqual(report["removed"], 1)
        self.assertEqual(code.find(self.con, "dup")[0]["path"], "pkg/one.py")
        self.assertIn(("pkg/caller.py::go", "pkg/one.py::dup"), self.edges("calls"), "now unique, so inferred")

    def test_should_stop_at_the_time_budget_and_continue_later(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        first = code.index(con, self.repo, max_seconds=1e-9)
        self.assertTrue(first["partial"])
        second = code.index(con, self.repo, max_seconds=60)
        self.assertFalse(second["partial"])
        self.assertEqual(code.stats(con)["files"], self.report["files"] - self.report["skipped"])

    def test_should_reindex_everything_when_the_extractor_changes(self):
        self.con.execute("UPDATE code_file SET version = version - 1")
        self.assertEqual(code.index(self.con, self.repo)["parsed"], self.report["parsed"])


class QueryTests(CodeCase):
    def test_should_report_dependents_with_the_site_that_uses_them(self):
        rows, total = code.impact(self.con, "pkg/core.py", depth=1)
        files = {row["file"]: row for row in rows}
        self.assertIn("pkg/api.py", files)
        self.assertIn("tests/test_core.py", files)
        self.assertTrue(files["tests/test_core.py"]["test"])
        self.assertRegex(files["pkg/api.py"]["site"], r"^pkg/api\.py:\d+$")
        deeper, _ = code.impact(self.con, "pkg/core.py", depth=2)
        self.assertIn("pkg/lazy.py", {row["file"] for row in deeper}, "depth 2 reaches importers of importers")
        self.assertGreaterEqual(total, 2)

    def test_should_start_impact_from_a_symbol(self):
        rows, _ = code.impact(self.con, "pkg/util.py::unique_name", depth=1)
        self.assertEqual([row["file"] for row in rows], ["tests/test_core.py"])

    def test_should_find_cycles_and_leave_out_deferred_imports(self):
        found, _ = code.cycles(self.con)
        self.assertIn(["pkg/cyc_a.py", "pkg/cyc_b.py"], [row["files"] for row in found])
        self.assertFalse(any("pkg/lazy.py" in row["files"] for row in found))
        everything, _ = code.cycles(self.con, include_deferred=True)
        self.assertTrue(any("pkg/lazy.py" in row["files"] for row in everything))

    def test_should_refuse_to_guess_a_name(self):
        result = self.cli("code", "index")
        self.assertEqual(result.returncode, 0, result.stderr)
        ambiguous = self.cli("code", "find", "dup")
        self.assertEqual(ambiguous.returncode, 4)
        self.assertEqual(len(toon.loads(ambiguous.stdout)["candidates"]), 2)
        self.assertEqual(self.cli("code", "find", "nothing_like_this").returncode, 5)
        found = toon.loads(self.cli("code", "find", "unique_name").stdout)
        self.assertEqual(found["id"], "pkg/util.py::unique_name")
        self.assertEqual(self.cli("impact", "dup").returncode, 4)

    def test_cli_impact_cycles_symbols_and_stats(self):
        self.cli("code", "index")
        impact = json.loads(self.cli("impact", "pkg/core.py", "--format", "json").stdout)
        self.assertIn("pkg/api.py", [row["file"] for row in impact["dependents"]])
        cycles = json.loads(self.cli("cycles", "--format", "json").stdout)
        self.assertEqual(cycles["total"], 1)
        symbols = json.loads(self.cli("code", "symbols", "pkg/core.py", "--format", "json").stdout)
        self.assertIn("pkg/core.py::Engine.step", [row["id"] for row in symbols["symbols"]])
        stats = json.loads(self.cli("stats", "--format", "json").stdout)
        self.assertTrue(stats["code"]["indexed"])
        self.assertEqual(self.cli("code", "symbols", "missing.py").returncode, 5)


class IntegrationTests(CodeCase):
    def test_should_refresh_the_code_layer_in_sync_and_show_it_in_context(self):
        result = graph.sync(self.repo, refine_after=True)
        self.assertGreater(result["code"]["parsed"], 0)
        with graph.locked(self.repo) as value:
            graph.add_insight(value, "Engine must stay single pass", "convention", about=["file:pkg/core.py"], repo=self.repo)
        context = self.cli("context", "--files", "pkg/core.py")
        self.assertIn("Files that depend on these", context.stdout)
        self.assertIn("pkg/api.py (imports at pkg/api.py:", context.stdout)
        self.assertIn("Code notes:", context.stdout)
        self.assertIn("WHY: one pass keeps the parser linear", context.stdout)
        structured = json.loads(self.cli("context", "--files", "pkg/core.py", "--json").stdout)
        self.assertIn("pkg/api.py", [row["file"] for row in structured["code"]["dependents"]])
        self.assertNotIn("imports", structured["code"], "core.py imports nothing from the repository")
        api = json.loads(self.cli("context", "--files", "pkg/api.py", "--json").stdout)
        self.assertIn("pkg/core.py", api["code"]["imports"])

    def test_should_show_code_for_files_the_graph_has_never_seen(self):
        graph.sync(self.repo, code_index=True)
        structured = json.loads(self.cli("context", "--files", "pkg/util.py", "--json").stdout)
        self.assertIn("tests/test_core.py", [row["file"] for row in structured["code"]["dependents"]])

    def test_code_index_runs_inside_the_graph_transaction(self):
        graph.sync(self.repo, code_index=True)
        con = sqlite3.connect(self.repo / ".improve" / "graph.db")
        try:
            files = con.execute("SELECT count(*) FROM code_file").fetchone()[0]
            revision = json.loads(con.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])
        finally:
            con.close()
        self.assertGreater(files, 0)
        self.assertGreaterEqual(revision, 1)

    def test_should_disable_the_code_layer_with_zero_seconds(self):
        result = graph.sync(self.repo, refine_after=True, code_seconds=0)
        self.assertNotIn("code", result)


if __name__ == "__main__":
    unittest.main()
