"""Control and request UI checks called by browser_board.py; no model launches."""
import json
from pathlib import Path
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
