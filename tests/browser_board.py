"""Optional real-browser regression checks: uv run --with playwright python tests/browser_board.py."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import improve_board as board  # noqa: E402
from browser_controls import check_controls  # noqa: E402
from browser_focus import check_focus  # noqa: E402
from browser_graph import check_graph  # noqa: E402
from browser_mobile import check_mobile  # noqa: E402
from browser_workflow import check_workflow  # noqa: E402
from browser_triage import check_triage  # noqa: E402


def check_large_history(page, tasks, write_tasks, artifacts):
    """Thousands of finished tasks must remain bounded, searchable, and keyboard-accessible."""
    tasks.clear()
    baseline = 1700000000
    tasks.extend(dict(id=f"bulk-{i}", title=f"Completed improvement {i}", status="done",
                      area="performance" if i % 2 == 0 else "ui", completed_at=board.stamp(baseline + i),
                      commit=f"{i:040x}", verification="Measured result recorded") for i in range(2000))
    tasks[42]["title"] = "Needle investigation from an earlier run"
    tasks.extend(dict(id=f"rejected-{i}", title=f"Rejected hypothesis {i}", status="rejected", area="quality",
                      updated_at=board.stamp(baseline + i), note="Counter-scenario disproved the claim") for i in range(100))
    tasks.extend([dict(id="active-1", title="Current investigation", status="in_progress", area="ui"),
                  dict(id="active-2", title="Next bounded change", status="ready", area="quality")])
    write_tasks()
    page.set_viewport_size({"width": 1600, "height": 1050})
    page.locator("#refresh").click()
    expect(page.locator("#completed")).to_have_text("2,000")
    expect(page.locator(".task-card")).to_have_count(2)
    expect(page.locator("#recent-list .task-row")).to_have_count(5)
    expect(page.locator("#recent-list .task-row").first).to_have_attribute("data-key", "current:bulk-1999")
    page.screenshot(path=str(artifacts / "large-board.png"), full_page=True)
    page.locator("#view-completed").click()
    expect(page.locator("#history-tab")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#history-list .task-row")).to_have_count(20)
    expect(page.locator("#recent-list .task-row")).to_have_count(0)
    expect(page.locator("#history-list .task-row").first).to_have_attribute("data-key", "current:bulk-1999")
    page.screenshot(path=str(artifacts / "large-history.png"), full_page=True)
    page.locator("#history-list .task-row").last.scroll_into_view_if_needed()
    assert page.locator("#history-view .history-pagination").bounding_box()["y"] >= 0, "Page controls should stay visible while reading history"
    page.locator("#history-next").click()
    expect(page.locator("#history-list .task-row").first).to_have_attribute("data-key", "current:bulk-1979")
    # New completions do not displace the row being read on later pages.
    tasks.append(dict(id="new-completion", title="A newly finished task", status="done", area="ui", completed_at=board.stamp(baseline + 3000)))
    write_tasks()
    page.locator("#refresh").click()
    expect(page.locator("#completed")).to_have_text("2,001")
    expect(page.locator("#history-list .task-row").first).to_have_attribute("data-key", "current:bulk-1979")
    page.locator("#history-first").click()
    expect(page.locator("#history-list .task-row").first).to_have_attribute("data-key", "current:new-completion")
    page.locator("#search").fill("Needle investigation")
    expect(page.locator("#history-list .task-row")).to_have_count(1)
    expect(page.locator("#history-next")).to_be_disabled()
    page.locator("#history-list .task-row").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#detail-content")).to_contain_text("Measured result recorded")
    page.keyboard.press("Escape")
    page.locator("#area").select_option("ui")
    expect(page.locator("#history-empty")).to_be_visible()
    page.locator("#search").fill("")
    page.locator("#area").select_option("all")
    page.locator("#history-outcome").select_option("rejected")
    expect(page.locator("#history-list .task-row")).to_have_count(20)
    expect(page.locator("#history-list .row-outcome").first).to_have_text("Rejected")
    with page.expect_download() as download:
        page.locator("#export").click()
    exported = json.loads(Path(download.value.path()).read_text())
    assert len(exported["tasks"]) == 100, "Export should include every matching task, not only the current page"
    assert {task["status"] for task in exported["tasks"]} == {"rejected"}
    page.locator("#history-outcome").select_option("all")
    for width in (768, 1024):
        page.set_viewport_size({"width": width, "height": 900})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "History overflows tablet viewport"
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(path=str(artifacts / "mobile-history.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "History overflows mobile viewport"
    page.locator("#history-tab").focus()
    page.keyboard.press("ArrowLeft")
    expect(page.locator("#board-tab")).to_have_attribute("aria-selected", "true")
    page.keyboard.press("ArrowRight")
    expect(page.locator("#history-tab")).to_have_attribute("aria-selected", "true")
    # Shrinking data clamps the page and preserves graceful empty state handling.
    page.locator("#history-next").click()
    tasks[:] = [dict(id="last", title="Only remaining task", status="done", area="ui")]
    write_tasks()
    page.locator("#refresh").click()
    expect(page.locator("#history-list .task-row")).to_have_count(1)
    expect(page.locator("#history-prev")).to_be_disabled()
    expect(page.locator("#history-next")).to_be_disabled()
    expect(page.locator("#history-list .row-date")).to_have_text("Date not recorded")
    # Legacy records sort by a valid fallback timestamp; undated records remain last.
    tasks.extend([
        dict(id="fallback", title="Long history title " * 20, status="done", area="ui", completed_at="invalid", updated_at=board.stamp(baseline + 10)),
        dict(id="created", title="Created date only", status="done", area="ui", created_at=board.stamp(baseline)),
    ])
    write_tasks()
    page.locator("#refresh").click()
    expect(page.locator("#history-list .task-row")).to_have_count(3)
    expect(page.locator("#history-list .task-row").first).to_have_attribute("data-key", "current:fallback")
    expect(page.locator("#history-list .task-row").last).to_have_attribute("data-key", "current:last")
    expect(page.locator("#history-list .row-date").first).to_have_attribute("title", re.compile("^Updated:"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Long history title overflows mobile viewport"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    parser.add_argument("--chrome", default=str(chrome) if chrome.exists() else None)
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args()
    artifacts = args.artifacts or Path(tempfile.mkdtemp(prefix="improve-board-browser-"))
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="improve browser repo ") as directory:
        repo = Path(directory).resolve()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        state = repo / ".improve"
        state.mkdir()
        tasks = [
            dict(id="ui-01", title="Improve mobile navigation", area="ui", status="ready", acceptance="The menu works at narrow widths."),
            dict(id="perf-01", title="Measure checkout latency", area="performance", status="in_progress", evidence="Profiling the checkout journey.", focus_priority=1),
            dict(id="bug-01", title="Fix retry handling", area="resilience", status="done", verification="Retry test passed", commit="abcdef123456"),
            dict(id="test-01", title="Restore the integration environment", area="coverage", status="blocked", note="Test database unavailable.",
                 blocked_reason="Runner offline since 02:00", blocked_by="supervisor", blocked_at=board.stamp(1700000000), block_count=2),
            dict(id="asset-01", title="Refresh onboarding illustrations", area="assets", status="proposed", acceptance="Review the proposed visual direction."),
            dict(id="perf-02", title="Cache account results", area="performance", status="rejected", note="Counter-scenario exposed stale authorization."),
        ]
        def write_tasks():
            temp = state / "backlog.tmp"
            temp.write_text("".join(json.dumps(task) + "\n" for task in tasks))
            temp.replace(state / "backlog.jsonl")
        write_tasks()
        archive = state / "history/2026-10-01-example"
        archive.mkdir(parents=True)
        (archive / "backlog.jsonl").write_text(json.dumps(dict(id="ui-01", title="Earlier UI investigation", area="ui", status="done")) + "\n")
        (state / "discovery.jsonl").write_text(json.dumps(dict(at="2026-10-04T12:00:00Z", hypothesis="Inspect checkout error states", evidence="A retry can submit twice; reproduction saved.")) + "\n")
        info = board.start_board(repo, 0)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=args.chrome, headless=True)
                page = browser.new_page(viewport={"width": 1600, "height": 1050}, device_scale_factor=1, locale="en-US")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(info["url"])
                expect(page.locator("#connection")).to_have_text("Board connected")
                expect(page.locator(".task-card")).to_have_count(4)
                expect(page.locator(".column")).to_have_count(4)
                expect(page.locator("#recent-list .task-row")).to_have_count(2)
                page.screenshot(path=str(artifacts / "desktop.png"), full_page=True)

                page.locator("#search").fill("retry")
                expect(page.locator("#recent-list .task-row")).to_have_count(1)
                page.locator("#recent-list .task-row").click()
                expect(page.locator("#task-dialog")).to_be_visible()
                expect(page.locator("#detail-content")).to_contain_text("Retry test passed")
                expect(page.locator("#detail-content")).to_contain_text("abcdef123456")
                page.keyboard.press("Escape")
                expect(page.locator("#task-dialog")).not_to_be_visible()
                page.locator("#search").fill("")
                page.locator("#area").select_option("ui")
                expect(page.locator(".task-card")).to_have_count(1)
                expect(page.locator("#recent-list .task-row")).to_have_count(1)
                page.locator("#run").select_option("2026-10-01-example")
                expect(page.locator("#recent-list .task-row")).to_have_count(1)
                expect(page.locator("#recent-list .task-row")).to_contain_text("Earlier UI investigation")
                page.locator("#area").select_option("all")
                page.locator("#run").select_option("current")
                expect(page.locator(".task-card")).to_have_count(4)

                tasks[0]["status"] = "done"
                tasks[0]["verification"] = "Mobile navigation check passed"
                write_tasks()
                expect(page.locator('#recent-list .task-row')).to_have_count(2, timeout=10000)
                page.locator('[data-key="current:ui-01"]').focus()
                page.keyboard.press("Enter")
                expect(page.locator("#detail-content")).to_contain_text("Mobile navigation check passed")
                page.keyboard.press("Escape")
                with page.expect_download() as download:
                    page.locator("#export").click()
                exported = json.loads(Path(download.value.path()).read_text())
                assert len(exported["tasks"]) == 6

                (state / "bets.jsonl").write_text(json.dumps(dict(
                    id="b-01", hypothesis="Replace slow serialization", status="placed", journey="feed-open",
                    spike={"gain": 3.5}, pieces=[{"name": "handler", "status": "landed", "commit": "abc123"}])) + "\n")
                page.locator("#refresh").click()
                expect(page.locator(".task-card")).to_have_count(4)
                page.locator('[data-key="current:bet:b-01"]').click()
                expect(page.locator("#detail-content")).to_contain_text("3.5")
                expect(page.locator("#detail-content")).to_contain_text("abc123")
                page.keyboard.press("Escape")
                (state / "bets.jsonl").unlink()
                page.locator("#refresh").click()
                expect(page.locator(".task-card")).to_have_count(3)

                # Untrusted ledger text must remain visible text, never executable markup.
                tasks[0]["title"] = '<img src=x onerror="window.boardInjected=true">'
                write_tasks()
                page.locator("#refresh").click()
                expect(page.locator('[data-key="current:ui-01"]')).to_contain_text("<img src=x")
                assert page.evaluate("window.boardInjected === undefined")
                assert page.locator(".task-row img").count() == 0
                tasks[0]["title"] = "Improve mobile navigation"
                write_tasks()
                page.locator("#refresh").click()
                expect(page.locator('[data-key="current:ui-01"]')).to_contain_text("Improve mobile navigation")

                with (state / "backlog.jsonl").open("a") as stream:
                    stream.write('{"id":"bad-measurement","measurement":NaN}\n')
                page.locator("#refresh").click()
                expect(page.locator("#warning")).to_contain_text("invalid record")
                expect(page.locator("#connection")).to_have_text("Board connected")
                expect(page.locator(".task-card")).to_have_count(3)
                write_tasks()
                page.locator("#refresh").click()
                expect(page.locator("#warning")).not_to_be_visible()

                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path=str(artifacts / "mobile.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Page overflows mobile viewport"
                page.locator("#search").fill("environment")
                expect(page.locator(".task-card")).to_have_count(1)
                page.locator(".task-card").click()
                expect(page.locator("#detail-content")).to_contain_text("Test database unavailable")
                expect(page.locator("#detail-blocked")).to_contain_text("Blocked by the supervisor")
                expect(page.locator("#detail-blocked")).to_contain_text("(2 times): Runner offline since 02:00")
                expect(page.locator(".task-card .card-note")).to_contain_text("Runner offline since 02:00")
                page.locator("#close-dialog").click()
                page.locator("#search").fill("")

                # Connection to the viewer is separate from daemon liveness and report completion.
                now = time.time()
                run = dict(started_at=board.stamp(now - 20), deadline=board.stamp(now + 120),
                           cycle=4, outcome="completed", last_activity_at=board.stamp(now - 5))
                control = dict(started_at=run["started_at"], deadline=run["deadline"], outcome=None,
                               engine="codex", model="test-model", phase="account-limit", retry_at=board.stamp(now + 30),
                               heartbeat_at=board.stamp(now), consecutive_failures=2, last_error="Inspect a new scope",
                               cycle_started_at=board.stamp(now - 2700), child_pid=4242, cycle_timeout_seconds=3600,
                               quarantines=1, recoveries_total=2, crashes=0, limit_waited_seconds=90,
                               cycle_history=[dict(started_at=board.stamp(now - 7200), seconds=600, rc=0, advanced=True),
                                              dict(started_at=board.stamp(now - 6000), seconds=300, rc=0, advanced=False),
                                              dict(started_at=board.stamp(now - 5000), seconds=120, rc=1, advanced=False, outcome="gate failed")])
                board.atomic_json(state / "run.json", run)
                board.atomic_json(state / "supervisor.json", control)
                (state / "journal.md").write_text("# Hour 1\nJournal entry <b>kept as text</b>\n")
                page.locator("#refresh").click()
                expect(page.locator("#run-state")).to_have_text("Daemon not running")
                expect(page.locator("#connection")).to_have_text("Board connected")
                expect(page.locator("#run-meta")).to_contain_text("codex · test-model · Cycle 4")
                expect(page.locator("#run-notice")).to_contain_text("Consecutive failed or uncheckpointed cycles: 2")
                expect(page.locator("#run-notice")).to_contain_text("Saved account-limit retry")
                expect(page.locator("#warning")).to_contain_text("awaits supervisor confirmation")
                # Run health: live ages, counters, the cycle timeline, and the operational views.
                expect(page.locator("#health-heartbeat")).to_contain_text("Supervisor heartbeat")
                expect(page.locator("#health-counters")).to_contain_text("Quarantines")
                expect(page.locator("#health-counters li.attention")).to_have_count(1)
                expect(page.locator(".cycle-bar")).to_have_count(3)
                expect(page.locator(".cycle-bar.failed")).to_have_count(1)
                page.locator("#open-journal").click()
                expect(page.locator("#text-dialog-body")).to_contain_text("Journal entry <b>kept as text</b>")
                assert page.locator("#text-dialog-body b").count() == 0
                page.keyboard.press("Escape")
                expect(page.locator("#text-dialog")).not_to_be_visible()
                page.locator("#open-log").click()
                expect(page.locator("#text-dialog-note")).to_contain_text("No daemon log yet")
                page.keyboard.press("Escape")
                page.locator("#open-quarantine").click()
                expect(page.locator("#text-dialog-note")).to_contain_text("No quarantined work")
                page.keyboard.press("Escape")
                expect(page.locator("#open-quarantine")).to_be_focused()
                control.update(outcome="halted: verification unavailable", summary_pending=True, phase="ended")
                board.atomic_json(state / "supervisor.json", control)
                page.locator("#refresh").click()
                expect(page.locator("#run-notice")).to_contain_text("Final report pending")
                expect(page.locator("#run-state")).to_have_text("halted: verification unavailable")
                page.screenshot(path=str(artifacts / "mobile-health.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Health panel overflows mobile viewport"
                with board.repo_lock(state / "daemon.lock"):
                    control.update(phase="finalize", outcome=None)
                    board.atomic_json(state / "supervisor.json", control)
                    page.locator("#refresh").click()
                    expect(page.locator("#run-state")).to_have_text("Writing final report")
                    # 45 of 60 minutes used: the cycle age is amber, the heartbeat is fresh.
                    expect(page.locator("#health-cycle")).to_have_text(re.compile(r"Cycle running for 4[5-9] min of 1 h 00 min limit"))
                    expect(page.locator("#health-cycle")).to_have_class("health-age level-amber")
                    expect(page.locator("#health-heartbeat")).to_have_class("health-age level-ok")
                (state / "supervisor.json").unlink()
                (state / "run.json").unlink()
                (state / "journal.md").unlink()
                page.locator("#refresh").click()
                expect(page.locator("#run-health")).not_to_be_visible()

                # Fail a request, preserve the last snapshot, and recover on the next refresh.
                page.route("**/api/board?*", lambda route: route.abort())
                page.locator("#refresh").click()
                expect(page.locator("#connection")).to_have_text("Disconnected · retrying")
                expect(page.locator(".task-card")).to_have_count(3)
                # Controls stay usable while the last snapshot is under 30 s old, then close with a reason.
                expect(page.locator("#new-task")).to_be_enabled()
                expect(page.locator("#updated")).to_contain_text("last refresh failed")
                page.evaluate("lastSnapshotAt -= 31000; tickClock()")
                expect(page.locator("#new-task")).to_be_disabled()
                expect(page.locator("#control-note")).to_contain_text("No fresh data for")
                page.unroute("**/api/board?*")
                page.locator("#refresh").click()
                expect(page.locator("#connection")).to_have_text("Board connected")
                expect(page.locator("#new-task")).to_be_enabled()
                # Dark theme: the footer toggle cycles auto, light, dark and is remembered per browser.
                page.locator("#theme-toggle").click()
                expect(page.locator("#theme-toggle")).to_have_text("Theme: light")
                page.locator("#theme-toggle").click()
                expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(21, 22, 28)"
                page.screenshot(path=str(artifacts / "dark-mobile.png"), full_page=True)
                page.locator("#theme-toggle").click()
                expect(page.locator("#theme-toggle")).to_have_text("Theme: auto")
                assert page.evaluate("document.documentElement.dataset.theme") is None
                tasks.clear()
                write_tasks()
                page.locator("#refresh").click()
                expect(page.locator("#empty")).to_be_visible()
                expect(page.locator(".task-card")).to_have_count(0)
                check_large_history(page, tasks, write_tasks, artifacts)
                check_controls(page, repo, tasks, write_tasks, artifacts)
                check_workflow(page, repo, tasks, write_tasks, artifacts)
                check_triage(page, repo, tasks, write_tasks, artifacts)
                check_focus(page, repo, tasks, write_tasks, artifacts)
                check_mobile(page, repo, info["url"], tasks, write_tasks, artifacts)
                check_graph(page, repo, tasks, write_tasks, artifacts)
                assert not errors, errors
                browser.close()
        finally:
            board.stop_board(repo)
    print(f"Browser checks passed. Screenshots: {artifacts}")


if __name__ == "__main__":
    main()
