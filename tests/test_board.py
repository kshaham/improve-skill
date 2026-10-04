"""Exercise the real local HTTP service with disposable ledgers; no model calls."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import improve_board as board  # noqa: E402


class LedgerFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve board tests ")
        self.repo = Path(self.temp.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.state = self.repo / ".improve"
        self.state.mkdir()

    def tearDown(self):
        board.stop_board(self.repo)
        self.temp.cleanup()

    def rows(self, rows, directory=None):
        (directory or self.state).joinpath("backlog.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows))


class SnapshotTests(LedgerFixture):
    def test_empty_ledger_needs_no_timed_run(self):
        data = board.board_snapshot(self.repo)
        self.assertEqual(data["tasks"], [])
        self.assertEqual(sum(data["counts"].values()), 0)
        self.assertFalse(data["current"]["daemon_running"])

    def test_updates_merge_without_duplicate_cards_or_lost_evidence(self):
        self.rows([{"id": "a", "claim": "Fix it", "evidence": "reproduction", "status": "ready"},
                   {"id": "a", "status": "done", "commit": "abc"}])
        tasks = board.board_snapshot(self.repo)["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["evidence"], "reproduction")
        self.assertEqual(tasks[0]["status"], "done")

    def test_active_item_and_halted_supervisor_override_legacy_status(self):
        self.rows([{"id": "a", "status": "ready"}])
        board.atomic_json(self.state / "run.json", {"active_item": {"id": "a", "last_verification": "pending"}})
        data = board.board_snapshot(self.repo)
        self.assertEqual(data["tasks"][0]["status"], "in_progress")
        self.assertEqual(data["tasks"][0]["last_verification"], "pending")
        board.atomic_json(self.state / "supervisor.json", {"outcome": "halted: interrupted"})
        self.assertEqual(board.board_snapshot(self.repo)["tasks"][0]["status"], "blocked")

    def test_status_aliases_and_unknown_status_are_visible(self):
        self.rows([{"id": "a", "status": "benched"}, {"id": "b", "status": "doing"},
                   {"id": "c", "status": "surprise"}])
        data = board.board_snapshot(self.repo)
        self.assertEqual([task["status"] for task in data["tasks"]], ["blocked", "in_progress", "blocked"])
        self.assertTrue(data["warnings"])

    def test_partial_and_invalid_records_do_not_hide_valid_tasks(self):
        (self.state / "backlog.jsonl").write_text('{"id":"one"}\nnull\n{partial\n{"id":"two"}\n')
        data = board.board_snapshot(self.repo)
        self.assertEqual([task["id"] for task in data["tasks"]], ["one", "two"])
        self.assertEqual(len(data["warnings"]), 2)

    def test_archive_filter_does_not_confuse_reused_ids(self):
        archive = self.state / "history/2026-10-03-run"
        archive.mkdir(parents=True)
        self.rows([{"id": "a", "status": "ready"}])
        self.rows([{"id": "a", "status": "done"}], archive)
        all_tasks = board.board_snapshot(self.repo, "all")["tasks"]
        self.assertEqual(len({task["key"] for task in all_tasks}), 2)
        self.assertEqual(board.board_snapshot(self.repo, archive.name)["counts"]["done"], 1)
        with self.assertRaises(board.StateError):
            board.board_snapshot(self.repo, "../../")

    def test_symlinked_ledgers_and_archives_are_not_read(self):
        outside = self.repo / "outside.jsonl"
        outside.write_text('{"id":"hidden"}\n')
        (self.state / "backlog.jsonl").symlink_to(outside)
        history = self.state / "history"
        history.mkdir()
        (history / "external").symlink_to(self.repo, target_is_directory=True)
        data = board.board_snapshot(self.repo, "all")
        self.assertEqual(data["tasks"], [])
        self.assertEqual(len(data["runs"]), 1)
        self.assertIn("symlink", data["warnings"][0])

    def test_private_state_and_unknown_fields_are_not_exposed(self):
        board.atomic_json(self.state / "intake.json", {"private": "user answer"})
        self.rows([{"id": "a", "internal_only": "private context"}])
        data = json.dumps(board.board_snapshot(self.repo))
        self.assertNotIn("user answer", data)
        self.assertNotIn("private context", data)

    def test_discovery_is_sorted_newest_first(self):
        (self.state / "discovery.jsonl").write_text(
            '{"at":"2026-10-01T01:00:00Z","scope":"old"}\n'
            '{"at":"2026-10-02T01:00:00Z","scope":"new"}\n')
        self.assertEqual(board.board_snapshot(self.repo)["discovery"][0]["scope"], "new")

    def test_terminal_run_does_not_claim_unsettled_work_is_active(self):
        self.rows([{"id": "a", "status": "in_progress", "note": "Checking retry path"}])
        board.atomic_json(self.state / "run.json", {"outcome": "stopped by user"})
        task = board.board_snapshot(self.repo)["tasks"][0]
        self.assertEqual(task["status"], "blocked")
        self.assertIn("Checking retry path", task["note"])
        self.assertIn("before this task was settled", task["note"])

    def test_max_bets_keep_their_measurements_pieces_and_independent_keys(self):
        self.rows([{"id": "b-1", "status": "done"}])
        bets = [{"id": f"b-{i}", "hypothesis": "Reduce latency", "status": status,
                 "spike": {"gain": 3}, "pieces": [{"name": "endpoint", "commit": "abc"}]}
                for i, status in enumerate(("spiking", "placed", "landed", "killed", "proposed"), 1)]
        (self.state / "bets.jsonl").write_text("".join(json.dumps(row) + "\n" for row in bets))
        tasks = board.board_snapshot(self.repo)["tasks"]
        self.assertEqual(len({task["key"] for task in tasks}), 6)
        self.assertEqual([task["status"] for task in tasks[1:]], ["in_progress", "in_progress", "done", "rejected", "proposed"])
        self.assertEqual(tasks[1]["pieces"][0]["commit"], "abc")
        self.assertEqual(tasks[1]["spike"]["gain"], 3)


class ServerTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.info = board.start_board(self.repo, 0)
        self.url = self.info["url"]

    def request(self, path, headers=None, method="GET"):
        return build_opener(ProxyHandler({})).open(
            Request(self.url + path, headers=headers or {}, method=method), timeout=3)

    def test_http_assets_api_and_live_ledger_updates(self):
        for path, mime in (("/", "text/html"), ("/board.js", "text/javascript"), ("/board.css", "text/css")):
            with self.request(path) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(mime, response.headers["Content-Type"])
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
                self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        self.rows([{"id": "a", "title": "<script>alert(1)</script>", "status": "ready"}])
        with self.request("/api/board") as response:
            self.assertEqual(json.load(response)["counts"]["ready"], 1)
        self.rows([{"id": "a", "status": "done"}])
        with self.request("/api/board") as response:
            self.assertEqual(json.load(response)["counts"]["done"], 1)

    def test_private_files_and_path_traversal_are_not_routes(self):
        for path in ("/.improve/intake.json", "/.improve/board.json", "/../README.md", "/%2e%2e/README.md"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as caught:
                self.request(path)
            self.assertEqual(caught.exception.code, 404)
            caught.exception.close()

    def test_host_origin_and_stop_token_are_enforced(self):
        token = board.read_json(self.state / "board.json")["token"]
        for path, method, headers in (
            ("/api/board", "GET", {"Host": "attacker.invalid"}),
            ("/api/stop", "POST", {}),
            ("/api/stop", "POST", {"X-Improve-Board-Token": token, "Origin": "https://attacker.invalid"}),
        ):
            with self.subTest(headers=headers), self.assertRaises(HTTPError) as caught:
                self.request(path, headers, method)
            self.assertEqual(caught.exception.code, 403)
            caught.exception.close()
        self.assertTrue(board.board_status(self.repo)["running"])

    def test_start_reuses_and_stop_never_stops_the_run(self):
        board.atomic_json(self.state / "run.json", {"outcome": None})
        self.assertEqual(board.start_board(self.repo, 0)["pid"], self.info["pid"])
        self.assertEqual(board.stop_board(self.repo), {"running": False})
        self.assertEqual(board.read_json(self.state / "run.json"), {"outcome": None})
        self.assertFalse((self.state / "stop.request").exists())
        self.assertEqual(board.stop_board(self.repo), {"running": False})

    def test_default_port_falls_back_when_busy(self):
        board.stop_board(self.repo)
        with socket.socket() as occupied:
            try:
                occupied.bind(("127.0.0.1", 8765))
                occupied.listen()
            except OSError:
                pass  # A real local listener also exercises the fallback.
            info = board.start_board(self.repo)
            self.assertNotEqual(info["url"], "http://127.0.0.1:8765")

    def test_invalid_run_selector_returns_error_without_stopping_service(self):
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/board?run=missing")
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()
        self.assertTrue(board.board_status(self.repo)["running"])

    def test_concurrent_starts_reuse_one_server(self):
        from concurrent.futures import ThreadPoolExecutor
        board.stop_board(self.repo)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: board.start_board(self.repo, 0), range(4)))
        self.assertEqual(len({result["pid"] for result in results}), 1)


if __name__ == "__main__":
    unittest.main()
