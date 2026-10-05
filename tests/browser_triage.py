"""Bulk triage, task notes and overview interactions without model launches."""
import json
from pathlib import Path
import shutil
import time
import uuid

from playwright.sync_api import expect

import improve_board as board
import improve_control as controls


def check_triage(page, repo, tasks, write_tasks, artifacts):
    state = repo / ".improve"
    run = dict(run_id=uuid.uuid4().hex, started_at=board.stamp(), deadline=board.stamp(time.time() + 300), engine="codex")
    board.atomic_json(state / "supervisor.json", run)
    for name in ("operator.json", "board-receipts.json", "stop.request", "pause.json"):
        (state / name).unlink(missing_ok=True)
    tasks[:] = [dict(id=f"triage-{i:02}", title=f"Task {i:02}", status="ready", area="quality",
                    updated_at=board.stamp(1700000000 + i)) for i in range(60)]
    tasks.extend([dict(id="done", title="Verified result", status="done", area="ui", verification="Browser check passed", completed_at=board.stamp()),
                  dict(id="undated", title="No recorded time", status="blocked", area="ui")])
    write_tasks()
    page.goto(page.url.split("#")[0] + "#run=current&layout=rows")
    expect(page.locator("#work-list .task-row")).to_have_count(20)
    page.locator(".task-select").first.focus()
    tasks[0]["acceptance"] = "Verify selection focus survives a live update"
    write_tasks()
    page.evaluate("() => refresh()")
    expect(page.locator(".task-select").first).to_be_focused()
    page.locator("#select-page").click()
    expect(page.locator("#selection-count")).to_have_text("20 selected")
    page.locator("#work-next").click()
    page.locator("#select-page").click()
    expect(page.locator("#selection-count")).to_have_text("40 selected")
    page.locator("#work-next").click()
    page.locator("#select-page").click()
    expect(page.locator("#control-feedback")).to_contain_text("up to 50")
    expect(page.locator("#selection-count")).to_have_text("40 selected")
    page.locator("#search").fill("not visible")
    expect(page.locator("#selection-count")).to_contain_text("40 outside these filters")
    page.locator("#bulk-priority").click()
    expect(page.locator("#request-title")).to_have_text("Set priority for 40 tasks")
    expect(page.locator("#request-context")).to_contain_text("Task 39")
    page.locator("#request-priority").select_option("high")
    page.locator("#request-text").fill("Prioritize this batch")
    original = (state / "backlog.jsonl").read_bytes()
    def lost_response(route):
        route.fetch()
        route.abort()
    page.route("**/api/requests/batch", lost_response)
    page.locator("#request-submit").click()
    expect(page.locator("#request-error")).to_be_visible()
    assert len(controls.request_view(repo)) == 40
    assert (state / "backlog.jsonl").read_bytes() == original
    page.unroute("**/api/requests/batch", lost_response)
    page.reload()
    page.locator("#resume-draft").click()
    expect(page.locator("#request-title")).to_have_text("Set priority for 40 tasks")
    page.locator("#request-submit").click()
    expect(page.locator("#request-dialog")).not_to_be_visible()
    assert len(controls.request_view(repo)) == 40
    # Work that finishes while a batch form is open makes the whole new submission fail.
    page.locator("#board-tab").click()
    page.locator("#select-page").click()
    page.locator("#bulk-priority").click()
    tasks[0]["status"] = "done"
    write_tasks()
    page.locator("#request-priority").select_option("low")
    page.locator("#request-submit").click()
    expect(page.locator("#request-error")).to_contain_text("already finished")
    assert len(controls.request_view(repo)) == 40
    page.locator("#discard-draft").click()
    page.locator("#refresh").click()
    expect(page.locator("#selection-count")).to_have_text("19 selected")
    replacement = {**run, "run_id": uuid.uuid4().hex}
    board.atomic_json(state / "supervisor.json", replacement)
    page.locator("#refresh").click()
    expect(page.locator("#selection-count")).to_have_text("0 selected")
    # Notes can annotate a finished current task without changing its evidence or status.
    page.locator("#completed-shortcut").click()
    page.locator("#search").fill("Verified result")
    page.locator("#history-list .task-row").click()
    page.locator("#add-task-note").click()
    page.locator("#request-text").fill("Keep this context <img src=x onerror=alert(1)>")
    page.reload()
    page.locator("#resume-draft").click()
    expect(page.locator("#request-title")).to_have_text("Task note")
    page.locator("#request-submit").click()
    expect(page.locator("#request-dialog")).not_to_be_visible()
    note = controls.request_view(repo)[-1]
    assert note["type"] == "note" and note["expected_status"] == "done"
    target = next(row for row in tasks if row["id"] == "done")
    target["user_notes"] = [dict(request_id=note["id"], text=note["text"], at=board.stamp())]
    write_tasks()
    controls.acknowledge(repo, note["id"], "applied", "Context saved; original verification preserved")
    page.locator("#history-tab").click()
    page.locator("#search").fill("Verified result")
    page.locator("#refresh").click()
    page.locator("#history-list .task-row").click()
    expect(page.locator("#task-thread")).to_contain_text("Task note · applied")
    expect(page.locator("#task-thread")).to_contain_text("original verification preserved")
    assert page.locator("#task-thread img").count() == 0
    expect(page.locator("#detail-content")).to_contain_text("Browser check passed")
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=str(artifacts / "task-notes-mobile.png"))
    page.keyboard.press("Escape")
    # Overview ignores task search filters, shows selected-run facts and bounds activity rows.
    tasks[1]["user_priority"] = "high"
    write_tasks()
    page.locator("#overview-tab").click()
    page.locator("#refresh").click()
    expect(page.locator("#overview-priority")).to_have_text("1")
    expect(page.locator("#overview-pending")).to_have_text("40")
    expect(page.locator("#overview-verification")).to_have_text("1 / 2")
    expect(page.locator("#overview-note")).to_contain_text("1 task has no timestamp")
    expect(page.locator("#area-summary tr")).to_have_count(2)
    expect(page.locator("#activity-list .activity-row")).to_have_count(20)
    page.locator("#activity-next").click()
    anchor = page.locator(".activity-row").first.get_attribute("data-event")
    tasks.append(dict(id="recent", title="Recently recorded", status="ready", area="ui", updated_at=board.stamp(time.time() + 1)))
    write_tasks()
    page.locator("#refresh").click()
    expect(page.locator(".activity-row").first).to_have_attribute("data-event", anchor)
    page.locator("#activity-type").select_option("request")
    with page.expect_download() as download:
        page.locator("#export").click()
    exported = json.loads(Path(download.value.path()).read_text())
    assert exported["counts"]["tasks"] == 63
    assert len(exported["activity"]) == 42 and all(row["kind"] == "request" for row in exported["activity"])
    page.reload()
    expect(page.locator("#overview-tab")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#activity-type")).to_have_value("request")
    page.locator(".activity-row").first.click()
    expect(page.locator("#requests-tab")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#request-list details[open]")).to_have_count(1)
    page.locator("#overview-tab").click()
    page.locator("#activity-type").select_option("task")
    page.locator(".activity-row").first.click()
    expect(page.locator("#task-dialog")).to_be_visible()
    page.keyboard.press("Escape")
    for width in (320, 390, 768, 1440):
        page.set_viewport_size({"width": width, "height": 1000})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.locator("#overview-view").evaluate("node => node.scrollIntoView()")
        page.screenshot(path=str(artifacts / f"overview-{width}.png"))
    # Archived notes stay linked to archived work; no current-task actions leak into it.
    archive = state / "history" / "triage-archive"
    archive.mkdir(parents=True)
    for name in ("operator.json", "board-receipts.json", "backlog.jsonl"):
        shutil.copy2(state / name, archive / name)
    page.locator("#refresh").click()
    page.locator("#run").select_option("triage-archive")
    page.locator("#history-tab").click()
    page.locator("#search").fill("Verified result")
    page.locator("#history-list .task-row").click()
    expect(page.locator("#add-task-note")).not_to_be_visible()
    expect(page.locator("#task-thread")).to_contain_text("original verification preserved")
    page.keyboard.press("Escape")
