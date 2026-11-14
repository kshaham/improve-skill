"""The board's graph endpoints carry the signals the graph computes (code changed since a note,
contradictions, resolved blockers, gates, code dependents, link tiers, gaps, suggested
questions), refuse malformed numbers with JSON, and never drop a connection on a fault."""
import contextlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))
from graph_support import GIT_IDENTITY, budget, environment, make_repo, restore_environment, write_ledger, write_run  # noqa: E402
import improve_board as board  # noqa: E402
import improve_graph as graph  # noqa: E402

OPENER = build_opener(ProxyHandler({}))
SOURCES = {
    "pkg/__init__.py": "",
    "pkg/core.py": "# WHY: compute stays pure so the cache can memoize it\ndef compute(x):\n    return x + 1\n",
    "pkg/api.py": "from pkg.core import compute\n\n\ndef handler():\n    return compute(1)\n",
    "tests/test_core.py": "from pkg.core import compute\n\n\ndef test_compute():\n    assert compute(1) == 2\n",
}
# Eight tasks touched pkg/core.py: more than the six the task dialog shows.
TASKS = [dict(id="t1", title="Speed up compute", area="performance", status="blocked", file="pkg/core.py",
              blocked_reason="Benchmark runner offline", updated_at="2026-11-01T00:00:00Z", verification="py.fast")]
TASKS += [dict(id=f"t{index}", title=f"Compute fix {index}", area="quality", status="done", file="pkg/core.py",
               completed_at=f"2026-11-0{index}T00:00:00Z", verification="py.fast ok") for index in range(2, 9)]


def build_fixture(root):
    """A small repository with code, a run, two contradicting notes, a gap, and an edit
    made after the notes were recorded."""
    repo = make_repo(root / "repo", [])
    for name, text in SOURCES.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), *GIT_IDENTITY, "commit", "-qm", "fixture"], check=True)
    state = repo / ".improve"
    write_run(state, "live", gates={"py.fast": 12})
    write_ledger(state, TASKS)
    graph.sync(repo, refine_after=True)
    with graph.locked(repo) as value:
        pure = graph.add_insight(value, "compute must stay pure so the cache layer can memoize it", "convention",
                                 about=["file:pkg/core.py"], repo=repo)["id"]
        impure = graph.add_insight(value, "compute may write to the cache directly when speed matters", "decision",
                                   about=["file:pkg/core.py"], repo=repo)["id"]
        value["edges"][graph.edge_key(pure, "contradicts", impure)] = dict(
            src=pure, rel="contradicts", dst=impure, inferred=True, confidence=0.8, rule="opposite-claims",
            generation=1, evidence={})
        value["dream"]["gaps"] = [{"file": "pkg/api.py", "id": "file:pkg/api.py", "attempts": 3, "done": 0}]
    (repo / "pkg/core.py").write_text(SOURCES["pkg/core.py"].replace("x + 1", "x + 2"))
    return repo, pure, impure


@contextlib.contextmanager
def serving(repo):
    server = board.configure_server(board.BoardServer(("127.0.0.1", 0), board.BoardHandler), repo)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=budget(2))


class GraphSignalTests(unittest.TestCase):
    """Read-only against one fixture; tests that patch modules restore them."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="improve board signals ")
        cls.previous = environment(Path(cls.temp.name) / "home")
        cls.repo, cls.pure, cls.impure = build_fixture(Path(cls.temp.name))
        cls.serving = serving(cls.repo)
        cls.url = cls.serving.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.serving.__exit__(None, None, None)
        restore_environment(cls.previous)
        cls.temp.cleanup()

    def get(self, path, **params):
        query = ("?" + urlencode(params, doseq=True)) if params else ""
        with OPENER.open(Request(self.url + path + query), timeout=budget(10)) as response:
            return json.load(response)

    def failure(self, path, data=None, headers=None):
        with self.assertRaises(HTTPError) as caught:
            OPENER.open(Request(self.url + path, data=data, headers=headers or {}), timeout=budget(10))
        body = json.loads(caught.exception.read())
        caught.exception.close()
        return caught.exception.code, body

    def context(self, **params):
        return self.get("/api/graph/context", file="pkg/core.py", area="quality", **params)

    def test_node_detail_marks_changed_code_and_link_tiers(self):
        detail = self.get("/api/graph/node", id=self.pure)
        self.assertEqual(detail["node"]["code_changed"], ["pkg/core.py"])
        tiers = {row["rel"]: row["tier"] for row in detail["links"]}
        self.assertEqual(tiers["about"], "recorded")
        self.assertEqual(tiers["contradicts"], "inferred")
        self.assertTrue(all(row["tier"] in ("recorded", "observed", "inferred") for row in detail["links"]))
        task = self.get("/api/graph/node", id="file:pkg/core.py")
        self.assertIn("observed", {row["tier"] for row in task["links"]})

    def test_task_context_carries_every_signal_the_dialog_shows(self):
        result = self.context(text="compute cache")
        insights = {row["id"]: row for row in result["insights"]}
        self.assertEqual(insights[self.pure]["code_changed"], ["pkg/core.py"])
        self.assertEqual(insights[self.pure]["contradicted_by"], [self.impure])
        self.assertEqual(insights[self.impure]["contradicted_by"], [self.pure])
        blocker = next(row for row in result["blockers"] if row["label"] == "Benchmark runner offline")
        self.assertIs(blocker["resolved"], True, "later verified work on the file got past it")
        gate = next(row for row in result["gates"] if row["id"] == "gate:py.fast")
        self.assertEqual(gate["seconds"], 12)
        dependents = {row["file"]: row for row in result["code"]["dependents"]}
        self.assertEqual(set(dependents), {"pkg/api.py", "tests/test_core.py"})
        self.assertIs(dependents["tests/test_core.py"]["test"], True)
        self.assertEqual([(row["tag"], row["path"]) for row in result["code"]["notes"]], [("WHY", "pkg/core.py")])

    def test_the_open_task_is_excluded_inside_the_ranking(self):
        everything = self.context()
        self.assertEqual(len(everything["past_tasks"]), 6)
        first = everything["past_tasks"][0]["id"]
        excluded = self.context(exclude=first)
        ids = [row["id"] for row in excluded["past_tasks"]]
        self.assertNotIn(first, ids)
        self.assertEqual(len(ids), 6, "the excluded task does not take one of the six slots")
        by_ledger_id = self.context(exclude=first.rsplit(":", 1)[1])
        self.assertNotIn(first, [row["id"] for row in by_ledger_id["past_tasks"]])

    def test_knowledge_view_sends_gaps_and_omits_unrendered_fields(self):
        view = self.get("/api/graph")
        self.assertEqual(view["gaps"], [{"attempts": 3, "done": 0, "file": "pkg/api.py", "id": "file:pkg/api.py"}])
        for key in board.UNUSED_GRAPH_VIEW_KEYS:
            self.assertNotIn(key, view)
        self.assertTrue({"nodes", "edges", "dreams", "proposals", "hotspots", "revision"} <= set(view))

    def test_suggested_questions_name_their_kind_and_whether_they_open_a_node(self):
        result = self.get("/api/graph/questions")
        self.assertEqual(result["revision"], graph.load(graph.graph_path(self.repo))["revision"])
        kinds = {row["kind"] for row in result["questions"]}
        self.assertTrue({"contradiction", "changed", "gap"} <= kinds)
        contradiction = next(row for row in result["questions"] if row["kind"] == "contradiction")
        self.assertIs(contradiction["node"], True)
        self.assertIn(contradiction["other"], (self.pure, self.impure))
        gap = next(row for row in result["questions"] if row["kind"] == "gap")
        self.assertEqual(gap["id"], "file:pkg/api.py")
        self.assertIs(gap["node"], False, "a file no task touched is not a node to open")
        self.assertTrue(all(isinstance(row["question"], str) and row["question"] for row in result["questions"]))

    def test_malformed_and_huge_numbers_are_json_400(self):
        for path, params in (("/api/graph/history", {"offset": "9" * 30}),
                             ("/api/graph/history", {"limit": "many"}),
                             ("/api/graph/history", {"offset": "1e3"}),
                             ("/api/graph", {"depth": "9" * 5000}),
                             ("/api/graph", {"limit": "nan"})):
            with self.subTest(path=path, params=params):
                code, body = self.failure(path + "?" + urlencode(params))
                self.assertEqual(code, 400)
                self.assertIn("whole number", body["error"])
        self.assertEqual(self.get("/api/graph/history", offset="-5", limit="0")["total"], 0)
        self.assertLessEqual(len(self.get("/api/graph", limit="99999")["nodes"]), 800)

    def test_unexpected_faults_are_json_500_for_reads_and_writes(self):
        def broken(*_args, **_kwargs):
            raise RuntimeError("simulated graph fault")
        for module, name, path in ((graph, "board_view", "/api/graph"), (graph, "node_detail", "/api/graph/node?id=x"),
                                   (board, "suggested_questions", "/api/graph/questions")):
            original = getattr(module, name)
            setattr(module, name, broken)
            try:
                with self.subTest(path=path):
                    code, body = self.failure(path)
                    self.assertEqual((code, body["code"]), (500, "internal"))
                    self.assertIn("simulated graph fault", body["error"])
            finally:
                setattr(module, name, original)
        with OPENER.open(Request(self.url + "/api/session", headers={"X-Improve-Client": "board"}),
                         timeout=budget(10)) as response:
            token = json.load(response)["token"]
        original = board.graph_post
        board.graph_post = broken
        try:
            code, body = self.failure("/api/graph", json.dumps({"action": "refine", "request_id": str(uuid.uuid4())}).encode(),
                                      {"Content-Type": "application/json", "Origin": self.url,
                                       "X-Improve-Board-Token": token})
        finally:
            board.graph_post = original
        self.assertEqual((code, body["code"]), (500, "internal"))
        started = time.monotonic()
        self.assertIn("nodes", self.get("/api/graph"))
        self.assertLess(time.monotonic() - started, budget(5), "the server keeps answering after a fault")


if __name__ == "__main__":
    unittest.main()
