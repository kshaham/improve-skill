"""The instructions an agent follows for days must match the helpers it runs: every helper
command quoted in SKILL.md, max/, references/, docs/, and the README parses with that
helper's own parser; every IMPROVE_* setting the docs name exists in the code; the skill's
version matches the runtime's; and branch names the skill prescribes can exist in git."""
import argparse
import contextlib
import io
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from support import budget  # noqa: E402

DOCS = sorted([ROOT / "SKILL.md", ROOT / "README.md", *(ROOT / "max").rglob("*.md"), *(ROOT / "references").glob("*.md"),
               *(ROOT / "docs").glob("*.md")])
HELPERS = {"improve_graph.py": "improve_graph_cli", "improve_ledger.py": "improve_ledger",
           "improve_control.py": "improve_control", "improve_learning.py": "improve_learning"}
COMMAND = re.compile(r"scripts/(improve_(?:graph|ledger|control|learning)\.py)((?:[ \t]+[^\n`|]*?)?)(?=`|\n|$)")
# Shorthand the references define: `G` is the graph helper, `L` the ledger helper.
SHORTHAND = {"knowledge-graph.md": ("G", "improve_graph.py"), "ledger.md": ("L", "improve_ledger.py")}
PLACEHOLDER = re.compile(r"^<[^>]+>$")


class Parsed(Exception):
    """Raised instead of running the command once its arguments parsed."""


def parse_only(module_name, argv):
    """Run the helper's main() only as far as argument parsing; returns None or the error."""
    module = __import__(module_name)
    original = argparse.ArgumentParser.parse_known_args
    depth = [0]

    def stop(self, args=None, namespace=None):
        # Subparsers call parse_known_args recursively; stop once the outermost call is done,
        # so every check the top-level parser makes has run. parse_args calls it too.
        depth[0] += 1
        try:
            result = original(self, args, namespace)
        finally:
            depth[0] -= 1
        if depth[0] == 0:
            namespace_result, extra = result
            run_set_pairs = getattr(namespace_result, "action", None) == "run-set" and all(
                "=" in token and not token.startswith("-") for token in extra)
            if extra and not run_set_pairs:
                self.error(f"unrecognized arguments: {' '.join(extra)}")
            raise Parsed()
        return result

    stderr = io.StringIO()
    with mock.patch.object(argparse.ArgumentParser, "parse_known_args", stop), contextlib.redirect_stderr(stderr), \
            contextlib.redirect_stdout(io.StringIO()):
        try:
            module.main(argv)
        except Parsed:
            return None
        except SystemExit:
            return stderr.getvalue().strip().splitlines()[-1] if stderr.getvalue().strip() else "exit"
    return "main returned without parsing"


def documented_commands():
    """(doc, line, script, argv) for every helper invocation written in the docs."""
    found = []
    for doc in DOCS:
        text = doc.read_text()
        lines = text.splitlines()
        joined = re.sub(r"\\\n\s*", " ", text)  # Follow shell line continuations.
        for match in COMMAND.finditer(joined):
            found.append((doc, match.group(1), match.group(2)))
        shorthand = SHORTHAND.get(doc.name)
        if shorthand:
            letter, script = shorthand
            for line in lines:
                stripped = line.strip().lstrip("`")
                if re.match(rf"^{letter} [a-z-]+", stripped):
                    found.append((doc, script, " --repo /r " + stripped[2:].split("`")[0]))
    commands = []
    for doc, script, rest in found:
        try:
            tokens = shlex.split(rest.split("#", 1)[0])
        except ValueError:
            continue  # An unterminated quote in prose; not a runnable command.
        if not tokens or "..." in tokens or any(token.startswith("$(") for token in tokens):
            continue
        if len(tokens) <= 2 and tokens[0] == "--repo":
            continue  # A shorthand definition ("`G` stands for ..."), not an invocation.
        argv = ["x" if PLACEHOLDER.match(token) or token in ("PATH...", "ID...") else token for token in tokens]
        commands.append((doc.relative_to(ROOT), script, argv))
    return commands


class DocumentedCommandTests(unittest.TestCase):
    def test_every_documented_helper_command_parses(self):
        commands = documented_commands()
        self.assertGreater(len(commands), 25, "the extractor found the documented commands")
        failures = []
        for doc, script, argv in commands:
            error = parse_only(HELPERS[script], argv)
            if error:
                failures.append(f"{doc}: {script} {' '.join(argv)} -> {error}")
        self.assertEqual(failures, [])

    def test_every_command_in_the_cycle_prompt_parses(self):
        """The supervisor's prompt template names helper commands both as runnable lines
        (`{ledger} queue`) and inline (`set ID --status done ...`): both must parse."""
        source = (ROOT / "scripts/improve_runtime.py").read_text()
        modules = {"ledger": "improve_ledger", "graph": "improve_graph_cli", "controls": "improve_control",
                   "learning": "improve_learning"}
        commands = [(modules[name], rest) for name, rest in re.findall(r"^\{(ledger|graph|controls|learning)\} (.+)$",
                                                                       source, re.M)]
        inline = ("`set ID --status done --commit SHA --note '...'`", "`add --json '{{...}}'`", "`run-set KEY=VALUE`",
                  "`discovery-add --json '{{...}}'`", "`tasks --status blocked`", "`tasks --file PATH`", "`show ID`",
                  "`counts`", "`run --full`")
        for text in inline:
            self.assertIn(text, source, "the prompt still names this command")
            commands.append(("improve_ledger", text.strip("`")))
        self.assertGreaterEqual(len(commands), 15)
        failures = []
        for module, rest in commands:
            argv = ["--repo", "/r", *shlex.split(rest.replace("{{...}}", "{}").replace("'...'", "x"))]
            argv = ["x" if token in ("PATH...", "SHA") else token for token in argv]
            error = parse_only(module, argv)
            if error:
                failures.append(f"{module} {rest} -> {error}")
        self.assertEqual(failures, [])

    def test_output_switches_parse_in_either_position(self):
        for module in HELPERS.values():
            self.assertIsNone(parse_only(module, ["--repo", "/r", "--format", "json", self.any_command(module)]))
            self.assertIsNone(parse_only(module, ["--repo", "/r", self.any_command(module), "--format", "JSON"]))

    @staticmethod
    def any_command(module):
        return {"improve_graph_cli": "stats", "improve_ledger": "counts", "improve_control": "pending",
                "improve_learning": "guidance"}[module]


class DocumentedSettingsTests(unittest.TestCase):
    def test_every_documented_setting_exists_in_the_code(self):
        sources = [*(ROOT / "scripts").iterdir(), *(ROOT / "tests").glob("*.py")]  # Test knobs live in tests.
        code = "\n".join(path.read_text() for path in sources if path.suffix in (".py", ".sh"))
        missing = set()
        for doc in DOCS:
            for name in re.findall(r"\bIMPROVE_[A-Z][A-Z0-9_]*[A-Z0-9]\b", doc.read_text()):
                if not name.startswith("IMPROVE_TEST_") and name not in code:
                    missing.add(f"{doc.relative_to(ROOT)}: {name}")
        self.assertEqual(sorted(missing), [])

    def test_the_skill_version_matches_the_runtime(self):
        import improve_runtime
        declared = re.search(r"^\s+version:\s*(\S+)", (ROOT / "SKILL.md").read_text(), re.M).group(1)
        self.assertEqual(declared, improve_runtime.VERSION)


class ContinuousIntegrationTests(unittest.TestCase):
    """The workflow must keep exercising what the review found untested: macOS, Python 3.9 for
    every suite, lint, bounded jobs, and test-order independence."""

    def setUp(self):
        self.text = (ROOT / ".github/workflows/tests.yml").read_text()
        self.jobs = re.split(r"\n  (?=[a-z]+:\n)", self.text.split("\njobs:\n", 1)[1])

    def test_every_job_has_a_timeout(self):
        for job in self.jobs:
            self.assertIn("timeout-minutes:", job, job.splitlines()[0])

    def test_platforms_pythons_and_checks(self):
        self.assertIn("macos-latest", self.text)
        self.assertEqual(self.text.count('python: ["3.9", "3.13"]'), 2, "fast and slow suites on both Pythons")
        for step in ("tests/lint.py", "node --check", "reverse_order.py", "playwright==", "upload-artifact"):
            self.assertIn(step, self.text)
        self.assertEqual(self.text.count("browser_graph.py"), 0, "browser_board.py already runs the graph checks")


class BranchNameTests(unittest.TestCase):
    def test_bet_and_spike_branches_can_exist_beside_the_run_branch(self):
        text = "\n".join(path.read_text() for path in (ROOT / "max").rglob("*.md"))
        nested = re.findall(r"improve-max/<date>/(?:bet|spike)-<id>|improve-max/\d{4}-\d\d-\d\d/(?:bet|spike)-", text)
        self.assertEqual(nested, [], "a branch nested under the run branch cannot be created")
        with tempfile.TemporaryDirectory(prefix="improve branch names ") as directory:
            def git(*args):
                return subprocess.run(["git", "-C", directory, *args], capture_output=True, text=True, timeout=budget(30))
            git("init", "-q")
            git("commit", "-q", "--allow-empty", "-m", "base")
            for name in ("improve-max/2026-11-12", "improve-max/2026-11-12-bet-b-003", "improve-max/2026-11-12-spike-b-003"):
                result = git("branch", name)
                self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")


if __name__ == "__main__":
    unittest.main()
