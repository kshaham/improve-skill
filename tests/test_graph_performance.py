"""Speed and resource budgets for the knowledge graph at long-run scale, measured on real
processes. Ceilings are generous on purpose: they catch a design that degrades with size
(a full load where a partial one belongs, a quadratic pass), not a slow machine."""
from pathlib import Path
import random
import statistics
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_support import budget, slow, ROOT, LongSession, archive, check_disk, environment, load, make_repo, restore_environment, write_ledger, write_run  # noqa: E402
import improve_graph as graph  # noqa: E402

SCRIPT = str(ROOT / "scripts/improve_graph.py")
MEASURE = (
    "import resource, runpy, sys\n"
    "sys.argv = sys.argv[1:]\n"
    "sys.path.insert(0, __import__('os').path.dirname(sys.argv[0]))\n"
    "code = 0\n"
    "try:\n"
    "    runpy.run_path(sys.argv[0], run_name='__main__')\n"
    "except SystemExit as stop:\n"
    "    code = stop.code or 0\n"
    # On Linux a child's ru_maxrss starts at its parent's size when it was forked, so a test
    # process that has grown would report its own memory for every command. VmHWM is this
    # process's own peak.
    "def peak():\n"
    "    try:\n"
    "        with open('/proc/self/status') as status:\n"
    "            for line in status:\n"
    "                if line.startswith('VmHWM:'):\n"
    "                    return int(line.split()[1]) * 1024\n"
    "    except OSError:\n"
    "        pass\n"
    "    scale = 1 if sys.platform == 'darwin' else 1024\n"
    "    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale\n"
    "print('RSS', peak(), file=sys.stderr)\n"
    "sys.exit(code)\n")


@slow
class LongRunScale(unittest.TestCase):
    """20 archived runs of 300 tasks over 3,000 files, 300 cited notes, a live run, and dreams:
    about what a repository holds after several multi-day runs."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="improve graph performance ")
        cls.previous = environment(Path(cls.temp.name) / "home")
        cls.files = [f"src/m{index // 50}/f{index}.py" for index in range(3000)]
        cls.repo = make_repo(Path(cls.temp.name) / "repo", cls.files).resolve()
        rand = random.Random(13)
        with graph.locked(cls.repo) as value:
            cls.notes = [graph.add_insight(value, f"Module m{index % 60} needs fixture x{index} before test t{index}",
                                           ("pitfall", "technique", "fact")[index % 3],
                                           about=[cls.files[index * 3]], repo=cls.repo)["id"] for index in range(300)]
        for run in range(20):
            archive(cls.repo, f"2026-06-{run:02d}", f"r{run}", [
                dict(id=f"t{index}", title=f"Task {index} of run {run}", area=rand.choice(LongSession.AREAS),
                     status=rand.choice(("done", "done", "rejected", "blocked")), blocked_reason=f"Gate {index % 9} down",
                     paths=rand.sample(cls.files[:900], rand.randint(1, 4)), verification="py.fast ok",
                     graph_refs=rand.sample(cls.notes, rand.randint(0, 2)))
                for index in range(300)], gates={"py.fast": 9, "ts.fast": 31})
        cls.state = cls.repo / ".improve"
        write_run(cls.state, "live", {"py.fast": 9})
        write_ledger(cls.state, [dict(id=f"L{index}", title=f"Live {index}", status="done", area="ui",
                                      paths=[cls.files[index]], graph_refs=[cls.notes[index % 300]]) for index in range(400)])
        graph.sync(cls.repo)
        for _ in range(3):
            graph.sync(cls.repo, dream_after=True)
        cls.value = load(cls.repo)

    @classmethod
    def tearDownClass(cls):
        restore_environment(cls.previous)
        cls.temp.cleanup()

    def once(self, *args):
        """Wall time (ms) and peak memory (MB) of one real CLI process."""
        started = time.perf_counter()
        result = subprocess.run([sys.executable, "-c", MEASURE, SCRIPT, "--repo", str(self.repo), *args],
                                capture_output=True, text=True, timeout=budget(60))
        wall = (time.perf_counter() - started) * 1000
        self.assertEqual(result.returncode, 0, result.stderr)
        return wall, int(result.stderr.rsplit("RSS", 1)[1]) / 2 ** 20

    def cli(self, *args, runs=7):
        """Median wall time (ms) and peak memory (MB) of a real CLI process."""
        samples = [self.once(*args) for _ in range(runs)]
        return statistics.median(wall for wall, _ in samples), max(peak for _, peak in samples)

    def cli_in_turn(self, commands, runs=7):
        """Median and best wall time (ms) and peak memory (MB) per command, with the commands
        taking turns each round so a busy moment on the machine falls on all of them alike."""
        samples = {name: [] for name in commands}
        for _ in range(runs):
            for name, args in commands.items():
                samples[name].append(self.once(*args))
        return {name: (statistics.median(wall for wall, _ in rows), min(wall for wall, _ in rows),
                       max(peak for _, peak in rows)) for name, rows in samples.items()}

    def test_scale_is_what_a_long_run_produces(self):
        tasks = sum(node["type"] == "task" for node in self.value["nodes"].values())
        self.assertEqual(tasks, graph.MAX_TASKS, "the graph has reached its steady-state size")
        self.assertGreater(len(self.value["edges"]), 9000)
        self.assertGreater(sum(1 for edge in self.value["edges"].values() if edge.get("inferred")), 100)
        self.assertGreater(graph.history(self.repo, 1)["total"], 4000, "pruned work is in the history")
        check_disk(self, self.repo)

    def test_agent_reads_are_fast_lean_and_beat_loading_everything(self):
        file_path = self.files[3]
        reads = self.cli_in_turn({"context": ("context", "--files", file_path),
                                  "context with area": ("context", "--files", file_path, "--area", "ui"),
                                  "search": ("search", "fixture module"),
                                  "node": ("node", "file:" + file_path),
                                  "whole": ("stats",)})
        for name in ("context", "context with area", "search", "node"):
            with self.subTest(command=name):
                wall, _, rss = reads[name]
                self.assertLess(wall, budget(400), f"{name} median {wall:.0f} ms")
                self.assertLess(rss, budget(90), f"{name} peak {rss:.0f} MB")
        # Compare designs on each command's best time: noise only ever adds to a run.
        context, node, with_area, whole = (reads[name][1] for name in ("context", "node", "context with area", "whole"))
        context_rss, whole_rss = reads["context"][2], reads["whole"][2]
        # A file's context and a node read a neighborhood; an area walks all of its tasks by design.
        self.assertLess(context, whole * 0.8, f"context {context:.0f} ms vs whole-graph read {whole:.0f} ms")
        self.assertLess(node, whole * 0.8, f"node {node:.0f} ms vs whole-graph read {whole:.0f} ms")
        self.assertLess(with_area, whole * 1.05, f"area context {with_area:.0f} ms vs whole read {whole:.0f} ms")
        self.assertLess(context_rss, whole_rss, "a partial read holds less in memory")

    def test_agent_writes_are_fast(self):
        note, note_rss = self.cli("note", "--kind", "fact", "--text", "Performance probe note about cache keys",
                                  "--about", self.files[7], runs=5)
        rate, _ = self.cli("rate", self.notes[1], "useful", runs=5)
        self.assertLess(note, budget(500), f"note median {note:.0f} ms")
        self.assertLess(rate, budget(400), f"rate median {rate:.0f} ms")
        self.assertLess(note_rss, budget(90))

    def test_partial_reads_touch_a_fraction_of_the_graph(self):
        total = len(self.value["nodes"])
        with graph.reading(self.repo) as part:
            graph.load_node(part, self.files[3] and "file:" + self.files[3])
            self.assertLess(len(part["nodes"]), total * 0.05)
        with graph.reading(self.repo) as part:
            graph.load_context(part, [self.files[3]], None, self.repo)
            self.assertLess(len(part["nodes"]), total * 0.05, "a file's neighborhood, not every file")

    def test_daemon_work_fits_between_cycles(self):
        for label, kwargs, ceiling in (("sync", {}, 1.5), ("refine", {"refine_after": True}, 4.0),
                                       ("dream", {"dream_after": True}, 6.0)):
            with self.subTest(step=label):
                started = time.perf_counter()
                graph.sync(self.repo, **kwargs)
                self.assertLess(time.perf_counter() - started, budget(ceiling), label)
        _, dream_rss = self.cli("dream", runs=1)
        self.assertLess(dream_rss, budget(250), f"a whole-graph dream peaks at {dream_rss:.0f} MB")

    def test_the_board_reuses_an_unchanged_graph(self):
        path = graph.graph_path(self.repo)
        first = graph.load_cached(path)
        started = time.perf_counter()
        for _ in range(50):
            self.assertIs(graph.load_cached(path), first)
        self.assertLess((time.perf_counter() - started) / 50, budget(0.01), "an unchanged poll costs one tiny query")
        with graph.locked(self.repo, partial=True) as value:
            graph.add_insight(value, "Board cache invalidation probe", "fact")
        self.assertIsNot(graph.load_cached(path), first)


if __name__ == "__main__":
    unittest.main()
