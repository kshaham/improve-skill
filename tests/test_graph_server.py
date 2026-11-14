"""Board interactions with a large knowledge graph: every endpoint, parameter abuse,
concurrent and retried curation, cache freshness, and reads during live daemon writes.

The large fixture is built once per module and never served. Tests see copies of it: a test
that writes gets its own copy, so curation in one test cannot change what another one reads,
whatever order they run in."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import random
import shutil
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import uuid
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_support import budget, LongSession, archive, check_invariants, environment, load, make_repo, restore_environment, write_ledger, write_run  # noqa: E402
import improve_board as board  # noqa: E402
import improve_graph as graph  # noqa: E402

OPENER = build_opener(ProxyHandler({}))


_TEMPLATE = None


def template():
    """10 archived runs of 300 tasks over 1,500 files, a live run, and 300 notes, built once."""
    global _TEMPLATE
    if _TEMPLATE is not None:
        return _TEMPLATE
    temp = tempfile.TemporaryDirectory(prefix="improve graph server template ")
    root = Path(temp.name).resolve()
    previous = environment(root / "home")
    try:
        files = [f"app/p{index // 60}/m{index}.ts" for index in range(1500)]
        repo = make_repo(root / "repo", files).resolve()
        rand = random.Random(9)
        for run in range(10):
            archive(repo, f"2026-08-{run:02d}", f"r{run}", [
                dict(id=f"t{index}", title=f"Task {index} run {run}", area=rand.choice(LongSession.AREAS),
                     status=rand.choice(("done", "rejected", "blocked")), blocked_reason=f"Runner {index % 9} offline",
                     paths=rand.sample(files[:700], rand.randint(1, 4)), verification="ts.fast ok")
                for index in range(300)], gates={"ts.fast": 30})
        state = repo / ".improve"
        write_run(state, "live")
        rows = [dict(id=f"L{index}", title=f"Live {index}", area="ui", status="ready", file=files[index])
                for index in range(30)]
        write_ledger(state, rows)
        graph.sync(repo, refine_after=True)
        with graph.locked(repo) as value:
            notes = [graph.add_insight(value, f"Package {index} build needs cache key c{index} and flag v{index % 5}",
                                       "pitfall", about=[files[index]], repo=repo)["id"] for index in range(300)]
            graph.refine(value, repo)
    except BaseException:
        restore_environment(previous)
        temp.cleanup()
        raise
    restore_environment(previous)
    _TEMPLATE = SimpleNamespace(temp=temp, root=root, files=tuple(files), rows=tuple(rows), notes=tuple(notes))
    return _TEMPLATE


def tearDownModule():
    if _TEMPLATE is not None:
        _TEMPLATE.temp.cleanup()


class Workspace:
    """A private copy of the template (repository and learning home) with its own board."""

    def __init__(self, fixture):
        self.temp = tempfile.TemporaryDirectory(prefix="improve graph server ")
        root = Path(self.temp.name).resolve() / "copy"
        shutil.copytree(fixture.root, root, symlinks=True)
        self.previous = environment(root / "home")
        self.repo = root / "repo"
        self.state = self.repo / ".improve"
        try:
            self.url = board.start_board(self.repo, 0)["url"]
        except BaseException:
            self.close()
            raise

    def close(self):
        try:
            board.stop_board(self.repo)
        finally:
            restore_environment(self.previous)
            self.temp.cleanup()


class LargeBoardGraph(unittest.TestCase):
    """Each test gets its own copy of the large graph; a read-only class sets SHARED so its
    tests share one copy."""

    SHARED = False

    @classmethod
    def setUpClass(cls):
        fixture = template()
        cls.files, cls.rows, cls.notes = list(fixture.files), fixture.rows, list(fixture.notes)
        cls.shared_workspace = Workspace(fixture) if cls.SHARED else None

    @classmethod
    def tearDownClass(cls):
        if cls.shared_workspace is not None:
            cls.shared_workspace.close()

    def setUp(self):
        self.workspace = self.shared_workspace or Workspace(template())
        if self.workspace is not self.shared_workspace:
            self.addCleanup(self.workspace.close)
        self.repo, self.state, self.url = self.workspace.repo, self.workspace.state, self.workspace.url

    def get(self, path, **params):
        query = ("?" + urlencode(params, doseq=True)) if params else ""
        started = time.perf_counter()
        with OPENER.open(Request(self.url + path + query), timeout=budget(10)) as response:
            body = response.read()
        return json.loads(body), time.perf_counter() - started, len(body)

    def error(self, path, data=None, headers=None):
        with self.assertRaises(HTTPError) as caught:
            OPENER.open(Request(self.url + path, data=data, headers=headers or {},
                                method="POST" if data is not None else "GET"), timeout=budget(10))
        body = json.loads(caught.exception.read())
        caught.exception.close()
        return caught.exception.code, body.get("error", "")

    def token(self):
        with OPENER.open(Request(self.url + "/api/session", headers={"X-Improve-Client": "board"}),
                         timeout=budget(10)) as response:
            return json.load(response)["token"]

    def post(self, data, token=None):
        request = Request(self.url + "/api/graph", data=json.dumps(data).encode(), method="POST", headers={
            "Content-Type": "application/json", "Origin": self.url, "X-Improve-Board-Token": token or self.token()})
        with OPENER.open(request, timeout=budget(30)) as response:
            return json.load(response)

    def note(self, text, request_id=None, token=None, **extra):
        return self.post({"action": "note", "request_id": request_id or str(uuid.uuid4()), "text": text,
                          "kind": "fact", **extra}, token)


class GraphReadEndpointTests(LargeBoardGraph):
    SHARED = True  # Reads only: one copy for the class.

    def test_default_view_is_bounded_fast_and_small(self):
        view, seconds, size = self.get("/api/graph")
        self.assertLessEqual(len(view["nodes"]), 250)
        self.assertTrue(view["truncated"])
        self.assertGreater(view["total_nodes"], 2000)
        self.assertLess(seconds, budget(1.0))
        self.assertLess(size, 1024 * 1024)
        ids = {node["id"] for node in view["nodes"]}
        self.assertTrue(all(edge["src"] in ids and edge["dst"] in ids for edge in view["edges"]))

    def test_limit_depth_types_and_flags(self):
        self.assertEqual(len(self.get("/api/graph", limit=5000)[0]["nodes"]), 800)
        self.assertEqual(len(self.get("/api/graph", limit=0)[0]["nodes"]), 10)
        self.assertEqual(len(self.get("/api/graph", limit=-7)[0]["nodes"]), 10)
        files = self.get("/api/graph", types="file,nonsense")[0]
        self.assertEqual({node["type"] for node in files["nodes"]}, {"file"})
        anything = self.get("/api/graph", types="nonsense")[0]
        self.assertGreater(len({node["type"] for node in anything["nodes"]}), 1, "unknown types do not empty the view")
        focus, seconds, size = self.get("/api/graph", focus="area:ui", depth=3, limit=800)
        self.assertEqual(focus["focus"], "area:ui")
        self.assertLessEqual(len(focus["nodes"]), 800)
        self.assertLess(seconds, budget(1.5))
        self.assertLess(size, 3 * 1024 * 1024)
        hits = self.get("/api/graph", q="runner offline")[0]
        self.assertTrue(any(node["type"] == "blocker" for node in hits["nodes"]))

    def test_invalid_parameters_are_clean_errors(self):
        cases = [{"limit": "many"}, {"depth": "deep", "focus": "area:ui"}, {"focus": "file:not/here.ts"}]
        for params in cases:
            with self.subTest(params=params):
                code, message = self.error("/api/graph?" + urlencode(params))
                self.assertEqual(code, 400)
                self.assertTrue(message)
        code, _ = self.error("/api/graph?" + "&".join(f"f{index}=1" for index in range(60)))
        self.assertEqual(code, 400, "query parameter floods are rejected")
        self.assertEqual(self.error("/api/graph/node?id=insight:nope")[0], 400)
        self.assertEqual(self.error("/api/graph/unknown")[0], 404)
        long_id = "file:" + "x" * 5000
        self.assertEqual(self.error("/api/graph/node?" + urlencode({"id": long_id}))[0], 400)

    def test_node_detail_is_ranked_and_capped_for_busy_nodes(self):
        detail, seconds, _ = self.get("/api/graph/node", id="area:ui")
        self.assertEqual(len(detail["links"]), 200)
        self.assertGreater(detail["total_links"], 200)
        self.assertLess(seconds, budget(1.0))
        for note in (self.notes[0], self.notes[-1]):
            self.assertEqual(self.get("/api/graph/node", id=note)[0]["node"]["id"], note)

    def test_task_context_is_bounded_and_excludes_the_open_task(self):
        files = self.files[:25]
        context, seconds, _ = self.get("/api/graph/context", file=files, area="ui", text="cache key",
                                       exclude="task:live:L0")
        self.assertLess(seconds, budget(1.0))
        self.assertLessEqual(len([seed for seed in context["seeds"] if seed.startswith("file:")]), 20)
        self.assertNotIn("task:live:L0", [task["id"] for task in context["past_tasks"]])
        self.assertTrue(context["insights"])
        for area in ("UI", "../ui", "x" * 80):
            with self.subTest(area=area):
                self.assertEqual(self.get("/api/graph/context", area=area)[0]["past_tasks"], [])
        empty = self.get("/api/graph/context")[0]
        self.assertEqual((empty["seeds"], empty["insights"]), ([], []))

    def test_suggested_questions_are_bounded_on_a_large_graph(self):
        result, seconds, _ = self.get("/api/graph/questions")
        self.assertLessEqual(len(result["questions"]), board.QUESTION_LIMIT)
        self.assertTrue(all(set(row) >= {"kind", "id", "question", "node"} for row in result["questions"]))
        self.assertLess(seconds, budget(3.0))

    def test_board_snapshot_carries_a_cheap_summary(self):
        snapshot, seconds, _ = self.get("/api/board", run="all")
        self.assertEqual(snapshot["graph"]["runs"], 11)
        self.assertGreater(snapshot["graph"]["insights"], 0)
        self.assertEqual(set(snapshot["graph"]), {"revision", "nodes", "edges", "runs", "insights", "last_refine",
                                                  "last_dream", "tick", "inferred"},
                         "the polled snapshot carries counters only; the graph itself is fetched on demand")
        self.assertIsInstance(snapshot["graph"]["nodes"], int)
        self.assertLess(seconds, budget(3.0))


class GraphCurationTests(LargeBoardGraph):
    def test_writes_require_the_board_session_and_same_origin(self):
        body = json.dumps({"action": "refine", "request_id": "unauth-0001"}).encode()
        for headers in ({"Content-Type": "application/json"},
                        {"Content-Type": "application/json", "Origin": self.url, "X-Improve-Board-Token": "0" * 64},
                        {"Content-Type": "application/json", "Origin": "https://evil.invalid",
                         "X-Improve-Board-Token": self.token()}):
            with self.subTest(headers=headers):
                self.assertEqual(self.error("/api/graph", body, headers)[0], 403)
        headers = {"Content-Type": "text/plain", "Origin": self.url, "X-Improve-Board-Token": self.token()}
        self.assertEqual(self.error("/api/graph", body, headers)[0], 400)
        headers["Content-Type"] = "application/json"
        self.assertEqual(self.error("/api/graph", b"x" * 70000, headers)[0], 400, "bodies over 64 KiB are refused")
        self.assertEqual(self.error("/api/graph", b"[1, 2]", headers)[0], 400)

    def test_validation_of_every_action(self):
        token = self.token()
        bad = [
            {"action": "note", "request_id": "short", "text": "A valid piece of knowledge"},
            {"action": "note", "request_id": str(uuid.uuid4()), "text": "tiny"},
            {"action": "note", "request_id": str(uuid.uuid4()), "text": "x" * 601},
            {"action": "note", "request_id": str(uuid.uuid4()), "text": 12345678},
            {"action": "note", "request_id": str(uuid.uuid4()), "text": "Valid knowledge", "kind": "rumor"},
            {"action": "note", "request_id": str(uuid.uuid4()), "text": "Valid knowledge", "about": "area:ui"},
            {"action": "note", "request_id": str(uuid.uuid4()), "text": "Valid knowledge", "about": ["x" * 301]},
            {"action": "note", "request_id": str(uuid.uuid4()), "text": "Valid knowledge", "about": ["../../etc"]},
            {"action": "rate", "request_id": str(uuid.uuid4()), "id": "insight:missing", "verdict": "pin"},
            {"action": "rate", "request_id": str(uuid.uuid4()), "id": self.notes[0], "verdict": "love"},
            {"action": "merge", "request_id": str(uuid.uuid4())},
        ]
        headers = {"Content-Type": "application/json", "Origin": self.url, "X-Improve-Board-Token": token}
        revision = load(self.repo)["revision"]
        for data in bad:
            with self.subTest(data=data):
                code, message = self.error("/api/graph", json.dumps(data).encode(), headers)
                self.assertEqual(code, 400)
                self.assertTrue(message)
        self.assertEqual(load(self.repo)["revision"], revision, "rejected requests change nothing")

    def test_concurrent_retries_of_one_request_apply_once(self):
        token, request_id = self.token(), str(uuid.uuid4())
        text = "Retried knowledge: the docs build needs Node 22 exactly"
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _: self.note(text, request_id, token), range(8)))
        self.assertEqual(len({json.dumps(result, sort_keys=True) for result in results}), 1)
        node = load(self.repo)["nodes"][results[0]["node"]["id"]]
        self.assertEqual(node["reinforced"], 1)

    def test_concurrent_distinct_notes_are_all_kept(self):
        token = self.token()
        texts = [f"Parallel note {index}: shard s{index} requires warmup token w{index}x" for index in range(40)]
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda text: self.note(text, token=token, about=["area:quality"]), texts))
        ids = {result["node"]["id"] for result in results}
        self.assertEqual(len(ids), 40)
        value = load(self.repo)
        self.assertTrue(ids <= set(value["nodes"]))
        self.assertTrue(all(value["nodes"][node_id]["source"] == "user" for node_id in ids))
        check_invariants(self, value)

    def test_every_rating_round_trips_through_the_board(self):
        token, node_id = self.token(), self.notes[5]
        for verdict, field, expected in (("useful", "useful", 1), ("pin", "pinned", True), ("unpin", "pinned", False),
                                         ("archive", "archived", "archived by user"), ("restore", "archived", None),
                                         ("wrong", "archived", "marked wrong by the user")):
            with self.subTest(verdict=verdict):
                result = self.post({"action": "rate", "request_id": str(uuid.uuid4()), "id": node_id,
                                    "verdict": verdict}, token)
                self.assertTrue(result["message"])
                self.assertEqual(load(self.repo)["nodes"][node_id].get(field), expected)

    def test_aliases_resolve_for_rating_and_focus(self):
        with graph.locked(self.repo) as value:
            value["aliases"]["insight:retired"] = self.notes[7]
        self.assertEqual(self.post({"action": "rate", "request_id": str(uuid.uuid4()), "id": "insight:retired",
                                    "verdict": "useful"})["node"]["id"], self.notes[7])
        self.assertEqual(self.get("/api/graph", focus="insight:retired")[0]["focus"], self.notes[7])

    def test_refine_on_a_large_graph_is_quick_and_bounded_history(self):
        token = self.token()
        started = time.perf_counter()
        result = self.post({"action": "refine", "request_id": str(uuid.uuid4())}, token)
        self.assertLess(time.perf_counter() - started, budget(5.0))
        self.assertIn("Refined", result["message"])
        for _ in range(60):
            self.note(f"Bulk note {uuid.uuid4().hex} keeps the op log bounded", token=token)
        value = load(self.repo)
        self.assertLessEqual(len(value["board_ops"]), 50)
        check_invariants(self, value)

    def test_reads_see_writes_immediately(self):
        before = self.get("/api/graph")[0]["revision"]
        summary = self.get("/api/board")[0]["graph"]["revision"]
        result = self.note("Freshness check: written knowledge is readable at once", about=["area:docs"])
        after = self.get("/api/graph", q="freshness written readable")[0]
        self.assertGreater(after["revision"], before)
        self.assertIn(result["node"]["id"], {node["id"] for node in after["nodes"]})
        self.assertGreater(self.get("/api/board")[0]["graph"]["revision"], summary)
        self.assertEqual(self.get("/api/graph/node", id=result["node"]["id"])[0]["node"]["source"], "user")


class DreamEndpointTests(LargeBoardGraph):
    def forget(self, node_id, reason="endpoint test"):
        with graph.locked(self.repo) as value:
            graph.remove_node(value, node_id, reason=reason, op="forgotten")

    def test_history_lists_filters_pages_and_validates(self):
        doomed = self.notes[40:46]
        for node_id in doomed:
            self.forget(node_id, reason=f"cleanup {node_id}")
        listing = self.get("/api/graph/history", limit=3)[0]
        self.assertGreaterEqual(listing["total"], 6)
        self.assertEqual(len(listing["rows"]), 3)
        self.assertEqual(listing["rows"][0]["id"], doomed[-1], "newest first")
        second = self.get("/api/graph/history", limit=3, offset=3)[0]
        self.assertFalse({row["id"] for row in listing["rows"]} & {row["id"] for row in second["rows"]})
        self.assertEqual(self.get("/api/graph/history", q=doomed[0])[0]["rows"][0]["id"], doomed[0])
        self.assertTrue(all(row["type"] == "insight" for row in self.get("/api/graph/history", types="insight")[0]["rows"]))
        self.assertLessEqual(len(self.get("/api/graph/history", limit=5000)[0]["rows"]), 100)
        self.assertEqual(self.error("/api/graph/history?limit=many")[0], 400)

    def test_restore_brings_a_node_back_once_and_validates(self):
        target = self.notes[60]
        links = {key for key, edge in load(self.repo)["edges"].items() if target in (edge["src"], edge["dst"])}
        self.forget(target)
        request = {"action": "restore", "request_id": str(uuid.uuid4()), "id": target}
        token = self.token()
        first = self.post(request, token)
        self.assertEqual(self.post(request, token), first, "a retried restore applies once")
        self.assertEqual(first["node"]["id"], target)
        value = load(self.repo)
        self.assertEqual({key for key, edge in value["edges"].items() if target in (edge["src"], edge["dst"])}, links)
        self.assertEqual(self.get("/api/graph/node", id=target)[0]["node"]["restored"]["by"], "user")
        headers = {"Content-Type": "application/json", "Origin": self.url, "X-Improve-Board-Token": token}
        for data in ({"action": "restore", "request_id": str(uuid.uuid4()), "id": target},
                     {"action": "restore", "request_id": str(uuid.uuid4()), "id": "insight:never"}):
            with self.subTest(data=data):
                code, message = self.error("/api/graph", json.dumps(data).encode(), headers)
                self.assertEqual(code, 400)
                self.assertIn("history", message)

    def test_dream_runs_once_per_request_and_reports(self):
        tick = load(self.repo)["tick"]
        request = {"action": "dream", "request_id": str(uuid.uuid4())}
        token = self.token()
        started = time.perf_counter()
        first = self.post(request, token)
        self.assertLess(time.perf_counter() - started, budget(10))
        self.assertEqual(self.post(request, token), first)
        self.assertEqual(load(self.repo)["tick"], tick + 1)
        self.assertIn("Dreamed:", first["message"])
        view = self.get("/api/graph")[0]
        self.assertEqual(view["dreams"][-1]["tick"], tick + 1)
        self.assertEqual(self.get("/api/board")[0]["graph"]["tick"], tick + 1)

    def test_rejecting_an_inferred_link_from_the_board(self):
        left, right = sorted(self.notes[70:72])
        key = graph.edge_key(left, "co_cited", right)
        with graph.locked(self.repo) as value:
            value["edges"][key] = dict(src=left, rel="co_cited", dst=right, inferred=True, confidence=0.7,
                                       rule="cited-together", generation=1, evidence={"tasks": {"task:r1:t1": "done"}})
        detail = self.get("/api/graph/node", id=left)[0]
        link = next(row for row in detail["links"] if row["id"] == right)
        self.assertEqual((link["inferred"], link["confidence"], link["rule"]), (True, 0.7, "cited-together"))
        self.assertIn("task:r1:t1", link["evidence"])
        token = self.token()
        result = self.post({"action": "reject_link", "request_id": str(uuid.uuid4()), "src": right, "rel": "co_cited",
                            "dst": left, "reason": "unrelated"}, token)
        self.assertTrue(result["removed"])
        value = load(self.repo)
        self.assertNotIn(key, value["edges"])
        self.assertEqual(value["dream"]["rejected"][key]["by"], "user")
        headers = {"Content-Type": "application/json", "Origin": self.url, "X-Improve-Board-Token": token}
        about = next(edge for edge in value["edges"].values() if edge["rel"] == "about")
        for data in ({"action": "reject_link", "request_id": str(uuid.uuid4()), "src": left, "rel": "co_cited"},
                     {"action": "reject_link", "request_id": str(uuid.uuid4()), "src": about["src"], "rel": "about",
                      "dst": about["dst"]},
                     {"action": "reject_link", "request_id": str(uuid.uuid4()), "src": "insight:none", "rel": "co_cited",
                      "dst": left}):
            with self.subTest(data=data):
                self.assertEqual(self.error("/api/graph", json.dumps(data).encode(), headers)[0], 400)


class GraphCacheTests(LargeBoardGraph):
    def test_cached_reads_reuse_unchanged_files_and_reload_changed_ones(self):
        path = graph.graph_path(self.repo)
        first = graph.load_cached(path)
        self.assertIs(graph.load_cached(path), first, "an unchanged file is not parsed again")
        with graph.locked(self.repo) as value:
            graph.add_insight(value, "Cache invalidation test knowledge entry", "fact")
        second = graph.load_cached(path)
        self.assertIsNot(second, first)
        self.assertEqual(second["revision"], first["revision"] + 1)
        self.assertEqual(graph.load_cached(self.repo / ".improve" / "absent.json")["nodes"], {})


class LiveDaemonTests(LargeBoardGraph):
    def test_board_stays_correct_while_the_daemon_writes_every_cycle(self):
        stop, failures, reads = threading.Event(), [], [0]
        rows = list(self.rows)

        def daemon():
            cycle = 0
            while not stop.is_set():
                rows.append(dict(id=f"D{cycle}", title=f"Daemon task {cycle}", area="security", status="done",
                                 paths=[self.files[cycle % 700], self.files[(cycle * 7) % 700]]))
                write_ledger(self.state, rows)
                graph.sync(self.repo, refine_after=cycle % 5 == 4)
                cycle += 1

        def reader(index):
            while not stop.is_set():
                try:
                    path = ("/api/graph", "/api/graph/node?id=area:security", "/api/board",
                            "/api/graph/context?file=" + self.files[index], "/api/graph?q=daemon+task")[index % 5]
                    with OPENER.open(Request(self.url + path), timeout=budget(15)) as response:
                        json.load(response)
                    reads[0] += 1
                except Exception as exc:  # Any failed read is the finding; record it for the assertion.
                    failures.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=daemon)] + [threading.Thread(target=reader, args=(index,)) for index in range(5)]
        for thread in threads:
            thread.start()
        token = self.token()
        written = []
        try:
            for index in range(15):
                written.append(self.note(f"Live-session note {index}: subsystem q{index} needs lock order l{index}",
                                         token=token)["node"]["id"])
                time.sleep(0.1)
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=budget(60))
        self.assertEqual(failures, [])
        self.assertGreater(reads[0], 20)
        value = load(self.repo)
        self.assertTrue(set(written) <= set(value["nodes"]), "board writes survive concurrent daemon syncs")
        self.assertTrue({f"task:live:{row['id']}" for row in rows} <= set(value["nodes"]))
        check_invariants(self, value)


if __name__ == "__main__":
    unittest.main()
