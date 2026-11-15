"""Each codebase has its own board, graph, and wiki; the boards know of each other only so that
the usual address can lead to the codebase being improved now."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from support import budget  # noqa: E402  (also points IMPROVE_LEARNING_HOME at a temporary folder)
import improve_board as board  # noqa: E402
import improve_boards as boards  # noqa: E402
from graph_support import graph, make_repo  # noqa: E402

BOARD = ROOT / "assets" / "board"


def write_run(repo, started, deadline, outcome=None):
    state = Path(repo) / ".improve"
    state.mkdir(exist_ok=True)
    (state / "run.json").write_text(json.dumps({"started_at": started, "deadline": deadline, "outcome": outcome}))


class RegistryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.previous = os.environ.get("IMPROVE_LEARNING_HOME")
        os.environ["IMPROVE_LEARNING_HOME"] = str(self.home)
        self.alpha, self.beta = (Path(self.tmp.name) / name for name in ("alpha", "beta"))
        for repo in (self.alpha, self.beta):
            (repo / ".improve").mkdir(parents=True)

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("IMPROVE_LEARNING_HOME", None)
        else:
            os.environ["IMPROVE_LEARNING_HOME"] = self.previous
        self.tmp.cleanup()


class RegistryTests(RegistryCase):
    def test_boards_are_recorded_and_forgotten(self):
        boards.record(self.alpha, 8765, "one")
        boards.record(self.beta, 50123, "two")
        self.assertEqual({path: entry["port"] for path, entry in boards.load().items()},
                         {str(self.alpha): 8765, str(self.beta): 50123})
        boards.forget(self.alpha, "someone-else")
        self.assertIn(str(self.alpha), boards.load(), "only the recorded board may remove its entry")
        boards.forget(self.alpha, "one")
        self.assertEqual(list(boards.load()), [str(self.beta)])

    def test_damage_reads_as_empty_and_is_repaired_by_the_next_board(self):
        self.home.mkdir(parents=True)
        for text in ("{not json", "[]", '{"boards": [1]}', '{"boards": {"/x": {"port": "80", "instance": "a"}}}',
                     '{"boards": {"/x": {"port": 0, "instance": "a"}}}', '{"boards": {"/x": {"port": 80}}}'):
            (self.home / "boards.json").write_text(text)
            self.assertEqual(boards.load(), {}, text)
        boards.record(self.alpha, 8765, "one")
        self.assertEqual(list(boards.load()), [str(self.alpha)])

    def test_a_symlinked_registry_is_never_written_through(self):
        self.home.mkdir(parents=True)
        target = Path(self.tmp.name) / "elsewhere.json"
        target.write_text("keep")
        (self.home / "boards.json").symlink_to(target)
        boards.record(self.alpha, 8765, "one")
        self.assertEqual(target.read_text(), "keep")
        self.assertEqual(boards.load(), {})


class RankTests(RegistryCase):
    def test_a_live_run_outranks_a_finished_one_and_later_beats_earlier(self):
        write_run(self.alpha, "2026-11-14T10:00:00Z", "2026-11-14T13:00:00Z", "completed")
        write_run(self.beta, "2026-11-10T10:00:00Z", "2999-01-01T00:00:00Z")
        self.assertEqual(boards.run_rank(self.alpha), (False, "2026-11-14T10:00:00Z"))
        self.assertEqual(boards.run_rank(self.beta), (True, "2026-11-10T10:00:00Z"))
        self.assertGreater(boards.run_rank(self.beta), boards.run_rank(self.alpha))
        write_run(self.beta, "2026-11-10T10:00:00Z", "2026-11-10T13:00:00Z", "completed")
        self.assertGreater(boards.run_rank(self.alpha), boards.run_rank(self.beta))

    def test_a_run_past_its_deadline_or_with_an_outcome_is_not_live(self):
        write_run(self.alpha, "2026-11-14T10:00:00Z", "2026-11-14T13:00:00Z")
        self.assertFalse(boards.run_rank(self.alpha)[0], "the deadline passed")
        write_run(self.alpha, "2026-11-14T10:00:00Z", "2999-01-01T00:00:00Z", "stopped by user")
        self.assertFalse(boards.run_rank(self.alpha)[0])

    def test_an_early_completed_without_a_supervisor_is_still_live_as_the_board_shows_it(self):
        write_run(self.alpha, "2026-11-14T10:00:00Z", "2999-01-01T00:00:00Z", "completed")
        self.assertTrue(boards.run_rank(self.alpha)[0])
        self.assertIsNone(board.board_snapshot(self.alpha)["current"]["outcome"], "the board agrees")
        (self.alpha / ".improve" / "supervisor.json").write_text(json.dumps(
            {"started_at": "2026-11-14T10:00:00Z", "deadline": "2999-01-01T00:00:00Z", "outcome": "completed"}))
        self.assertFalse(boards.run_rank(self.alpha)[0], "a supervisor's outcome is final")

    def test_no_run_or_a_damaged_one_ranks_last(self):
        self.assertEqual(boards.run_rank(self.alpha), (False, ""))
        (self.alpha / ".improve" / "run.json").write_text("{broken")
        self.assertEqual(boards.run_rank(self.alpha), (False, ""))
        (self.alpha / ".improve" / "run.json").write_text(json.dumps({"started_at": 5, "deadline": []}))
        self.assertEqual(boards.run_rank(self.alpha), (False, ""))

    def test_the_supervisor_record_wins_over_run_json(self):
        write_run(self.alpha, "2026-11-14T10:00:00Z", "2999-01-01T00:00:00Z")
        (self.alpha / ".improve" / "supervisor.json").write_text(json.dumps(
            {"started_at": "2026-11-14T09:00:00Z", "deadline": "2026-11-14T12:00:00Z", "outcome": "completed"}))
        self.assertEqual(boards.run_rank(self.alpha), (False, "2026-11-14T09:00:00Z"))


class ViewTests(RegistryCase):
    def setUp(self):
        super().setUp()
        boards.record(self.alpha, 8765, "one")
        boards.record(self.beta, 50123, "two")
        self.up = lambda path, entry: True

    def test_a_finished_codebase_points_to_the_one_being_improved_and_follows(self):
        write_run(self.alpha, "2026-11-14T10:00:00Z", "2026-11-14T13:00:00Z", "completed")
        write_run(self.beta, "2026-11-14T14:00:00Z", "2999-01-01T00:00:00Z")
        view = boards.view(self.alpha, self.up)
        self.assertEqual(view["active"], {"repo": "beta", "url": "http://127.0.0.1:50123"})
        self.assertTrue(view["follow"])
        self.assertEqual(view["others"], [{"repo": "beta", "url": "http://127.0.0.1:50123", "live": True}])
        mine = boards.view(self.beta, self.up)
        self.assertIsNone(mine["active"], "the codebase being improved leads nowhere")
        self.assertFalse(mine["follow"])
        self.assertEqual(mine["others"], [{"repo": "alpha", "url": "http://127.0.0.1:8765", "live": False}])

    def test_with_both_finished_the_one_run_last_is_shown(self):
        write_run(self.alpha, "2026-11-14T10:00:00Z", "2026-11-14T13:00:00Z", "completed")
        write_run(self.beta, "2026-11-12T10:00:00Z", "2026-11-12T13:00:00Z", "completed")
        self.assertIsNone(boards.view(self.alpha, self.up)["active"])
        view = boards.view(self.beta, self.up)
        self.assertEqual(view["active"]["repo"], "alpha")
        self.assertTrue(view["follow"])

    def test_a_board_whose_own_run_is_live_never_sends_its_page_away(self):
        write_run(self.alpha, "2026-11-14T10:00:00Z", "2999-01-01T00:00:00Z")
        write_run(self.beta, "2026-11-14T14:00:00Z", "2999-01-01T00:00:00Z")
        view = boards.view(self.alpha, self.up)
        self.assertEqual(view["active"]["repo"], "beta", "the later run is offered")
        self.assertFalse(view["follow"])

    def test_a_board_that_no_longer_answers_is_left_out_and_forgotten(self):
        write_run(self.beta, "2026-11-14T14:00:00Z", "2999-01-01T00:00:00Z")
        view = boards.view(self.alpha, lambda path, entry: False)
        self.assertEqual(view, {"others": [], "active": None, "follow": False})
        self.assertEqual(list(boards.load()), [str(self.alpha)])

    def test_a_slow_board_is_left_out_but_not_forgotten(self):
        # One slow health answer deleted a running board for good: nothing recorded it again.
        write_run(self.beta, "2026-11-14T14:00:00Z", "2999-01-01T00:00:00Z")
        view = boards.view(self.alpha, lambda path, entry: None)
        self.assertEqual(view["others"], [])
        self.assertEqual(set(boards.load()), {str(self.alpha), str(self.beta)})

    def test_a_board_records_itself_again_without_rewriting_an_unchanged_entry(self):
        boards.forget(self.beta)
        boards.record(self.beta, 50123, "two")
        self.assertIn(str(self.beta), boards.load())
        before = boards.registry_path().stat().st_mtime_ns
        boards.record(self.beta, 50123, "two")
        self.assertEqual(boards.registry_path().stat().st_mtime_ns, before)

    def test_only_the_usual_address_sends_its_page_on(self):
        write_run(self.alpha, "2026-11-14T10:00:00Z", "2026-11-14T13:00:00Z", "completed")
        write_run(self.beta, "2026-11-14T14:00:00Z", "2999-01-01T00:00:00Z")
        elsewhere = boards.view(self.alpha, self.up, usual_address=False)
        self.assertEqual(elsewhere["active"]["repo"], "beta", "the link is still offered")
        self.assertFalse(elsewhere["follow"], "a board opened at its own address stays on its codebase")
        self.assertTrue(boards.view(self.alpha, self.up, usual_address=True)["follow"])

    def test_the_page_is_never_given_a_full_path(self):
        write_run(self.beta, "2026-11-14T14:00:00Z", "2999-01-01T00:00:00Z")
        self.assertNotIn(self.tmp.name, json.dumps(boards.view(self.alpha, self.up)))


class TwoBoardsTests(unittest.TestCase):
    """Two real boards: each serves only its own codebase, and the finished one names the live one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Resolved, as the command line resolves --repo: a board records the real path.
        root = Path(self.tmp.name).resolve()
        # Its own registry: other tests' boards share the suite's and may leave entries there.
        self.previous = os.environ.get("IMPROVE_LEARNING_HOME")
        os.environ["IMPROVE_LEARNING_HOME"] = str(root / "home")
        self.alpha = make_repo(root / "alpha", ["src/a.py"]).resolve()
        self.beta = make_repo(root / "beta", ["src/b.py"]).resolve()
        write_run(self.alpha, "2026-11-14T10:00:00Z", "2026-11-14T13:00:00Z", "completed")
        write_run(self.beta, "2026-11-14T14:00:00Z", "2999-01-01T00:00:00Z")
        with graph.locked(self.alpha) as value:
            graph.add_insight(value, "Alpha keeps its upload retries in src/a.py", about=["file:src/a.py"], repo=self.alpha)
        self.started = []
        for repo in (self.alpha, self.beta):
            self.started.append((repo, board.start_board(repo, 0)))

    def tearDown(self):
        for repo, _ in self.started:
            board.stop_board(repo)
        if self.previous is None:
            os.environ.pop("IMPROVE_LEARNING_HOME", None)
        else:
            os.environ["IMPROVE_LEARNING_HOME"] = self.previous
        self.tmp.cleanup()

    def get(self, info, path):
        with build_opener(ProxyHandler({})).open(Request(info["url"] + path), timeout=budget(5)) as response:
            return json.load(response)

    def test_the_finished_board_names_the_live_one_and_each_shows_only_its_own_knowledge(self):
        (alpha, alpha_info), (beta, beta_info) = self.started
        mine = self.get(alpha_info, "/api/board")["boards"]
        self.assertEqual(mine["active"], {"repo": "beta", "url": beta_info["url"]})
        self.assertFalse(mine["follow"], "a board on its own port never redirects")
        theirs = self.get(beta_info, "/api/board")["boards"]
        self.assertIsNone(theirs["active"])
        self.assertEqual([row["repo"] for row in theirs["others"]], ["alpha"])
        # Knowledge stays with its codebase.
        alpha_graph = self.get(alpha_info, "/api/graph")
        beta_graph = self.get(beta_info, "/api/graph")
        self.assertIn("Alpha keeps its upload retries", json.dumps(alpha_graph))
        self.assertNotIn("Alpha keeps its upload retries", json.dumps(beta_graph))
        self.assertTrue((alpha / ".improve" / "graph.db").is_file())
        self.assertNotEqual(self.get(alpha_info, "/api/board")["workspace_id"],
                            self.get(beta_info, "/api/board")["workspace_id"])

    def test_a_stopped_board_leaves_the_registry(self):
        (alpha, _), (beta, beta_info) = self.started
        self.assertEqual(set(boards.load()), {str(alpha), str(beta)})
        board.stop_board(alpha)
        self.assertEqual(set(boards.load()), {str(beta)})
        self.assertEqual(self.get(beta_info, "/api/board")["boards"]["others"], [])


class UsualAddressTests(unittest.TestCase):
    def test_the_server_follows_only_from_the_preferred_port(self):
        server = (ROOT / "scripts" / "improve_board.py").read_text()
        self.assertIn("usual_address=server.server_port == preferred_port()", server)
        self.assertIn("improve_boards.record(server.repo, server.server_port, server.instance)", server)

    def test_a_refused_connection_is_gone_and_anything_else_is_unknown(self):
        entry = {"port": 1, "instance": "x"}  # Nothing listens on port 1.
        self.assertIs(boards.running("/nowhere", entry), False)


class PageTests(unittest.TestCase):
    def test_the_page_follows_only_local_boards_and_lets_the_reader_stay(self):
        script = (BOARD / "boards.js").read_text()
        self.assertIn("/^http:\\/\\/127\\.0\\.0\\.1:\\d{1,5}$/", script, "only a local board address is ever opened")
        self.assertIn('new URLSearchParams(location.search).has("stay")', script)
        self.assertIn("if (boards.follow && !boardStay()) location.replace(sameViewOn(active.url, data.repo));", script)
        self.assertIn('"?from=" + encodeURIComponent(from)', script, "the destination is told where the reader came from")
        page = (BOARD / "index.html").read_text()
        for marker in ('id="active-board"', 'id="arrived-board"', 'id="other-boards"', 'src="/boards.js"'):
            self.assertIn(marker, page)
        server = (ROOT / "scripts" / "improve_board.py").read_text()
        self.assertIn('"/boards.js": ("boards.js", "text/javascript; charset=utf-8")', server)

    def test_a_page_whose_address_now_serves_another_codebase_reloads(self):
        self.assertIn("location.reload()", (BOARD / "boards.js").read_text())


if __name__ == "__main__":
    unittest.main()
