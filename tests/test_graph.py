"""Knowledge graph: ingest, refinement, queries, and board endpoints, without launching agents."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import improve_board as board  # noqa: E402
import improve_graph as graph  # noqa: E402
import improve_learning as learning  # noqa: E402


def write_rows(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


class GraphFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve graph tests ")
        self.repo = Path(self.temp.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        for name in ("src/api/db.py", "src/api/auth.py", "src/ui/cart.tsx", "src/ui/button.tsx"):
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text("x\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "src"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "commit", "-qm", "fixture"], check=True)
        self.state = self.repo / ".improve"
        self.state.mkdir()
        self.home = os.environ.get("IMPROVE_LEARNING_HOME")
        os.environ["IMPROVE_LEARNING_HOME"] = str(Path(self.temp.name) / "home")
        archive = self.state / "history" / "2026-11-01T00-00-00Z-old"
        archive.mkdir(parents=True)
        (archive / "supervisor.json").write_text(json.dumps({"run_id": "r-old", "outcome": "completed"}))
        (archive / "run.json").write_text(json.dumps({"gates": {"py.fast": "pytest -q"}, "gate_seconds": {"py.fast": 12}}))
        write_rows(archive / "backlog.jsonl", [
            dict(id="t1", title="Pool DB connections", area="performance", status="rejected", file="src/api/db.py",
                 note="benchmark noise"),
            dict(id="t2", title="Retry deadlocks", area="resilience", status="rejected", file="src/api/db.py:42",
                 note="tests flaked"),
            dict(id="t3", title="Token expiry", area="security", status="done", paths=["src/api/auth.py", "src/api/db.py"],
                 verification="py.fast passed"),
        ])
        (self.state / "supervisor.json").write_text(json.dumps({"run_id": "r-new", "outcome": None}))
        write_rows(self.state / "backlog.jsonl", [
            dict(id="n1", title="Cart empty state", area="ui", status="blocked", file="src/ui/cart.tsx",
                 blocked_reason="Simulator 17 unavailable"),
            dict(id="n2", title="Cart labels", area="ui", status="blocked", file="/etc/passwd",
                 blocked_reason="Simulator 18 unavailable"),
            dict(id="n3", title="Removed module", area="quality", status="done", paths=["src/old.py", "../escape.py"]),
        ])

    def tearDown(self):
        if self.home is None:
            os.environ.pop("IMPROVE_LEARNING_HOME", None)
        else:
            os.environ["IMPROVE_LEARNING_HOME"] = self.home
        board.stop_board(self.repo)
        self.temp.cleanup()

    def graph(self):
        return graph.load(graph.graph_path(self.repo), repair=False)


class GraphEngineTests(GraphFixture):
    def test_sync_backfills_history_and_is_idempotent(self):
        result = graph.sync(self.repo, refine_after=True)
        self.assertEqual(result["ingested"], ["r-old", "r-new"])
        first = self.graph()
        graph.sync(self.repo, refine_after=True)
        second = self.graph()
        self.assertEqual(len(first["nodes"]), len(second["nodes"]))
        self.assertEqual(len(first["edges"]), len(second["edges"]))
        self.assertEqual([run["seq"] for run in second["runs"]], [1, 2])
        nodes = second["nodes"]
        self.assertEqual(nodes["file:src/api/db.py"]["stats"]["attempts"], 3, "a :line suffix is the same file")
        self.assertNotIn("file:/etc/passwd", nodes)
        self.assertFalse(any("escape" in key for key in nodes), "paths outside the repo are never stored")
        self.assertTrue(nodes["file:src/old.py"]["stale"])
        self.assertIn("task:r-old:t3|verified_by|gate:py.fast", second["edges"])
        blockers = [node for node in nodes.values() if node["type"] == "blocker"]
        self.assertEqual(len(blockers), 1, "numbers must not split a recurring blocker")
        self.assertEqual(blockers[0]["uses"], 2)

    def test_co_change_hotspots_context_and_paths(self):
        graph.sync(self.repo, refine_after=True)
        value = self.graph()
        self.assertIn("file:src/api/auth.py|co_changed|file:src/api/db.py", value["edges"])
        self.assertEqual(graph.hotspots(value)[0]["file"], "src/api/db.py")
        result = graph.context(value, ["src/api/db.py"], "performance", repo=self.repo)
        self.assertEqual({task["label"] for task in result["past_tasks"]} >= {"Pool DB connections", "Retry deadlocks"}, True)
        self.assertEqual(result["co_changed"][0]["label"], "src/api/auth.py")
        self.assertEqual(result["gates"][0]["label"], "py.fast")
        # A directory or a new file beside known files still finds their history.
        self.assertTrue(graph.context(value, ["src/api"], repo=self.repo)["past_tasks"])
        self.assertTrue(graph.context(value, ["src/api/new.py"], repo=self.repo)["past_tasks"])
        steps = graph.shortest_path(value, "file:src/api/auth.py", "area:performance")
        self.assertEqual(steps[-1]["to"], "area:performance")
        with self.assertRaises(graph.GraphError):
            graph.context(value)

    def test_notes_reinforce_merge_and_reach_the_brief(self):
        graph.sync(self.repo)
        with graph.locked(self.repo) as value:
            first = graph.add_insight(value, "db.py retry tests flake under parallel pytest; run serially",
                                      "pitfall", about=["src/api/db.py", "area:resilience"], repo=self.repo)
            again = graph.add_insight(value, "DB.py retry tests flake under parallel pytest, run serially!", "pitfall")
            self.assertEqual(first["id"], again["id"])
            self.assertEqual(again["reinforced"], 2)
            with self.assertRaises(graph.GraphError):
                graph.add_insight(value, "short")
            with self.assertRaises(graph.GraphError):
                graph.add_insight(value, "A valid sentence with a bad kind", "rumor")
            with self.assertRaises(graph.GraphError):
                graph.add_insight(value, "Points at nothing that can exist", about=["nonsense"])
        value = self.graph()
        context = graph.context(value, ["src/api/db.py"], repo=self.repo)
        self.assertEqual(context["insights"][0]["id"], first["id"])
        text = graph.brief(value, "graph.py", "/repo")
        self.assertIn(first["id"], text)
        self.assertIn("about src/api/db.py", text)
        self.assertIn("py.fast 12s", text)
        self.assertLessEqual(len(graph.brief(value, "graph.py", "/repo", max_chars=200)), 200)

    def test_refine_merges_twins_and_keeps_aliases(self):
        with graph.locked(self.repo) as value:
            graph.upsert(value, "insight:a", "insight", "x", 0, summary="Run the cart tests with the iOS 18 simulator",
                         kind="pitfall", reinforced=3, useful=0, wrong=0)
            graph.upsert(value, "insight:b", "insight", "x", 0, summary="Run cart tests with the iOS 18 simulator",
                         kind="pitfall", reinforced=1, useful=0, wrong=0, source="user")
            report = graph.refine(value, self.repo)
        self.assertEqual(report["merged"], 1)
        value = self.graph()
        self.assertNotIn("insight:b", value["nodes"])
        merged = value["nodes"]["insight:a"]
        self.assertEqual((merged["reinforced"], merged["source"]), (4, "user"))
        self.assertEqual(graph.node_detail(value, "insight:b")["node"]["id"], "insight:a")

    def test_citations_and_ratings_change_confidence_and_fade_knowledge(self):
        with graph.locked(self.repo) as value:
            helpful = graph.add_insight(value, "Seed the cache before running the cart benchmark", "technique")
            wrong = graph.add_insight(value, "The cart screen never needs a simulator", "fact")
        rows = [json.loads(line) for line in (self.state / "backlog.jsonl").read_text().splitlines()]
        rows.append(dict(id="n4", title="Cart benchmark", area="performance", status="done",
                         file="src/ui/cart.tsx", graph_refs=[helpful["id"], "insight:missing"]))
        write_rows(self.state / "backlog.jsonl", rows)
        graph.sync(self.repo, refine_after=True)
        with graph.locked(self.repo) as value:
            self.assertEqual(value["nodes"][helpful["id"]]["cited_done"], 1)
            graph.rate(value, wrong["id"], "wrong")
            graph.rate(value, wrong["id"], "wrong")
            graph.refine(value, self.repo)
        value = self.graph()
        self.assertGreater(value["nodes"][helpful["id"]]["score"], value["nodes"][wrong["id"]]["score"])
        self.assertIn("contradicted", value["nodes"][wrong["id"]]["archived"])
        self.assertNotIn("never needs a simulator", graph.brief(value, "g", "r"))
        # Knowledge unused for many runs fades unless pinned; restoring brings it back.
        with graph.locked(self.repo) as value:
            stale = graph.add_insight(value, "An old note nobody relies on any more", "fact")
            kept = graph.add_insight(value, "A pinned convention that must stay visible", "convention")
            graph.rate(value, kept["id"], "pin")
            value["runs"].extend({"run_id": f"later-{index}", "seq": index + 3} for index in range(12))
            graph.refine(value, self.repo)
            self.assertIn("not used", value["nodes"][stale["id"]]["archived"])
            self.assertNotIn("archived", value["nodes"][kept["id"]])
            graph.rate(value, stale["id"], "restore")
            graph.refine(value, self.repo)
            self.assertNotIn("archived", value["nodes"][stale["id"]])
        with graph.locked(self.repo) as value, self.assertRaises(graph.GraphError):
            graph.rate(value, kept["id"], "love")

    def test_pruning_folds_outcomes_into_file_baselines(self):
        with graph.locked(self.repo) as value:
            graph.upsert(value, "file:src/api/db.py", "file", "src/api/db.py", 1)
            for index in range(30):
                task = f"task:r:{index}"
                graph.upsert(value, task, "task", task, 1, status="done" if index % 3 else "rejected", area="quality")
                graph.link(value, task, "touches", "file:src/api/db.py", 1)
            original = graph.MAX_TASKS
            graph.MAX_TASKS = 10
            try:
                report = graph.refine(value, self.repo)
            finally:
                graph.MAX_TASKS = original
            stats = value["nodes"]["file:src/api/db.py"]["stats"]
        self.assertEqual(report["pruned_tasks"], 20)
        self.assertEqual((stats["done"], stats["rejected"]), (20, 10))

    def test_learning_lessons_are_mirrored_once(self):
        learning.add_lesson(self.repo, "Use the serial runner for integration tests.", area="coverage")
        graph.sync(self.repo)
        graph.sync(self.repo)
        lessons = [node for node in self.graph()["nodes"].values() if node.get("kind") == "lesson"]
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]["source"], "learning")
        self.assertIn(f"{lessons[0]['id']}|about|area:coverage", self.graph()["edges"])
        # The prompt already lists learning lessons; the graph brief does not repeat them.
        self.assertNotIn("serial runner", graph.brief(self.graph(), "g", "r"))

    def test_damaged_graph_is_set_aside_and_readers_never_fail(self):
        graph.graph_path(self.repo).write_text("{broken")
        self.assertEqual(graph.load(graph.graph_path(self.repo), repair=False)["nodes"], {})
        self.assertTrue(graph.graph_path(self.repo).exists(), "readers leave the damaged file alone")
        graph.sync(self.repo)
        self.assertTrue(graph.graph_path(self.repo).with_suffix(".damaged.json").exists())
        self.assertTrue(self.graph()["nodes"])

    def test_board_view_projects_edges_through_hidden_tasks(self):
        graph.sync(self.repo, refine_after=True)
        view = graph.board_view(self.graph(), types=["file", "area", "blocker"])
        self.assertFalse(any(node["type"] == "task" for node in view["nodes"]))
        projected = [edge for edge in view["edges"] if edge["rel"] == "via_task"]
        self.assertTrue(any({edge["src"], edge["dst"]} == {"file:src/ui/cart.tsx", "area:ui"} for edge in projected))
        focus = graph.board_view(self.graph(), focus="file:src/api/db.py", depth=1)
        self.assertIn("task:r-old:t1", {node["id"] for node in focus["nodes"]})
        with self.assertRaises(graph.GraphError):
            graph.board_view(self.graph(), focus="file:missing")
        hits = graph.board_view(self.graph(), query="simulator")
        self.assertTrue(any(node["type"] == "blocker" for node in hits["nodes"]))

    def test_cli_round_trip(self):
        script = [sys.executable, str(ROOT / "scripts/improve_graph.py"), "--repo", str(self.repo)]
        run = lambda *args: subprocess.run([*script, *args], capture_output=True, text=True, env=os.environ)
        self.assertEqual(run("sync", "--refine").returncode, 0)
        note = run("note", "--kind", "pitfall", "--text", "Cart tests need the iOS 18 simulator", "--about", "src/ui/cart.tsx")
        self.assertEqual(note.returncode, 0, note.stderr)
        node_id = json.loads(note.stdout)["id"]
        self.assertIn(node_id, run("context", "--files", "src/ui/cart.tsx").stdout)
        self.assertIn("Cart empty state", run("context", "--files", "src/ui/cart.tsx").stdout)
        self.assertEqual(json.loads(run("rate", node_id, "useful").stdout)["useful"], 1)
        self.assertEqual(run("link", node_id, "fixes", "blocker:nothing").returncode, 2)
        self.assertEqual(run("link", node_id, "relates_to", "area:ui").returncode, 0)
        self.assertIn(node_id, run("brief").stdout)
        self.assertIn(node_id, [row["id"] for row in json.loads(run("search", "simulator").stdout)])
        self.assertEqual(json.loads(run("stats").stdout)["runs"], 2)
        self.assertIn("improve-graph:", run("node", "insight:missing").stderr)


class GraphServerTests(GraphFixture):
    def setUp(self):
        super().setUp()
        graph.sync(self.repo, refine_after=True)
        self.url = board.start_board(self.repo, 0)["url"]

    def request(self, path, data=None, headers=None):
        body = json.dumps(data).encode() if data is not None else None
        headers = {**({"Content-Type": "application/json"} if body else {}), **(headers or {})}
        with build_opener(ProxyHandler({})).open(Request(self.url + path, data=body, headers=headers,
                                                         method="POST" if body else "GET"), timeout=5) as response:
            return json.load(response)

    def post(self, data):
        token = self.request("/api/session", headers={"X-Improve-Client": "board"})["token"]
        return self.request("/api/graph", data, {"Origin": self.url, "X-Improve-Board-Token": token})

    def test_board_reads_summary_subgraph_node_and_task_context(self):
        snapshot = self.request("/api/board?run=all")
        self.assertEqual(snapshot["graph"]["runs"], 2)
        self.assertEqual({run["control_id"] for run in snapshot["runs"]}, {"r-new", "r-old"})
        view = self.request("/api/graph?types=file,insight&limit=50")
        self.assertTrue(view["nodes"])
        self.assertTrue(all(node["type"] in ("file", "insight") for node in view["nodes"]))
        detail = self.request("/api/graph/node?id=file:src/api/db.py")
        self.assertEqual(detail["node"]["stats"]["attempts"], 3)
        context = self.request("/api/graph/context?file=src/api/db.py&area=performance&exclude=task:r-old:t1")
        self.assertNotIn("task:r-old:t1", [task["id"] for task in context["past_tasks"]])
        self.assertTrue(context["past_tasks"])
        empty = self.request("/api/graph/context?area=Not%20An%20Area")
        self.assertEqual(empty["past_tasks"], [])
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/graph/node?id=insight:missing")
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()

    def test_curation_requires_the_board_session_and_applies_retries_once(self):
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/graph", {"action": "refine", "request_id": "abcdefgh1"})
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()
        note = {"action": "note", "request_id": "note-retry-1", "text": "Payments code is frozen until the audit.",
                "kind": "decision", "about": ["file:src/api/db.py", "area:security"]}
        first = self.post(note)
        self.assertEqual(self.post(note), first)
        node = self.graph()["nodes"][first["node"]["id"]]
        self.assertEqual((node["reinforced"], node["source"]), (1, "user"))
        self.assertEqual(self.post({"action": "rate", "request_id": "rate-0001", "id": node["id"], "verdict": "pin"})
                         ["node"]["pinned"], True)
        self.assertIn("Refined", self.post({"action": "refine", "request_id": "refine-001"})["message"])
        for bad in ({"action": "note", "request_id": "bad-00001", "text": "x" * 700},
                    {"action": "note", "request_id": "bad-00002", "text": "Valid text here", "about": ["a"] * 7},
                    {"action": "rate", "request_id": "bad-00003", "id": node["id"], "verdict": "love"},
                    {"action": "drop", "request_id": "bad-00004"},
                    {"action": "refine"}):
            with self.subTest(bad=bad), self.assertRaises(HTTPError) as caught:
                self.post(bad)
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()


if __name__ == "__main__":
    unittest.main()
