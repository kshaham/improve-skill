"""Worklist, bookmarks, durable drafts and follow-ups using a disposable real board."""
import time
import uuid
import json
from pathlib import Path

from playwright.sync_api import expect

import improve_board as board
import improve_control as controls


def check_workflow(page, repo, tasks, write_tasks, artifacts):
    state = repo / ".improve"
    run = dict(run_id=uuid.uuid4().hex, started_at=board.stamp(), deadline=board.stamp(time.time() + 300),
               engine="codex", board_control_version=1)
    board.atomic_json(state / "supervisor.json", run)
    page.goto(page.url.split("#")[0])
    page.locator("#run").select_option("current")
    tasks[:] = [dict(id=f"work-{i:04}", title=f"Useful task {i:04}", status="ready", area="quality",
                    updated_at=board.stamp(1700000000 + i)) for i in range(1500)]
    tasks[0].update(user_priority="low", focus_priority=1)
    tasks[1].update(user_priority="high", focus_priority=2)
    tasks[2].update(user_priority="high", focus_priority=1, status="blocked")
    tasks[-1].update(completed_at=board.stamp(1700000000), reopens="previous-work")
    tasks.append(dict(id="finished", title="Verified older change", status="done", area="ui", commit="abc1234"))
    write_tasks()
    page.locator("#refresh").click()
    page.locator("#work-layout").select_option("rows")
    expect(page.locator("#work-list .task-row")).to_have_count(20)
    expect(page.locator(".task-card")).to_have_count(0)
    expect(page.locator("#work-list .task-row").first).to_have_attribute("data-key", "current:work-0002")
    expect(page.locator("#work-list .row-outcome").first).to_have_text("Blocked")
    expect(page.locator("#work-list .row-priority").first).to_have_text("high")
    page.locator("#work-next").click()
    anchor = page.locator("#work-list .task-row").first.get_attribute("data-key")
    tasks.append(dict(id="new-high", title="New high priority", status="ready", area="ui", user_priority="high", focus_priority=0))
    write_tasks()
    page.locator("#refresh").click()
    expect(page.locator("#work-list .task-row").first).to_have_attribute("data-key", anchor)
    page.locator("#priority-filter").select_option("high")
    expect(page.locator("#work-list .task-row")).to_have_count(3)
    expect(page.locator("#work-prev")).to_be_disabled()
    page.locator("#work-sort").select_option("title")
    page.locator("#area").select_option("quality")
    expect(page.locator("#work-list .task-row")).to_have_count(2)
    page.locator("#work-list .task-row").first.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#detail-title")).to_have_text("Useful task 0001")
    page.locator("#copy-task-link").click()
    link = page.locator("#task-link").input_value()
    assert "task=current%3Awork-0001" in link
    page.goto(link)
    expect(page.locator("#task-dialog")).to_be_visible()
    expect(page.locator("#detail-title")).to_have_text("Useful task 0001")
    expect(page.locator("#priority-filter")).to_have_value("high")
    expect(page.locator("#work-sort")).to_have_value("title")
    expect(page.locator("#area")).to_have_value("quality")
    page.keyboard.press("Escape")
    # A delayed failure from the previous bookmark cannot replace the newer selection.
    delayed = []
    page.route("**/api/board?run=missing-hold", lambda route: delayed.append(route))
    page.evaluate("""() => {
        location.hash = 'run=missing-hold';
        document.getElementById('task-dialog').dispatchEvent(new Event('close'));
    }""")
    expect(page.locator("#run")).to_have_value("missing-hold")
    page.wait_for_timeout(100)
    assert delayed
    page.evaluate("fragment => { location.hash = fragment; }", link.split("#", 1)[1])
    expect(page.locator("#run")).to_have_value("current")
    delayed[0].fulfill(status=400, json={"error": "unknown run"})
    page.unroute("**/api/board?run=missing-hold")
    expect(page.locator("#task-dialog")).to_be_visible()
    expect(page.locator("#detail-title")).to_have_text("Useful task 0001")
    page.keyboard.press("Escape")
    page.locator("#clear-filters").click()
    page.locator("#work-sort").select_option("updated")
    expect(page.locator("#work-list .task-row").first).to_have_attribute("data-key", "current:work-1499")
    with page.expect_download() as download:
        page.locator("#export").click()
    exported = json.loads(Path(download.value.path()).read_text())["tasks"]
    assert len(exported) == 1501 and all(row["status"] != "done" for row in exported)
    page.set_viewport_size({"width": 1440, "height": 1050})
    page.screenshot(path=str(artifacts / "worklist-desktop.png"), full_page=True)
    for width in (768, 1024):
        page.set_viewport_size({"width": width, "height": 900})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), page.evaluate("({width:innerWidth,scroll:document.documentElement.scrollWidth,body:document.body.scrollWidth,x:scrollX,overflow:[...document.querySelectorAll('body *')].filter(x=>x.getBoundingClientRect().right>innerWidth-.5).slice(0,8).map(x=>[x.id||x.className,x.getBoundingClientRect().right])})")
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=str(artifacts / "worklist-mobile.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), page.evaluate("({width:innerWidth,scroll:document.documentElement.scrollWidth,body:document.body.scrollWidth,x:scrollX,overflow:[...document.querySelectorAll('body *')].filter(x=>x.getBoundingClientRect().right>innerWidth-.5).slice(0,12).map(x=>[x.id||x.className,x.getBoundingClientRect().right])})")
    # A broken bookmark must recover, while reused current IDs cannot open a different run's task.
    page.goto(link.replace("run=current", "run=removed-archive"))
    expect(page.locator("#connection")).to_have_text("Board connected")
    expect(page.locator("#run")).to_have_value("all")
    expect(page.locator("#view-notice")).to_contain_text("no longer available")
    replacement = {**run, "run_id": uuid.uuid4().hex}
    board.atomic_json(state / "supervisor.json", replacement)
    page.goto(link)
    expect(page.locator("#view-notice")).to_contain_text("task link")
    expect(page.locator("#task-dialog")).not_to_be_visible()
    page.locator("#clear-filters").click()
    # Follow-ups carry source context, are separate requests, and leave finished evidence intact.
    page.locator("#completed-shortcut").click()
    page.locator("#search").fill("Verified older change")
    page.locator("#history-list .task-row").click()
    page.locator("#follow-up-task").click()
    expect(page.locator("#request-task-title")).to_have_value("Follow up: Verified older change")
    assert "abc1234" in page.locator("#request-text").input_value()
    page.locator("#request-text").fill(page.locator("#request-text").input_value() + "Verify the next scenario")
    original = (state / "backlog.jsonl").read_bytes()
    page.locator("#request-submit").click()
    expect(page.locator("#request-dialog")).not_to_be_visible()
    assert (state / "backlog.jsonl").read_bytes() == original
    assert controls.request_view(repo)[-1]["title"] == "Follow up: Verified older change"
    # Close, reload, and resume preserve all fields and isolate unrelated drafts.
    page.locator("#new-task").click()
    page.locator("#request-task-title").fill("Draft to recover")
    page.locator("#request-area").select_option("performance")
    page.locator("#request-priority").select_option("low")
    page.locator("#request-text").fill("Keep this text across a reload")
    page.keyboard.press("Escape")
    page.locator("#send-guidance").click()
    page.locator("#request-text").fill("A separate guidance draft")
    page.reload()
    expect(page.locator("#resume-draft")).to_be_visible()
    page.locator("#resume-draft").click()
    expect(page.locator("#request-text")).to_have_value("A separate guidance draft")
    page.locator("#discard-draft").click()
    page.locator("#new-task").click()
    expect(page.locator("#request-task-title")).to_have_value("Draft to recover")
    expect(page.locator("#request-area")).to_have_value("performance")
    expect(page.locator("#request-priority")).to_have_value("low")
    expect(page.locator("#request-text")).to_have_value("Keep this text across a reload")
    # Losing a successful response, then reloading, must retain the retry ID.
    def lose_response(route):
        route.fetch()
        route.abort()
    page.route("**/api/requests", lose_response)
    before = len(controls.request_view(repo))
    page.locator("#request-submit").click()
    expect(page.locator("#request-error")).to_be_visible()
    assert len(controls.request_view(repo)) == before + 1
    page.unroute("**/api/requests", lose_response)
    page.reload()
    page.locator("#resume-draft").click()
    page.locator("#request-submit").click()
    expect(page.locator("#request-dialog")).not_to_be_visible()
    assert len(controls.request_view(repo)) == before + 1
    # Service capabilities refresh once automatically; persistent rejection remains bounded.
    page.evaluate("controlsToken = 'expired-service-token'")
    page.locator("#send-guidance").click()
    page.locator("#request-text").fill("Reconnect once")
    page.locator("#request-submit").click()
    expect(page.locator("#request-dialog")).not_to_be_visible()
    rejects = []
    def forbidden(route):
        rejects.append(1)
        route.fulfill(status=403, json={"error": "Still rejected"})
    page.route("**/api/requests", forbidden)
    page.locator("#send-guidance").click()
    page.locator("#request-text").fill("Do not retry forever")
    page.locator("#request-submit").click()
    expect(page.locator("#request-error")).to_have_text("Still rejected")
    assert len(rejects) == 2
    page.unroute("**/api/requests", forbidden)
    page.locator("#discard-draft").click()
    # Browser storage failure still allows explicit submission, with an honest draft warning.
    page.evaluate("() => { window.originalSetItem = Storage.prototype.setItem; Storage.prototype.setItem = () => { throw new Error('Storage blocked'); }; }")
    page.locator("#new-task").click()
    page.locator("#request-task-title").fill("Works without storage")
    expect(page.locator("#draft-status")).to_contain_text("only in this open form")
    page.locator("#request-submit").click()
    expect(page.locator("#request-dialog")).not_to_be_visible()
    page.evaluate("() => { Storage.prototype.setItem = window.originalSetItem; }")
    page.locator("#new-task").click()
    page.locator("#request-task-title").fill("Storage recovered")
    expect(page.locator("#draft-status")).not_to_contain_text("only in this open form")
    page.locator("#request-task-title").fill("")
    page.reload()
    page.locator("#new-task").click()
    expect(page.locator("#request-task-title")).to_have_value("")
    page.locator("#discard-draft").click()
    # Drafts from an old run remain recoverable but cannot be silently retargeted.
    page.locator("#send-guidance").click()
    page.locator("#request-text").fill("Old-run draft")
    page.keyboard.press("Escape")
    board.atomic_json(state / "supervisor.json", {**replacement, "run_id": uuid.uuid4().hex})
    page.reload()
    page.locator("#resume-draft").click()
    expect(page.locator("#draft-status")).to_contain_text("earlier run")
    expect(page.locator("#request-submit")).to_be_disabled()
    page.screenshot(path=str(artifacts / "saved-draft-mobile.png"), full_page=True)
    page.locator("#discard-draft").click()
    # A second repository on the same local origin cannot restore the first one's drafts.
    page.locator("#send-guidance").click()
    page.locator("#request-text").fill("Only for the original repository")
    page.keyboard.press("Escape")
    expect(page.locator("#resume-draft")).to_be_visible()
    def other_workspace(route):
        response = route.fetch()
        data = response.json()
        data["workspace_id"] = "f" * 64
        route.fulfill(response=response, json=data)
    page.route("**/api/board?*", other_workspace)
    page.reload()
    expect(page.locator("#connection")).to_have_text("Board connected")
    expect(page.locator("#resume-draft")).not_to_be_visible()
    page.unroute("**/api/board?*", other_workspace)
    page.reload()
    expect(page.locator("#resume-draft")).to_be_visible()
