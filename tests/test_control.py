"""Exercise local browser capabilities and durable requests without real agents."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from test_board import LedgerFixture, board
import improve_control as control


class ControlTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.info = board.start_board(self.repo, 0)
        self.url = self.info["url"]
        self.token = self.http("/api/session", headers={"X-Improve-Client": "board"})["token"]

    def http(self, path, data=None, headers=None, raw=None):
        body = raw if raw is not None else json.dumps(data).encode() if data is not None else None
        defaults = {"Origin": self.url, "Content-Type": "application/json", "X-Improve-Board-Token": self.token} if body is not None else {}
        request = Request(self.url + path, data=body, headers={**defaults, **(headers or {})})
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            return json.load(response)

    def rejected(self, path, data=None, code=400, **kwargs):
        with self.assertRaises(HTTPError) as caught:
            self.http(path, data, **kwargs)
        self.assertEqual(caught.exception.code, code)
        message = json.load(caught.exception)["error"]
        caught.exception.close()
        return message

    def seed(self, **updates):
        saved = dict(run_id=uuid.uuid4().hex, started_at=board.stamp(time.time() - 5),
                     deadline=board.stamp(time.time() + 60), engine="codex", phase="working",
                     skill="improve", args="", outcome=None, board_control_version=1, **updates)
        board.atomic_json(self.state / "supervisor.json", saved)
        return saved

    def request_data(self, kind="guidance", **updates):
        return dict(id=str(uuid.uuid4()), run_id=control.run_identity(self.repo), type=kind,
                    text="Focus on keyboard navigation", **updates)

    def test_browser_capability_is_same_origin_and_separate_from_service_stop(self):
        for headers in ({}, {"X-Improve-Client": "board", "Origin": "https://outside.invalid"},
                        {"X-Improve-Client": "board", "Sec-Fetch-Site": "cross-site"},
                        {"X-Improve-Client": "board", "Host": "outside.invalid"}):
            self.rejected("/api/session", code=403, headers=headers)
        self.rejected("/api/stop", {}, code=403)
        self.assertTrue(board.board_status(self.repo)["running"])
        self.assertNotEqual(self.token, board.read_json(self.state / "board.json")["token"])
        payload = self.request_data()
        for headers in ({"Origin": "https://outside.invalid"}, {"Origin": "null"},
                        {"Origin": ""}, {"X-Improve-Board-Token": "wrong"},
                        {"Sec-Fetch-Site": "same-site"}, {"Host": "outside.invalid"}):
            self.rejected("/api/requests", payload, code=403, headers=headers)
        self.assertFalse((self.state / "operator.json").exists())

    def test_pause_resume_stop_preserve_worker_state(self):
        saved = self.seed()
        before = (self.state / "supervisor.json").read_bytes()
        with board.repo_lock(self.state / "daemon.lock"):
            for action, expected in (("pause", True), ("resume", False)):
                self.http("/api/control", dict(action=action, run_id=saved["run_id"]))
                self.assertEqual(control.pause_requested(self.repo, saved["run_id"]), expected)
            self.http("/api/control", dict(action="stop", run_id=saved["run_id"]))
            self.assertTrue((self.state / "stop.request").exists())
            self.assertIn("already pending", self.rejected("/api/control", dict(action="resume", run_id=saved["run_id"])))
        self.assertEqual((self.state / "supervisor.json").read_bytes(), before)

    def test_stale_tabs_inactive_and_legacy_runs_cannot_be_controlled(self):
        saved = self.seed()
        self.rejected("/api/control", dict(action="pause", run_id=saved["run_id"]))
        with board.repo_lock(self.state / "daemon.lock"):
            self.rejected("/api/control", dict(action="pause", run_id="old-run"))
            replacement = {**saved, "run_id": uuid.uuid4().hex}
            board.atomic_json(self.state / "supervisor.json", replacement)
            # Even runs started within the same second have different identities.
            self.rejected("/api/control", dict(action="stop", run_id=saved["run_id"]))
            replacement.pop("board_control_version")
            board.atomic_json(self.state / "supervisor.json", replacement)
            self.assertIn("updated skill", self.rejected("/api/control", dict(action="pause", run_id=replacement["run_id"])))
        self.assertFalse((self.state / "pause.json").exists())
        self.assertFalse((self.state / "stop.request").exists())

    def test_requests_are_idempotent_separate_and_acknowledged_explicitly(self):
        self.rows([dict(id="a", title="Original", status="ready")])
        before = (self.state / "backlog.jsonl").read_bytes()
        payload = self.request_data("task", title="A requested feature", area="features", priority="high")
        first = self.http("/api/requests", payload)
        self.assertEqual(self.http("/api/requests", payload), first)
        self.assertEqual((self.state / "backlog.jsonl").read_bytes(), before)
        self.assertEqual(len(control.request_view(self.repo)), 1)
        self.assertEqual(control.request_view(self.repo)[0]["status"], "pending")
        receipt = control.acknowledge(self.repo, payload["id"], "applied", "Queued user-" + payload["id"])
        self.assertEqual(control.acknowledge(self.repo, payload["id"], "applied", receipt["note"]), receipt)
        self.http("/api/requests", payload)
        with self.assertRaises(control.StateError):
            control.acknowledge(self.repo, payload["id"], "declined", "Changed response")
        self.assertEqual(control.request_view(self.repo)[0]["status"], "applied")
        self.rejected("/api/requests", {**payload, "title": "Different request"})

    def test_concurrent_requests_are_not_lost(self):
        payloads = [self.request_data("task", title=f"Request {i}") for i in range(20)]
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda data: self.http("/api/requests", data), payloads))
        self.assertEqual({row["id"] for row in control.request_view(self.repo)}, {row["id"] for row in payloads})

    def test_task_actions_require_current_unfinished_targets(self):
        self.rows([dict(id="a", status="proposed"), dict(id="b", status="done")])
        payload = self.request_data("decision", task_key="current:a", decision="approve")
        self.http("/api/requests", payload)
        self.assertEqual(control.request_view(self.repo)[0]["expected_status"], "proposed")
        self.assertEqual(board.board_snapshot(self.repo)["tasks"][0]["status"], "proposed")
        for key in ("archived:a", "current:b", "current:missing"):
            self.rejected("/api/requests", self.request_data("priority", task_key=key, priority="high"))
        self.rows([dict(id="a", status="done")])
        self.http("/api/requests", payload)  # Retry after the target changed remains idempotent.
        self.rejected("/api/requests", self.request_data("decision", task_key="current:a", decision="reject"))

    def test_proposal_and_priority_requests_support_bets(self):
        (self.state / "bets.jsonl").write_text(json.dumps(dict(id="b", hypothesis="Faster feed", status="proposed")) + "\n")
        self.http("/api/requests", self.request_data("decision", task_key="current:bet:b", decision="approve"))
        self.http("/api/requests", self.request_data("priority", task_key="current:bet:b", priority="high"))
        self.assertEqual(len(control.request_view(self.repo)), 2)
        self.assertEqual(board.board_snapshot(self.repo)["tasks"][0]["bet_status"], "proposed")

    def test_invalid_and_oversized_payloads_preserve_existing_requests(self):
        self.http("/api/requests", self.request_data())
        before = (self.state / "operator.json").read_bytes()
        for data in ([], {"type": "shell", "command": "unexpected"}, self.request_data("task", title=""),
                     self.request_data("task", title="x", area="unknown"),
                     {**self.request_data(), "text": "x" * 8001},
                     {**self.request_data(), "run_id": "stale"}):
            self.rejected("/api/requests", data)
        self.rejected("/api/requests", raw=b"{" + b" " * 65536)
        self.rejected("/api/requests", raw=b"{invalid")
        self.rejected("/api/requests", self.request_data(), headers={"Content-Type": "text/plain"})
        self.assertEqual((self.state / "operator.json").read_bytes(), before)

    def test_full_length_unicode_guidance_is_preserved(self):
        payload = {**self.request_data(), "text": "界" * 8000}
        self.http("/api/requests", payload)
        self.assertEqual(control.request_view(self.repo)[0]["text"], payload["text"])

    def test_malformed_request_state_disables_controls_without_hiding_tasks(self):
        self.rows([dict(id="visible", status="ready")])
        for raw in ('{truncated', '{"requests":[{"id":"a","type":[]}]}', '{"nested":' + '[' * 1100 + '0' + ']' * 1100 + '}'):
            (self.state / "operator.json").write_text(raw)
            snapshot = self.http("/api/board")
            self.assertEqual(snapshot["tasks"][0]["id"], "visible")
            self.assertFalse(snapshot["controls"]["requests_available"])
            self.assertTrue(snapshot["warnings"])
            self.rejected("/api/requests", self.request_data())
            self.assertEqual((self.state / "operator.json").read_text(), raw)

    def test_new_work_is_closed_at_deadline_and_finalization(self):
        saved = self.seed()
        for changes in ({"deadline": board.stamp(time.time() - 1)}, {"phase": "finalize"}, {"outcome": "completed"}):
            board.atomic_json(self.state / "supervisor.json", {**saved, **changes})
            with board.repo_lock(self.state / "daemon.lock"):
                self.rejected("/api/requests", self.request_data())
                self.rejected("/api/control", dict(action="pause", run_id=saved["run_id"]))

    def test_control_sidecars_do_not_follow_symlinks(self):
        saved = self.seed()
        outside = self.repo / "outside.json"
        outside.write_text('{"private":"untouched"}')
        before = outside.read_bytes()
        with board.repo_lock(self.state / "daemon.lock"):
            for name in ("operator.json", "board-receipts.json", "pause.json", "operator.lock"):
                path = self.state / name
                path.unlink(missing_ok=True)
                path.symlink_to(outside)
                self.rejected("/api/requests", self.request_data())
                path.unlink()
            (self.state / "stop.request").symlink_to(outside)
            self.rejected("/api/control", dict(action="stop", run_id=saved["run_id"]))
        self.assertEqual(outside.read_bytes(), before)

    def test_helper_reads_pending_and_acknowledges_without_task_side_effects(self):
        payload = self.request_data()
        self.http("/api/requests", payload)
        script = Path(board.__file__).with_name("improve_control.py")
        command = [sys.executable, str(script), "--repo", str(self.repo)]
        result = subprocess.run([*command, "pending"], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)["requests"][0]["id"], payload["id"])
        subprocess.run([*command, "ack", "--id", payload["id"], "--status", "declined", "--note", "Outside the current scope"], check=True, capture_output=True)
        result = subprocess.run([*command, "pending"], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)["requests"], [])
        self.assertFalse((self.state / "backlog.jsonl").exists())

    def test_report_route_is_bounded_and_never_follows_symlinks(self):
        self.rejected("/api/report", code=404)
        path = self.state / "final-report.md"
        path.write_text("# Summary\n<script>not executable</script>")
        self.assertIn("<script>", self.http("/api/report")["text"])
        path.write_text("x" * (512 * 1024 + 1))
        self.rejected("/api/report")
        path.unlink()
        path.symlink_to(self.repo / "private-report")
        (self.repo / "private-report").write_text("private")
        self.rejected("/api/report")

    def test_archived_requests_link_only_to_work_in_their_own_run(self):
        request_id = str(uuid.uuid4())
        archive = self.state / "history" / "older-run"
        archive.mkdir(parents=True)
        self.rows([dict(id="same", title="Current task", board_request_id=request_id)], self.state)
        self.rows([dict(id="same", title="Archived task", board_request_id=request_id)], archive)
        (archive / "bets.jsonl").write_text(json.dumps(dict(id="same", hypothesis="Archived bet", status="landed")))
        request = dict(id=request_id, type="task", title="Reusable request", text="", created_at=board.stamp())
        board.atomic_json(self.state / "operator.json", {"requests": [request]})
        board.atomic_json(archive / "operator.json", {"requests": [request,
            dict(id=str(uuid.uuid4()), type="priority", task_key="current:bet:same", expected_status="proposed")]})
        board.atomic_json(archive / "board-receipts.json", {request_id: dict(status="applied", note="Queued archived task", at=board.stamp())})
        data = self.http("/api/board?run=all")
        rows = {row["key"]: row for row in data["requests"]}
        self.assertEqual(rows[f"current:{request_id}"]["task_keys"], ["current:same"])
        old = rows[f"older-run:{request_id}"]
        self.assertEqual(old["task_keys"], ["older-run:same"])
        self.assertEqual(old["response"], "Queued archived task")
        priority = next(row for row in rows.values() if row["type"] == "priority")
        self.assertEqual(priority["task_keys"], ["older-run:bet:same"])
        self.assertEqual(priority["expected_status"], "proposed")
        selected = self.http("/api/board?run=older-run")
        self.assertEqual({row["source_run"] for row in selected["requests"]}, {"older-run"})
        self.assertEqual(len(selected["controls"]["requests"]), 1)
        self.assertEqual(len(self.http("/api/board?run=current")["requests"]), 1)

    def test_bad_archive_does_not_disable_current_requests_or_hide_tasks(self):
        archive = self.state / "history" / "damaged"
        archive.mkdir(parents=True)
        self.rows([dict(id="visible")], archive)
        for raw in ("{partial", '{"requests":[{"id":"bad","type":[]}]}'):
            (archive / "operator.json").write_text(raw)
            snapshot = self.http("/api/board?run=all")
            self.assertTrue(snapshot["controls"]["requests_available"])
            self.assertEqual(snapshot["tasks"][0]["id"], "visible")
            self.assertTrue(any("damaged: Requests:" in warning for warning in snapshot["warnings"]))
            self.http("/api/requests", self.request_data())
        (archive / "operator.json").unlink()
        outside = self.repo / "private.json"
        outside.write_text('{"requests":[]}')
        (archive / "operator.json").symlink_to(outside)
        self.assertTrue(self.http("/api/board?run=all")["warnings"])

    def test_archived_reports_are_selected_without_exposing_arbitrary_paths(self):
        archive = self.state / "history" / "older-run"
        archive.mkdir(parents=True)
        (archive / "final-report.md").write_text("# Archived work")
        (self.state / "final-report.md").write_text("# Current work")
        report = self.http("/api/report?run=older-run")
        self.assertEqual(report, {"text": "# Archived work", "run_id": "older-run"})
        self.assertEqual(self.http("/api/report")["text"], "# Current work")
        runs = self.http("/api/board")["runs"]
        self.assertTrue(all(run["report_available"] for run in runs))
        for selected in ("all", "missing", "..", "..%2F..", "%2Ftmp"):
            self.rejected("/api/report?run=" + selected)
        self.rejected("/api/report?" + "&".join("run=older-run" for _ in range(9)))
        outside = self.repo / "private"
        outside.mkdir()
        (outside / "final-report.md").write_text("private")
        (archive.parent / "linked").symlink_to(outside)
        self.rejected("/api/report?run=linked")
        (archive / "final-report.md").unlink()
        (archive / "final-report.md").symlink_to(outside / "final-report.md")
        self.rejected("/api/report?run=older-run")
        (archive.parent / "current").mkdir()
        (archive.parent / "current" / "final-report.md").write_text("Shadowed")
        self.assertEqual(self.http("/api/report")["text"], "# Current work")

    def test_pending_reader_waits_for_new_run_transaction(self):
        saved = self.seed()
        script = Path(board.__file__).with_name("improve_control.py")
        with control.operator_lock(self.repo):
            process = subprocess.Popen([sys.executable, str(script), "--repo", str(self.repo), "pending"],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.communicate(timeout=0.3)
                replacement = {**saved, "run_id": uuid.uuid4().hex}
                board.atomic_json(self.state / "supervisor.json", replacement)
                board.atomic_json(self.state / "operator.json", {"requests": [dict(
                    id=str(uuid.uuid4()), type="guidance", text="New run only", run_id=replacement["run_id"])]})
            except BaseException:
                process.kill()
                process.communicate()
                raise
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        result = json.loads(stdout)
        self.assertEqual(result["run_id"], replacement["run_id"])
        self.assertEqual(result["requests"][0]["run_id"], result["run_id"])
