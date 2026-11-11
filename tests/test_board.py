"""Exercise the real local HTTP service with disposable ledgers; no model calls."""
import contextlib
import gzip
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import improve_board as board  # noqa: E402
import improve_control as control  # noqa: E402
import improve_graph  # noqa: E402

GIT_IDENTITY = ["-c", "user.name=Board Tests", "-c", "user.email=board@example.invalid"]


@contextlib.contextmanager
def in_process_server(repo):
    """A board server inside the test process, so module attributes can be monkeypatched."""
    server = board.configure_server(board.BoardServer(("127.0.0.1", 0), board.BoardHandler), repo)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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
    def test_worker_cannot_mark_supervised_run_ended_on_the_board(self):
        run = dict(started_at=board.stamp(time.time() - 10), deadline=board.stamp(time.time() + 60),
                   active_item={"id": "a"}, outcome="completed")
        board.atomic_json(self.state / "run.json", run)
        board.atomic_json(self.state / "supervisor.json", {**run, "outcome": None})
        data = board.board_snapshot(self.repo)
        self.assertIsNone(data["current"]["outcome"])
        self.assertEqual(data["tasks"][0]["status"], "in_progress")
        self.assertTrue(data["warnings"])
        self.assertEqual(board.read_json(self.state / "run.json"), run)

    def test_foreground_premature_completion_is_not_displayed_as_finished(self):
        run = dict(started_at=board.stamp(time.time() - 10), deadline=board.stamp(time.time() + 60), outcome="completed")
        board.atomic_json(self.state / "run.json", run)
        data = board.board_snapshot(self.repo)
        self.assertIsNone(data["current"]["outcome"])
        self.assertIn("Premature completion", data["warnings"][0])

    def test_health_distinguishes_saved_phase_from_daemon_liveness(self):
        control = dict(started_at=board.stamp(time.time() - 10), deadline=board.stamp(time.time() + 60),
                       phase="working", engine="codex", model="test-model", heartbeat_at=board.stamp(),
                       summary_pending=True, consecutive_failures=2, last_error="No new evidence")
        board.atomic_json(self.state / "supervisor.json", control)
        current = board.board_snapshot(self.repo)["current"]
        self.assertFalse(current["daemon_running"])
        self.assertTrue(current["supervised"])
        self.assertTrue(current["summary_pending"])
        self.assertEqual(current["consecutive_failures"], 2)
        self.assertEqual(current["model"], "test-model")
        with board.repo_lock(self.state / "daemon.lock"):
            self.assertTrue(board.board_snapshot(self.repo)["current"]["daemon_running"])

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

    def test_nonfinite_measurements_cannot_poison_the_browser_response(self):
        (self.state / "backlog.jsonl").write_text('{"id":"valid","measurement":1.5}\n' +
            ''.join('{"id":"invalid","measurement":' + value + '}\n'
                    for value in ('NaN', 'Infinity', '-Infinity', '1e999')))
        data = board.board_snapshot(self.repo)
        self.assertEqual([task["id"] for task in data["tasks"]], ["valid"])
        self.assertEqual(len(data["warnings"]), 4)
        json.dumps(data, allow_nan=False)

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
            # An ephemeral listener stands in for the preferred port so a live board is never touched.
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            busy = occupied.getsockname()[1]
            previous = os.environ.get("IMPROVE_BOARD_PORT")
            os.environ["IMPROVE_BOARD_PORT"] = str(busy)
            try:
                info = board.start_board(self.repo)
            finally:
                if previous is None:
                    os.environ.pop("IMPROVE_BOARD_PORT", None)
                else:
                    os.environ["IMPROVE_BOARD_PORT"] = previous
            self.assertNotEqual(urlsplit(info["url"]).port, busy)
            self.assertTrue(board.board_status(self.repo)["running"])

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


class LedgerCacheTests(LedgerFixture):
    def tearDown(self):
        self.temp.cleanup()

    def test_unchanged_file_is_served_from_the_parse_cache(self):
        path = self.state / "backlog.jsonl"
        self.rows([{"id": "a", "status": "ready"}])
        first = board.load_file(self.state, "backlog.jsonl", [], jsonl=True)
        self.assertEqual(first[0]["id"], "a")
        info = os.stat(path)
        # Same size and mtime as the cached parse: the stale value proves the cache was used.
        path.write_text(json.dumps({"id": "b", "status": "ready"}) + "\n")
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(board.load_file(self.state, "backlog.jsonl", [], jsonl=True)[0]["id"], "a")
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
        self.assertEqual(board.load_file(self.state, "backlog.jsonl", [], jsonl=True)[0]["id"], "b")

    def test_cached_warnings_are_replayed_and_the_cache_stays_bounded(self):
        (self.state / "backlog.jsonl").write_text('{"id":"one"}\n{broken\n')
        for _ in range(2):
            warnings = []
            rows = board.load_file(self.state, "backlog.jsonl", warnings, jsonl=True)
            self.assertEqual([row["id"] for row in rows], ["one"])
            self.assertEqual(len(warnings), 1)
        for index in range(board.FILE_CACHE_LIMIT + 10):
            (self.state / f"ledger-{index}.jsonl").write_text('{"id":"x"}\n')
            board.load_file(self.state, f"ledger-{index}.jsonl", [])
        self.assertLessEqual(len(board._file_cache), board.FILE_CACHE_LIMIT)


class SnapshotFieldTests(LedgerFixture):
    def tearDown(self):
        self.temp.cleanup()

    def test_supervisor_health_fields_and_ages_are_exposed_defensively(self):
        now = time.time()
        history = [dict(started_at=board.stamp(now - 600 * i), seconds=120, rc=0, advanced=i % 2 == 0, outcome="ok")
                   for i in range(60)] + ["garbage", {"seconds": "slow", "rc": "x"}]
        control_state = dict(started_at=board.stamp(now - 100), deadline=board.stamp(now + 3600), phase="working",
                             heartbeat_at=board.stamp(now - 12), cycle_started_at=board.stamp(now - 300), child_pid=4242,
                             cycle_timeout_seconds=3600, finish_grace_seconds=120, quarantines=2, recoveries_total=3,
                             crashes=1, limit_waited_seconds=90, next_limit_wait=600, park_until=board.stamp(now + 900),
                             cycle_history=history)
        board.atomic_json(self.state / "supervisor.json", control_state)
        current = board.board_snapshot(self.repo)["current"]
        self.assertEqual(current["child_pid"], 4242)
        self.assertEqual(current["cycle_timeout_seconds"], 3600)
        self.assertEqual(current["finish_grace_seconds"], 120)
        self.assertEqual((current["quarantines"], current["recoveries_total"], current["crashes"]), (2, 3, 1))
        self.assertEqual(current["limit_waited_seconds"], 90)
        self.assertEqual(current["next_limit_wait"], 600)
        self.assertEqual(current["park_until"], control_state["park_until"])
        self.assertIsNone(current["ended_at"])
        self.assertEqual(len(current["cycle_history"]), board.CYCLE_HISTORY_LIMIT)
        self.assertIsNone(current["cycle_history"][-1]["seconds"])
        self.assertIsNone(current["cycle_history"][-1]["rc"])
        self.assertTrue(11 <= current["heartbeat_age_seconds"] <= 15)
        self.assertTrue(299 <= current["cycle_age_seconds"] <= 303)
        json.dumps(current, allow_nan=False)

    def test_missing_supervisor_fields_default_to_null_and_zero(self):
        board.atomic_json(self.state / "supervisor.json", {"phase": "working", "heartbeat_at": "not a stamp"})
        current = board.board_snapshot(self.repo)["current"]
        self.assertIsNone(current["cycle_started_at"])
        self.assertIsNone(current["heartbeat_age_seconds"])
        self.assertIsNone(current["cycle_age_seconds"])
        self.assertEqual(current["cycle_history"], [])
        self.assertEqual(current["quarantines"], 0)

    def test_request_types_are_advertised_and_optional_sections_are_gated(self):
        data = board.board_snapshot(self.repo, include=())
        self.assertEqual(data["controls"]["request_types"], sorted(control.REQUEST_TYPES))
        self.assertIsNone(data["learning"])
        self.assertIsNone(data["graph"])
        full = board.board_snapshot(self.repo)
        self.assertIsNotNone(full["graph"])

    def test_sqlite_errors_in_the_graph_become_a_warning_not_a_failure(self):
        original = improve_graph.load_cached

        def locked(_path):
            raise sqlite3.OperationalError("database is locked")
        improve_graph.load_cached = locked
        try:
            data = board.board_snapshot(self.repo)
        finally:
            improve_graph.load_cached = original
        self.assertIsNone(data["graph"])
        self.assertTrue(any("database is locked" in warning for warning in data["warnings"]))

    def test_etag_ignores_the_moving_clock_but_tracks_content(self):
        board.atomic_json(self.state / "supervisor.json", dict(
            started_at=board.stamp(time.time() - 100), deadline=board.stamp(time.time() + 3600), heartbeat_at=board.stamp()))
        first_snapshot = board.board_snapshot(self.repo)
        first = board.snapshot_etag(first_snapshot)
        time.sleep(1.1)
        second_snapshot = board.board_snapshot(self.repo)
        self.assertNotEqual(second_snapshot["updated_at"], first_snapshot["updated_at"])
        self.assertNotEqual(second_snapshot["current"]["clock"]["remaining_seconds"],
                            first_snapshot["current"]["clock"]["remaining_seconds"])
        self.assertEqual(board.snapshot_etag(second_snapshot), first,
                         "a ticking clock alone does not change the tag")
        # A new supervisor heartbeat is content: the browser must learn the fresh age.
        board.atomic_json(self.state / "supervisor.json", {**board.read_json(self.state / "supervisor.json"),
                                                            "heartbeat_at": board.stamp()})
        with_heartbeat = board.snapshot_etag(board.board_snapshot(self.repo))
        self.assertNotEqual(with_heartbeat, first)
        self.rows([{"id": "a", "status": "ready"}])
        self.assertNotEqual(board.snapshot_etag(board.board_snapshot(self.repo)), with_heartbeat)


class EndpointTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.info = board.start_board(self.repo, 0)
        self.url = self.info["url"]

    def open(self, path, headers=None, method="GET", url=None):
        return build_opener(ProxyHandler({})).open(
            Request((url or self.url) + path, headers=headers or {}, method=method), timeout=3)

    def get(self, path, headers=None, url=None):
        with self.open(path, headers, url=url) as response:
            return json.load(response), response

    def rejected(self, path, headers=None, url=None):
        with self.assertRaises(HTTPError) as caught:
            self.open(path, headers, url=url)
        body = json.load(caught.exception)
        caught.exception.close()
        return caught.exception.code, body

    def test_include_parameter_gates_learning_and_graph(self):
        absent, _ = self.get("/api/board")
        self.assertIsNotNone(absent["graph"], "callers that do not know include= keep the full snapshot")
        gated, _ = self.get("/api/board?include=")
        self.assertIsNone(gated["learning"])
        self.assertIsNone(gated["graph"])
        graph_only, _ = self.get("/api/board?include=graph")
        self.assertIsNotNone(graph_only["graph"])
        self.assertIsNone(graph_only["learning"])

    def test_etag_round_trip_returns_304_until_the_ledger_changes(self):
        self.rows([{"id": "a", "status": "ready"}])
        data, response = self.get("/api/board?include=")
        etag = response.headers["ETag"]
        self.assertTrue(etag.startswith('"'))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        with self.assertRaises(HTTPError) as caught:
            self.open("/api/board?include=", {"If-None-Match": etag})
        self.assertEqual(caught.exception.code, 304)
        self.assertEqual(caught.exception.headers["ETag"], etag)
        caught.exception.close()
        self.rows([{"id": "a", "status": "done"}])
        changed, response = self.get("/api/board?include=", {"If-None-Match": etag})
        self.assertEqual(changed["counts"]["done"], 1)
        self.assertNotEqual(response.headers["ETag"], etag)

    def test_large_bodies_are_gzipped_only_when_accepted(self):
        self.rows([{"id": f"t{i}", "title": "A task title that pads the body " * 3, "status": "ready"} for i in range(40)])
        with self.open("/api/board?include=", {"Accept-Encoding": "gzip, deflate"}) as response:
            self.assertEqual(response.headers["Content-Encoding"], "gzip")
            self.assertEqual(response.headers["Vary"], "Accept-Encoding")
            body = json.loads(gzip.decompress(response.read()))
        self.assertEqual(body["counts"]["ready"], 40)
        with self.open("/api/board?include=") as response:
            self.assertIsNone(response.headers["Content-Encoding"])
            self.assertEqual(json.load(response)["counts"]["ready"], 40)
        with self.open("/api/health", {"Accept-Encoding": "gzip"}) as response:
            self.assertIsNone(response.headers["Content-Encoding"], "small bodies are sent as-is")

    def test_snapshot_answers_while_the_operator_lock_is_held(self):
        self.rows([{"id": "a", "status": "ready"}])
        with control.operator_lock(self.repo):
            started = time.monotonic()
            data, _ = self.get("/api/board?include=")
            self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(data["counts"]["ready"], 1)

    def test_health_reports_snapshot_age_daemon_and_heartbeat(self):
        health, _ = self.get("/api/health")
        self.assertIsNone(health["snapshot_age_seconds"])
        self.assertFalse(health["daemon_running"])
        self.assertIsNone(health["heartbeat_age_seconds"])
        board.atomic_json(self.state / "supervisor.json", {"heartbeat_at": board.stamp(time.time() - 40)})
        self.get("/api/board?include=")
        with board.repo_lock(self.state / "daemon.lock"):
            health, _ = self.get("/api/health")
        self.assertTrue(0 <= health["snapshot_age_seconds"] < 3)
        self.assertTrue(health["daemon_running"])
        self.assertTrue(39 <= health["heartbeat_age_seconds"] <= 43)
        self.assertEqual(health["service"], "improve-board")

    def test_journal_is_served_per_run_bounded_and_never_through_symlinks(self):
        code, body = self.rejected("/api/journal")
        self.assertEqual((code, body["code"]), (404, "journal_missing"))
        (self.state / "journal.md").write_text("# Hour 1\nProgress <b>noted</b>\n")
        data, _ = self.get("/api/journal")
        self.assertEqual(data["text"], "# Hour 1\nProgress <b>noted</b>\n")
        self.assertFalse(data["truncated"])
        archive = self.state / "history/2026-10-01-run"
        archive.mkdir(parents=True)
        (archive / "journal.md").write_text("x" * (board.JOURNAL_LIMIT + 500) + "\nlast line\n")
        data, _ = self.get("/api/journal?run=2026-10-01-run")
        self.assertTrue(data["truncated"])
        self.assertEqual(data["text"], "last line\n")
        self.assertLessEqual(len(data["text"].encode()), board.JOURNAL_LIMIT)
        code, body = self.rejected("/api/journal?run=missing")
        self.assertEqual((code, body["code"]), (400, "unknown_run"))
        (self.state / "journal.md").unlink()
        (self.state / "journal.md").symlink_to(archive / "journal.md")
        code, body = self.rejected("/api/journal")
        self.assertEqual(code, 400)
        self.assertNotIn("last line", json.dumps(body))

    def test_daemon_log_tail_is_bounded_by_lines_and_bytes(self):
        code, body = self.rejected("/api/log")
        self.assertEqual((code, body["code"]), (404, "log_missing"))
        (self.state / "daemon.log").write_text("".join(f"line {i}\n" for i in range(3000)))
        data, _ = self.get("/api/log?lines=3")
        self.assertEqual(data["text"].splitlines(), ["line 2997", "line 2998", "line 2999"])
        self.assertTrue(data["truncated"])
        data, _ = self.get("/api/log?lines=99999")
        self.assertEqual(data["line_count"], board.LOG_LINES_MAX)
        self.assertEqual(len(data["text"].splitlines()), board.LOG_LINES_MAX)
        (self.state / "daemon.log").write_text("".join(f"{i:06d} " + "p" * 200 + "\n" for i in range(3000)))
        data, _ = self.get("/api/log?lines=2000")
        self.assertLess(len(data["text"].encode()), board.LOG_LIMIT)
        self.assertTrue(data["text"].endswith("p" * 200))
        self.assertEqual(self.rejected("/api/log?lines=soon")[0], 400)

    def test_quarantine_refs_are_listed_with_recovery_commands(self):
        empty, _ = self.get("/api/quarantine")
        self.assertEqual(empty["refs"], [])
        (self.repo / "file.txt").write_text("saved work\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "file.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), *GIT_IDENTITY, "commit", "-q", "-m", "quarantine: dirty tree"], check=True)
        commit = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "-C", str(self.repo), "update-ref", "refs/improve/quarantine/2026-11-08T100000Z", "HEAD"], check=True)
        data, _ = self.get("/api/quarantine")
        self.assertEqual(len(data["refs"]), 1)
        ref = data["refs"][0]
        self.assertEqual(ref["ref"], "refs/improve/quarantine/2026-11-08T100000Z")
        self.assertEqual(ref["commit"], commit)
        self.assertEqual(ref["subject"], "quarantine: dirty tree")
        self.assertEqual(ref["recover"], "git checkout refs/improve/quarantine/2026-11-08T100000Z -- .")
        self.assertTrue(ref["date"])

    def test_operational_endpoints_follow_the_host_rule(self):
        for path in ("/api/journal", "/api/log", "/api/quarantine"):
            code, body = self.rejected(path, {"Host": "attacker.invalid"})
            self.assertEqual(code, 403, path)

    def test_error_codes_accompany_messages(self):
        code, body = self.rejected("/api/board?run=missing")
        self.assertEqual((code, body["code"]), (400, "unknown_run"))
        code, body = self.rejected("/api/graph/node?id=insight:does-not-exist")
        self.assertEqual((code, body["code"]), (400, "node_missing"))
        code, body = self.rejected("/no-such-route")
        self.assertEqual((code, body["code"]), (404, "not_found"))


class InProcessServerTests(LedgerFixture):
    def tearDown(self):
        self.temp.cleanup()

    def get(self, url, path):
        with build_opener(ProxyHandler({})).open(Request(url + path), timeout=3) as response:
            return json.load(response), response

    def test_locked_graph_database_keeps_the_board_answering(self):
        original = improve_graph.load_cached

        def locked(_path):
            raise sqlite3.OperationalError("database is locked")
        improve_graph.load_cached = locked
        try:
            with in_process_server(self.repo) as (_, url):
                data, response = self.get(url, "/api/board")
        finally:
            improve_graph.load_cached = original
        self.assertEqual(response.status, 200)
        self.assertIsNone(data["graph"])
        self.assertTrue(any("database is locked" in warning for warning in data["warnings"]))

    def test_unexpected_snapshot_failure_is_a_json_500(self):
        original = board.board_snapshot

        def broken(*_args, **_kwargs):
            raise RuntimeError("simulated fault")
        board.board_snapshot = broken
        try:
            with in_process_server(self.repo) as (_, url):
                with self.assertRaises(HTTPError) as caught:
                    self.get(url, "/api/board")
        finally:
            board.board_snapshot = original
        self.assertEqual(caught.exception.code, 500)
        body = json.load(caught.exception)
        caught.exception.close()
        self.assertEqual(body["code"], "internal")
        self.assertIn("simulated fault", body["error"])

    def test_missing_asset_is_a_json_404(self):
        original = board.ASSETS
        board.ASSETS = self.repo / "no-assets-here"
        try:
            with in_process_server(self.repo) as (_, url):
                with self.assertRaises(HTTPError) as caught:
                    self.get(url, "/board.js")
        finally:
            board.ASSETS = original
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(json.load(caught.exception)["code"], "asset_missing")
        caught.exception.close()


class OptionalRequestTypeTests(LedgerFixture):
    """Plumbing for retry/withdraw; their validation lives in improve_control.prepare_request."""

    def setUp(self):
        super().setUp()
        self.info = board.start_board(self.repo, 0)
        self.url = self.info["url"]
        self.saved = dict(run_id=uuid.uuid4().hex, started_at=board.stamp(time.time() - 5),
                          deadline=board.stamp(time.time() + 600), engine="codex", phase="working",
                          outcome=None, board_control_version=1)
        board.atomic_json(self.state / "supervisor.json", self.saved)
        self.token = self.http("/api/session", headers={"X-Improve-Client": "board"})["token"]

    def http(self, path, data=None, headers=None, url=None, token=None):
        url = url or self.url
        body = json.dumps(data).encode() if data is not None else None
        defaults = ({"Origin": url, "Content-Type": "application/json", "X-Improve-Board-Token": token or self.token}
                    if body is not None else {})
        request = Request(url + path, data=body, headers={**defaults, **(headers or {})})
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            return json.load(response)

    def rejected(self, path, data, **kwargs):
        with self.assertRaises(HTTPError) as caught:
            self.http(path, data, **kwargs)
        body = json.load(caught.exception)
        caught.exception.close()
        return caught.exception.code, body

    def payload(self, kind, **fields):
        return {"id": str(uuid.uuid4()), "run_id": self.saved["run_id"], "type": kind, "text": "", **fields}

    def test_request_types_are_advertised(self):
        types = self.http("/api/board?include=")["controls"]["request_types"]
        self.assertEqual(types, sorted(control.REQUEST_TYPES))
        self.assertIn("retry", types)
        self.assertIn("withdraw", types)

    def test_retry_requeues_only_blocked_current_tasks(self):
        self.rows([{"id": "a", "status": "blocked", "blocked_reason": "Runner offline", "blocked_by": "supervisor",
                    "block_count": 2, "supervisor_note": "Gate runner unreachable"},
                   {"id": "b", "status": "ready"}])
        result = self.http("/api/requests", self.payload("retry", task_key="current:a"))
        self.assertEqual(result["request"]["type"], "retry")
        self.assertEqual(result["request"]["task_key"], "current:a")
        self.assertEqual(result["request"]["expected_status"], "blocked")
        self.assertEqual(self.rejected("/api/requests", self.payload("retry", task_key="current:b"))[0], 400)
        snapshot = self.http("/api/board?include=")
        self.assertEqual(snapshot["requests"][0]["task_keys"], ["current:a"])
        blocked = next(task for task in snapshot["tasks"] if task["id"] == "a")
        self.assertEqual(blocked["blocked_reason"], "Runner offline")
        self.assertEqual(blocked["blocked_by"], "supervisor")
        self.assertEqual(blocked["block_count"], 2)
        self.assertEqual(blocked["supervisor_note"], "Gate runner unreachable")

    def test_withdraw_targets_a_pending_request_and_shows_as_withdrawn(self):
        pending = self.http("/api/requests", self.payload("guidance", text="Look at the checkout"))["request"]
        result = self.http("/api/requests", self.payload("withdraw", target_id=pending["id"]))
        self.assertEqual(result["request"]["type"], "withdraw")
        self.assertEqual(result["request"]["target_id"], pending["id"])
        rows = {row["id"]: row for row in self.http("/api/board?include=")["requests"]}
        self.assertEqual(rows[pending["id"]]["status"], "withdrawn")
        self.assertTrue(rows[pending["id"]]["response"])
        self.assertEqual(rows[result["request"]["id"]]["status"], "pending")
        self.assertEqual(self.rejected("/api/requests", self.payload("withdraw", target_id=str(uuid.uuid4())))[0], 400)
        self.assertEqual(self.rejected("/api/requests", self.payload("withdraw", target_id=pending["id"]))[0], 400)

    def test_optional_types_are_refused_clearly_when_the_runtime_lacks_them(self):
        self.rows([{"id": "a", "status": "blocked"}])
        board.stop_board(self.repo)
        original = control.REQUEST_TYPES
        control.REQUEST_TYPES = frozenset(original - {"retry", "withdraw"})
        try:
            with in_process_server(self.repo) as (_, url):
                token = self.http("/api/session", headers={"X-Improve-Client": "board"}, url=url)["token"]
                self.assertNotIn("retry", self.http("/api/board?include=", url=url)["controls"]["request_types"])
                code, body = self.rejected("/api/requests", self.payload("retry", task_key="current:a"), url=url, token=token)
                self.assertEqual((code, body["code"]), (400, "unsupported_request_type"))
                code, body = self.rejected("/api/requests", self.payload("withdraw", target_id=str(uuid.uuid4())), url=url, token=token)
                self.assertEqual(body["code"], "unsupported_request_type")
                self.assertEqual(len(control.request_view(self.repo)), 0)
        finally:
            control.REQUEST_TYPES = original

if __name__ == "__main__":
    unittest.main()
