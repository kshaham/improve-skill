"""Board server fixes from the 2026-11-12 review: liveness outside the snapshot ETag, quarantine
commands safe to paste, a parse cache that fits every run, cross-site reads refused, and a
report endpoint that cannot wait forever on the operator lock."""
import contextlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import budget  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import improve_board as board  # noqa: E402
import improve_control as control  # noqa: E402

GIT_IDENTITY = ["-c", "user.name=Board Tests", "-c", "user.email=board@example.invalid"]
OPENER = build_opener(ProxyHandler({}))


@contextlib.contextmanager
def serving(repo):
    """An in-process board server, so module attributes can be patched for one test."""
    server = board.configure_server(board.BoardServer(("127.0.0.1", 0), board.BoardHandler), repo)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=budget(2))


class BoardFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve board hardening ")
        self.repo = Path(self.temp.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.state = self.repo / ".improve"
        self.state.mkdir()
        self.server = serving(self.repo)
        self.url = self.server.__enter__()

    def tearDown(self):
        self.server.__exit__(None, None, None)
        self.temp.cleanup()

    def open(self, path, headers=None):
        return OPENER.open(Request(self.url + path, headers=headers or {}), timeout=budget(5))

    def get(self, path, headers=None):
        with self.open(path, headers) as response:
            return json.load(response), response.headers

    def failure(self, path, headers=None):
        with self.assertRaises(HTTPError) as caught:
            self.open(path, headers)
        error = caught.exception
        body = error.read()
        error.close()
        return error.code, (json.loads(body) if body else {}), error.headers

    def supervisor(self, **fields):
        now = time.time()
        value = {"run_id": "run-live", "started_at": board.stamp(now - 600), "deadline": board.stamp(now + 3600),
                 "phase": "paused", "board_control_version": 1, **fields}
        board.atomic_json(self.state / "supervisor.json", value)
        return value


class LivenessTests(BoardFixture):
    def test_a_paused_run_rewriting_its_heartbeat_answers_304(self):
        self.supervisor(heartbeat_at=board.stamp(time.time() - 30))
        (self.state / "backlog.jsonl").write_text(json.dumps({"id": "a", "title": "Task", "status": "ready"}) + "\n")
        _, headers = self.get("/api/board?include=")
        etag = headers["ETag"]
        # The pause loop saves supervisor.json every five seconds with only a new heartbeat.
        fresh = board.stamp(time.time() - 1)
        self.supervisor(heartbeat_at=fresh)
        code, _, headers = self.failure("/api/board?include=", {"If-None-Match": etag})
        self.assertEqual(code, 304)
        self.assertEqual(headers["ETag"], etag)
        health, _ = self.get("/api/health")
        self.assertEqual(health["heartbeat_at"], fresh)
        self.assertLessEqual(health["heartbeat_age_seconds"], 1 + budget(3))
        self.assertEqual(health["control_id"], "run-live")
        # Real content changes still change the tag.
        self.supervisor(heartbeat_at=fresh, phase="between-cycles")
        _, headers = self.get("/api/board?include=", {"If-None-Match": etag})
        self.assertNotEqual(headers["ETag"], etag)

    def test_health_reports_the_running_cycle_and_tolerates_missing_state(self):
        empty, _ = self.get("/api/health")
        self.assertIsNone(empty["control_id"])
        self.assertIsNone(empty["cycle_age_seconds"])
        started = board.stamp(time.time() - 120)
        self.supervisor(heartbeat_at=board.stamp(), cycle_started_at=started, phase="working")
        health, _ = self.get("/api/health")
        self.assertEqual(health["cycle_started_at"], started)
        self.assertGreaterEqual(health["cycle_age_seconds"], 120)
        self.assertLessEqual(health["cycle_age_seconds"], 120 + budget(3))
        (self.state / "supervisor.json").write_text("[not an object")
        health, _ = self.get("/api/health")
        self.assertEqual(health["service"], "improve-board", "identity answers even when liveness cannot be read")
        self.assertIsNone(health["heartbeat_at"])


class QuarantineTests(BoardFixture):
    def commit(self):
        (self.repo / "file.txt").write_text("saved work\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "file.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), *GIT_IDENTITY, "commit", "-q", "-m", "quarantine: dirty tree"],
                       check=True)

    def test_only_supervisor_named_refs_are_listed_and_commands_are_quoted(self):
        self.commit()
        valid = "refs/improve/quarantine/2026-11-12T10-30-00Z-cycle-4-between"
        hostile = ["refs/improve/quarantine/$(touch${IFS}pwned)",
                   "refs/improve/quarantine/2026-11-12T10-30-00Z-x`id`",
                   "refs/improve/quarantine/notes"]
        for ref in [valid, *hostile]:
            subprocess.run(["git", "-C", str(self.repo), "update-ref", ref, "HEAD"], check=True)
        data, _ = self.get("/api/quarantine")
        self.assertEqual([row["ref"] for row in data["refs"]], [valid])
        self.assertEqual(data["hidden"], len(hostile))
        self.assertNotIn("$(", json.dumps(data))
        self.assertNotIn("`", json.dumps(data))
        self.assertEqual(shlex.split(data["refs"][0]["recover"]), ["git", "checkout", valid, "--", "."])

    def test_recovery_command_is_shell_quoted_even_for_a_matching_name(self):
        self.commit()
        commit = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True,
                                capture_output=True, text=True).stdout.strip()
        original = board.git

        def listing(repo, *args):
            if args and args[0] == "for-each-ref":
                # A name the pattern accepts is still passed through shlex.quote.
                return f"refs/improve/quarantine/2026-11-12T10-30-00Z-a.b_c\t{commit}\t2026-11-12T10:30:00Z\tsubject"
            return original(repo, *args)
        board.git = listing
        try:
            data = board.quarantine_refs(self.repo)
        finally:
            board.git = original
        ref = "refs/improve/quarantine/2026-11-12T10-30-00Z-a.b_c"
        self.assertEqual(data["refs"][0]["recover"], f"git checkout {shlex.quote(ref)} -- .")


class ParseCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve board cache ")
        self.repo = Path(self.temp.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.state = self.repo / ".improve"
        self.state.mkdir()
        with board._file_cache_lock:
            board._file_cache.clear()
        board.size_file_cache(0)

    def tearDown(self):
        with board._file_cache_lock:
            board._file_cache.clear()
        board.size_file_cache(0)
        self.temp.cleanup()

    def counting(self):
        calls = []
        original = board.parse_ledger_file

        def parse(*args, **kwargs):
            calls.append(args[1])
            return original(*args, **kwargs)
        board.parse_ledger_file = parse
        self.addCleanup(setattr, board, "parse_ledger_file", original)
        return calls

    def test_an_unchanged_poll_over_forty_runs_parses_nothing(self):
        rows = "".join(json.dumps({"id": f"T{index}", "title": f"Task {index}", "status": "done"}) + "\n"
                       for index in range(20))
        for number in range(40):
            directory = self.state / "history" / f"2026-11-{number:03d}"
            directory.mkdir(parents=True)
            fields = {"run_id": f"r{number}", "started_at": "2026-11-01T00:00:00Z",
                      "deadline": "2026-11-02T00:00:00Z", "outcome": "completed"}
            (directory / "run.json").write_text(json.dumps(fields))
            (directory / "supervisor.json").write_text(json.dumps(fields))
            (directory / "backlog.jsonl").write_text(rows)
        (self.state / "run.json").write_text(json.dumps({"run_id": "now", "started_at": "2026-11-12T00:00:00Z",
                                                         "deadline": "2026-11-13T00:00:00Z"}))
        calls = self.counting()
        for selected in ("all", "current", "all"):
            board.board_snapshot(self.repo, selected, ())
        calls.clear()
        snapshot = board.board_snapshot(self.repo, "all", ())
        self.assertEqual(calls, [], "every ledger came from the cache")
        self.assertEqual(len(snapshot["tasks"]), 800)
        self.assertGreaterEqual(board._file_cache_capacity, board.LEDGER_FILES_PER_RUN * 41)

    def test_eviction_drops_the_least_recently_used_parse(self):
        names = [f"ledger-{index}.json" for index in range(board.FILE_CACHE_LIMIT + 1)]
        for name in names:
            (self.state / name).write_text('{"id": "x"}')
        for name in names[:-1]:
            board.load_file(self.state, name, [])
        board.load_file(self.state, names[0], [])  # the oldest entry is used again
        board.load_file(self.state, names[-1], [])  # one over capacity: evict one
        self.assertEqual(len(board._file_cache), board.FILE_CACHE_LIMIT)
        calls = self.counting()
        board.load_file(self.state, names[0], [])
        self.assertEqual(calls, [], "a recently used parse survives eviction")
        board.load_file(self.state, names[1], [])
        self.assertEqual(calls, [names[1]], "the least recently used parse was the one evicted")


class CrossSiteTests(BoardFixture):
    def test_cross_site_and_same_site_reads_are_refused_on_every_api_path(self):
        (self.state / "final-report.md").write_text("# Report\n")
        paths = ("/api/board", "/api/health", "/api/report", "/api/graph", "/api/graph/questions",
                 "/api/journal", "/api/log", "/api/quarantine", "/api/session")
        for site in ("cross-site", "same-site"):
            for path in paths:
                with self.subTest(site=site, path=path):
                    code, body, _ = self.failure(path, {"Sec-Fetch-Site": site, "X-Improve-Client": "board"})
                    self.assertEqual(code, 403)
                    self.assertTrue(body.get("error"))
        for site in ("same-origin", "none"):
            with self.subTest(site=site):
                data, _ = self.get("/api/report", {"Sec-Fetch-Site": site})
                self.assertEqual(data["text"], "# Report\n")
        with self.open("/", {"Sec-Fetch-Site": "cross-site"}) as response:
            self.assertEqual(response.status, 200, "following a link to the board still opens it")

    def test_report_gives_up_on_a_held_operator_lock(self):
        (self.state / "final-report.md").write_text("# Report\n")
        original = board.REPORT_LOCK_SECONDS
        board.REPORT_LOCK_SECONDS = 0.3
        try:
            with control.operator_lock(self.repo):
                started = time.monotonic()
                code, body, _ = self.failure("/api/report")
                elapsed = time.monotonic() - started
        finally:
            board.REPORT_LOCK_SECONDS = original
        self.assertEqual((code, body["code"]), (503, "busy"))
        self.assertGreaterEqual(elapsed, 0.3)
        self.assertLess(elapsed, budget(3))
        data, _ = self.get("/api/report")
        self.assertEqual(data["text"], "# Report\n", "the lock is free again once the holder leaves")

    def test_bounded_lock_refuses_a_non_regular_lock_file(self):
        os.mkfifo(self.state / "operator.lock")
        with self.assertRaises(board.StateError):
            with board.bounded_operator_lock(self.repo, 0.1):
                pass


if __name__ == "__main__":
    unittest.main()
