"""Focus-area panel checks called by browser_board.py; no model launches."""
from playwright.sync_api import expect

import improve_board as board
import improve_control as controls


def check_focus(page, repo, tasks, write_tasks, artifacts):
    tasks[:] = [dict(id="f-ui", title="Tighten card spacing", area="ui", status="ready"),
                dict(id="f-doc", title="Rewrite setup guide", area="docs", status="ready"),
                dict(id="f-sec", title="Escape report links", area="security", status="done")]
    write_tasks()
    page.set_viewport_size({"width": 1600, "height": 1050})
    page.locator("#board-tab").click()
    page.locator("#work-layout").select_option("columns")
    page.locator("#search").fill("")
    if "collapsed" in (page.locator("#focus-panel").get_attribute("class") or ""):
        page.locator("#focus-toggle").click()
    page.locator("#refresh").click()
    if page.locator("#run").input_value() != "current":
        expect(page.locator('.focus-tile[data-area="ui"] .focus-count')).to_have_text("Select the current run for counts")
        page.locator("#run").select_option("current")
    expect(page.locator(".focus-tile")).to_have_count(len(controls.AREAS))
    expect(page.locator("#focus-state")).to_have_text("Default")
    expect(page.locator("#focus-save")).to_be_disabled()
    expect(page.locator('.focus-tile[data-area="security"] .focus-count')).to_have_text("0 open · 1 finished")
    page.locator('.focus-preset[data-preset="polish"]').click()
    expect(page.locator('.focus-preset[data-preset="polish"]')).to_have_attribute("aria-pressed", "true")
    page.locator('.focus-tile[data-area="docs"] .focus-level.skip').click()
    expect(page.locator("#focus-state")).to_have_text("Unsaved changes")
    expect(page.locator("#focus-summary")).to_contain_text("Skip Docs")
    # A live refresh must not discard unsaved choices.
    page.locator("#refresh").click()
    expect(page.locator('.focus-tile[data-area="docs"]')).to_have_attribute("data-level", "skip")
    page.screenshot(path=str(artifacts / "focus-unsaved.png"), full_page=True)
    page.locator("#focus-note").fill("Checkout screens first")
    page.locator("#focus-save").click()
    expect(page.locator("#focus-state")).to_have_text("Waiting for the next checkpoint")
    request = [row for row in controls.request_view(repo) if row["type"] == "focus"][-1]
    assert request["areas"] == {"ui": "prefer", "assets": "prefer", "accessibility": "prefer", "docs": "skip"}, request
    assert request["text"] == "Checkout screens first"
    assert all(task["status"] == "ready" for task in board.board_snapshot(repo)["tasks"] if task["id"] != "f-sec")
    expect(page.locator('.task-card[data-area="ui"]')).to_have_attribute("data-area-focus", "prefer")
    expect(page.locator('.task-card[data-area="docs"]')).to_have_attribute("data-area-focus", "skip")
    controls.acknowledge(repo, request["id"], "applied", "Focus updated: prefer UI, assets, accessibility; skip docs.")
    page.locator("#refresh").click()
    expect(page.locator("#focus-state")).to_have_text("Applied")
    page.locator('.focus-tile[data-area="ui"] .focus-filter').click()
    expect(page.locator(".task-card")).to_have_count(1)
    page.locator("#area").select_option("all")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Focus panel overflows on mobile"
    page.screenshot(path=str(artifacts / "focus-mobile.png"), full_page=True)
    page.set_viewport_size({"width": 1600, "height": 1050})
    page.screenshot(path=str(artifacts / "focus-applied.png"), full_page=True)
