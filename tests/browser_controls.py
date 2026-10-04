"""Control and request UI checks called by browser_board.py; no model launches."""
import json
from pathlib import Path
import shutil
import time
import uuid

from playwright.sync_api import expect

import improve_board as board
import improve_control as controls


def check_controls(page, repo, tasks, write_tasks, artifacts):
    state = repo / ".improve"
    tasks[:] = [dict(id="proposal", title="Refresh the task hierarchy", area="ui", status="proposed")]
    write_tasks()
    page.set_viewport_size({"width": 1600, "height": 1050})
    page.locator("#board-tab").click()
    page.locator("#refresh").click()
    expect(page.locator("#pause-run")).to_be_disabled()
    expect(page.locator("#new-task")).to_be_enabled()
    page.locator("#new-task").click()
    page.locator("#request-task-title").fill("Add keyboard shortcuts")
    page.locator("#request-area").select_option("ui")
    page.locator("#request-priority").select_option("high")
    page.locator("#request-text").fill("Support keyboard navigation and keep focus visible.")
    page.locator("#request-submit").click()
    expect(page.locator("#request-dialog")).not_to_be_visible()
    expect(page.locator("#requests-tab")).to_have_attribute("aria-selected", "true")
    expect(page.locator(".request-item")).to_have_count(1)
    expect(page.locator(".request-badge")).to_have_text("Pending")
    request = controls.request_view(repo)[0]
    assert len(board.board_snapshot(repo)["tasks"]) == 1, "Saving a request must not write the task ledger"
    tasks.append(dict(id="user-" + request["id"], board_request_id=request["id"], title=request["title"],
                      area=request["area"], status="ready", user_priority="high"))
    write_tasks()
    controls.acknowledge(repo, request["id"], "applied", "Queued a task with keyboard acceptance checks.")
    page.locator("#refresh").click()
    expect(page.locator(".request-badge")).to_have_text("Applied")
    page.locator(".request-item summary").click()
    expect(page.locator(".request-body")).to_contain_text("keyboard acceptance checks")
    page.locator(".request-task-link").click()
    expect(page.locator("#detail-title")).to_have_text("Add keyboard shortcuts")
    expect(page.locator("#detail-meta")).to_contain_text("Queued")
    page.keyboard.press("Escape")
    page.locator("#send-guidance").click()
    page.locator("#request-text").fill("Prioritize mobile usability.\n<img src=x onerror=alert(1)>")
    page.evaluate("refresh()")
    expect(page.locator("#request-text")).to_have_value("Prioritize mobile usability.\n<img src=x onerror=alert(1)>")
    page.locator("#request-submit").click()
    expect(page.locator(".request-item")).to_have_count(2)
    page.locator(".request-item summary").first.click()
    assert page.locator(".request-item img").count() == 0

    # A changed run rejects stale drafts; no silent delivery into the replacement run.
    page.locator("#new-task").click()
    page.locator("#request-task-title").fill("Keep this unsent draft")
    saved = dict(run_id=uuid.uuid4().hex, started_at=board.stamp(time.time() - 10),
                 deadline=board.stamp(time.time() + 120), phase="working", engine="codex", board_control_version=1)
    board.atomic_json(state / "supervisor.json", saved)
    page.locator("#request-submit").click()
    expect(page.locator("#request-error")).to_contain_text("current run changed")
    expect(page.locator("#request-task-title")).to_have_value("Keep this unsent draft")
    page.locator('[data-close="request-dialog"]').first.click()
    page.locator("#refresh").click()

    with board.repo_lock(state / "daemon.lock"):
        page.locator("#refresh").click()
        expect(page.locator("#pause-run")).to_be_enabled()
        page.locator("#pause-run").click()
        expect(page.locator("#pause-run")).to_have_text("Resume run")
        expect(page.locator("#control-state")).to_have_text("Pause requested")
        assert controls.pause_requested(repo, saved["run_id"])
        board.atomic_json(state / "supervisor.json", {**saved, "phase": "paused"})
        page.locator("#refresh").click()
        expect(page.locator("#control-state")).to_have_text("Paused")
        page.locator("#pause-run").click()
        expect(page.locator("#pause-run")).to_have_text("Pause after cycle")
        assert not controls.pause_requested(repo, saved["run_id"])
        assert board.read_json(state / "supervisor.json")["deadline"] == saved["deadline"]
        board.atomic_json(state / "supervisor.json", saved)
        page.locator("#refresh").click()
        page.locator("#board-tab").click()
        page.locator('[data-key="current:user-' + request["id"] + '"]').click()
        page.locator("#change-priority").click()
        page.locator("#request-priority").select_option("low")
        page.locator("#request-submit").click()
        expect(page.locator(".request-item")).to_have_count(3)
        page.locator("#board-tab").click()
        page.locator('[data-key="current:proposal"]').click()
        page.locator("#approve-proposal").click()
        page.locator("#request-text").fill("Approved for the current task board.")
        page.locator("#request-submit").click()
        expect(page.locator(".request-item")).to_have_count(4)
        assert controls.request_view(repo)[-1]["decision"] == "approve"
        page.screenshot(path=str(artifacts / "controls-requests-desktop.png"), full_page=True)
        page.locator("#request-status").select_option("pending")
        expect(page.locator(".request-item")).to_have_count(3)
        with page.expect_download() as download:
            page.locator("#export").click()
        exported = json.loads(Path(download.value.path()).read_text())
        assert len(exported["requests"]) == 3
        page.locator("#request-status").select_option("all")
        # Simulate a saved request whose HTTP response was lost. Retry must not duplicate it.
        def lose_response(route):
            route.fetch()
            route.abort()
        page.route("**/api/requests", lose_response)
        page.locator("#send-guidance").click()
        page.locator("#request-text").fill("Keep this retry idempotent")
        page.locator("#request-submit").click()
        expect(page.locator("#request-error")).to_be_visible()
        assert len(controls.request_view(repo)) == 5
        page.unroute("**/api/requests", lose_response)
        page.locator("#request-submit").click()
        expect(page.locator("#request-dialog")).not_to_be_visible()
        assert len(controls.request_view(repo)) == 5
        page.locator("#stop-run").click()
        page.locator('#stop-dialog [data-close]').last.click()
        assert not (state / "stop.request").exists()
        page.locator("#stop-run").click()
        page.locator("#confirm-stop").click()
        expect(page.locator("#stop-dialog")).not_to_be_visible()
        expect(page.locator("#control-state")).to_have_text("Stopping")
        expect(page.locator("#pause-run")).to_be_disabled()
        assert (state / "stop.request").exists()
        assert board.board_status(repo)["running"]

    board.atomic_json(state / "supervisor.json", {**saved, "outcome": "stopped by user", "summary_pending": True})
    (state / "final-report.md").write_text("# Saved results\n<script>alert('test')</script>\nKeyboard task queued, implementation pending.")
    page.locator("#refresh").click()
    expect(page.locator("#open-report")).to_be_enabled()
    page.locator("#open-report").click()
    expect(page.locator("#report-text")).to_contain_text("<script>")
    assert page.locator("#report-text script").count() == 0
    expect(page.locator("#report-note")).to_contain_text("retry is pending")
    with page.expect_download() as download:
        page.locator("#download-report").click()
    assert "Keyboard task queued" in Path(download.value.path()).read_text()
    page.keyboard.press("Escape")
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(path=str(artifacts / "controls-requests-mobile.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Controls overflow the mobile viewport"
    page.locator("#new-task").click()
    page.screenshot(path=str(artifacts / "controls-form-mobile.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Request form overflows mobile viewport"
    page.keyboard.press("Escape")
    page.locator("#requests-tab").focus()
    page.keyboard.press("Home")
    expect(page.locator("#board-tab")).to_have_attribute("aria-selected", "true")
    page.keyboard.press("End")
    expect(page.locator("#requests-tab")).to_have_attribute("aria-selected", "true")
    # Request history is also bounded and its export includes all matching pages.
    with controls.operator_lock(repo):
        for i in range(21):
            controls.queue_request(repo, dict(id=str(uuid.uuid4()), run_id=saved["run_id"],
                                   type="guidance", text=f"Additional guidance {i}"), [])
    page.locator("#refresh").click()
    expect(page.locator(".request-item")).to_have_count(20)
    page.locator("#request-next").click()
    expect(page.locator(".request-item")).to_have_count(6)
    with page.expect_download() as download:
        page.locator("#export").click()
    assert len(json.loads(Path(download.value.path()).read_text())["requests"]) == 26
    page.locator("#request-status").select_option("applied")
    expect(page.locator(".request-item")).to_have_count(1)
    expect(page.locator("#request-prev")).to_be_disabled()
    check_archived_requests(page, repo, artifacts)


def check_archived_requests(page, repo, artifacts):
    state = repo / ".improve"
    archive = state / "history" / "archive-browser"
    archive.mkdir(parents=True, exist_ok=True)
    for name in ("operator.json", "board-receipts.json", "backlog.jsonl", "final-report.md"):
        shutil.copy2(state / name, archive / name)
    (archive / "final-report.md").write_text("# Archived keyboard results\n<script>plain text</script>")
    archived = [json.loads(line) for line in (archive / "backlog.jsonl").read_text().splitlines()]
    for row in archived:
        row.update(status="done", title="Archived " + row["title"])
    (archive / "backlog.jsonl").write_text("".join(json.dumps(row) + "\n" for row in archived))
    receipts = json.loads((archive / "board-receipts.json").read_text())
    next(iter(receipts.values()))["note"] = "Archived keyboard decision"
    board.atomic_json(archive / "board-receipts.json", receipts)
    page.locator("#refresh").click()
    page.locator("#run").select_option("all")
    expect(page.locator(".request-item")).to_have_count(2)
    # A response-only match searches archived receipts and exports that same subset.
    page.locator("#search").fill("Archived keyboard decision")
    expect(page.locator(".request-item")).to_have_count(1)
    page.locator(".request-item summary").click()
    expect(page.locator(".request-task-link")).to_have_text("Done · Archived Add keyboard shortcuts")
    with page.expect_download() as download:
        page.locator("#export").click()
    exported = json.loads(Path(download.value.path()).read_text())["requests"]
    assert len(exported) == 1 and exported[0]["source_run"] == "archive-browser"
    page.locator(".request-task-link").click()
    expect(page.locator("#detail-title")).to_have_text("Archived Add keyboard shortcuts")
    expect(page.locator("#task-controls")).not_to_be_visible()
    page.keyboard.press("Escape")
    page.locator("#search").fill("")
    page.locator("#run").select_option("archive-browser")
    expect(page.locator(".request-item")).to_have_count(1)
    page.locator("#request-status").select_option("pending")
    expect(page.locator(".request-item")).to_have_count(20)
    page.locator(".request-item summary").first.click()
    expect(page.locator(".request-body").first).to_contain_text("will not be applied to the current run")
    page.locator("#request-status").select_option("all")
    page.locator("#request-next").click()
    first = page.locator(".request-item").first.get_attribute("data-id")
    page.locator(".request-item summary").first.click()
    # Refresh keeps both the anchored page and expanded row even after a new arrival.
    saved_requests = json.loads((archive / "operator.json").read_text())
    saved_requests["requests"].append(dict(id=str(uuid.uuid4()), type="guidance", text="Arrived later", created_at=board.stamp(time.time() + 10)))
    board.atomic_json(archive / "operator.json", saved_requests)
    page.locator("#refresh").click()
    expect(page.locator(".request-item").first).to_have_attribute("data-id", first)
    expect(page.locator(".request-item").first).to_have_attribute("open", "")
    page.locator("#search").fill("no matching phrase")
    expect(page.locator("#requests-empty")).to_contain_text("No requests match")
    expect(page.locator("#request-prev")).to_be_disabled()
    page.locator("#search").fill("Archived keyboard decision")
    page.set_viewport_size({"width": 1600, "height": 1050})
    page.screenshot(path=str(artifacts / "archived-requests-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=str(artifacts / "archived-requests-mobile.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#open-report").click()
    expect(page.locator("#report-run")).to_have_value("archive-browser")
    expect(page.locator("#report-text")).to_contain_text("Archived keyboard results")
    assert page.locator("#report-text script").count() == 0
    with page.expect_download() as download:
        page.locator("#download-report").click()
    assert download.value.suggested_filename == "improve-archive-browser-report.md"
    assert "Archived keyboard results" in Path(download.value.path()).read_text()
    page.locator("#report-run").select_option("current")
    expect(page.locator("#report-text")).to_contain_text("Keyboard task queued")
    # A late response from a previous selection must not replace the selected report.
    delayed = []
    page.route("**/api/report?run=archive-browser", lambda route: delayed.append(route))
    page.locator("#report-run").select_option("archive-browser")
    expect(page.locator("#report-text")).to_have_text("Loading report…")
    page.locator("#report-run").select_option("current")
    expect(page.locator("#report-text")).to_contain_text("Keyboard task queued")
    assert delayed
    delayed[0].fulfill(json={"text": "Late archive", "run_id": "archive-browser"})
    page.unroute("**/api/report?run=archive-browser")
    page.wait_for_timeout(100)
    expect(page.locator("#report-text")).to_contain_text("Keyboard task queued")
    page.keyboard.press("Escape")
    # Submitting from an archive is explicitly current-run work and reveals the new request.
    page.locator("#send-guidance").click()
    page.locator("#request-text").fill("Submitted while browsing an archive")
    page.locator("#request-submit").click()
    expect(page.locator("#request-dialog")).not_to_be_visible()
    expect(page.locator("#run")).to_have_value("current")
    expect(page.locator("#search")).to_have_value("")
    expect(page.locator(".request-item").first).to_contain_text("Submitted while browsing an archive")
    assert len(controls.request_view_at(archive)) == 27
    # Archives remain accessible even if the current run has no report.
    (state / "final-report.md").unlink()
    page.locator("#refresh").click()
    expect(page.locator("#open-report")).to_be_enabled()
    page.locator("#open-report").click()
    expect(page.locator("#report-run")).to_have_value("archive-browser")
    expect(page.locator("#report-text")).to_contain_text("Archived keyboard results")
    page.screenshot(path=str(artifacts / "archived-report-mobile.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.keyboard.press("Escape")
