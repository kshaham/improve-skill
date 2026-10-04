"""Optional real-browser regression checks: uv run --with playwright python tests/browser_board.py."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import improve_board as board  # noqa: E402


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
            dict(id="test-01", title="Restore the integration environment", area="coverage", status="blocked", note="Test database unavailable."),
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
                page = browser.new_page(viewport={"width": 1600, "height": 1050}, device_scale_factor=1)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(info["url"])
                expect(page.locator("#connection")).to_have_text("Board connected")
                expect(page.locator(".task-card")).to_have_count(7)
                expect(page.locator(".column")).to_have_count(6)
                page.screenshot(path=str(artifacts / "desktop.png"), full_page=True)

                page.locator("#search").fill("retry")
                expect(page.locator(".task-card")).to_have_count(1)
                page.locator(".task-card").click()
                expect(page.locator("#task-dialog")).to_be_visible()
                expect(page.locator("#detail-content")).to_contain_text("Retry test passed")
                expect(page.locator("#detail-content")).to_contain_text("abcdef123456")
                page.keyboard.press("Escape")
                expect(page.locator("#task-dialog")).not_to_be_visible()
                page.locator("#search").fill("")
                page.locator("#area").select_option("ui")
                expect(page.locator(".task-card")).to_have_count(2)
                page.locator("#run").select_option("2026-10-01-example")
                expect(page.locator(".task-card")).to_have_count(1)
                expect(page.locator(".task-card")).to_contain_text("Earlier UI investigation")
                page.locator("#area").select_option("all")
                page.locator("#run").select_option("current")
                expect(page.locator(".task-card")).to_have_count(6)

                tasks[0]["status"] = "done"
                tasks[0]["verification"] = "Mobile navigation check passed"
                write_tasks()
                expect(page.locator('.column[data-status="done"] .task-card')).to_have_count(2, timeout=10000)
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
                expect(page.locator(".task-card")).to_have_count(7)
                page.locator('[data-key="current:bet:b-01"]').click()
                expect(page.locator("#detail-content")).to_contain_text("3.5")
                expect(page.locator("#detail-content")).to_contain_text("abc123")
                page.keyboard.press("Escape")
                (state / "bets.jsonl").unlink()
                page.locator("#refresh").click()
                expect(page.locator(".task-card")).to_have_count(6)

                # Untrusted ledger text must remain visible text, never executable markup.
                tasks[0]["title"] = '<img src=x onerror="window.boardInjected=true">'
                write_tasks()
                page.locator("#refresh").click()
                expect(page.locator('[data-key="current:ui-01"]')).to_contain_text("<img src=x")
                assert page.evaluate("window.boardInjected === undefined")
                assert page.locator(".task-card img").count() == 0
                tasks[0]["title"] = "Improve mobile navigation"
                write_tasks()
                page.locator("#refresh").click()
                expect(page.locator('[data-key="current:ui-01"]')).to_contain_text("Improve mobile navigation")

                with (state / "backlog.jsonl").open("a") as stream:
                    stream.write('{"id":"bad-measurement","measurement":NaN}\n')
                page.locator("#refresh").click()
                expect(page.locator("#warning")).to_contain_text("invalid record")
                expect(page.locator("#connection")).to_have_text("Board connected")
                expect(page.locator(".task-card")).to_have_count(6)
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
                page.locator("#close-dialog").click()
                page.locator("#search").fill("")

                # Connection to the viewer is separate from daemon liveness and report completion.
                now = time.time()
                run = dict(started_at=board.stamp(now - 20), deadline=board.stamp(now + 120),
                           cycle=4, outcome="completed", last_activity_at=board.stamp(now - 5))
                control = dict(started_at=run["started_at"], deadline=run["deadline"], outcome=None,
                               engine="codex", model="test-model", phase="account-limit", retry_at=board.stamp(now + 30),
                               heartbeat_at=board.stamp(now), consecutive_failures=2, last_error="Inspect a new scope")
                board.atomic_json(state / "run.json", run)
                board.atomic_json(state / "supervisor.json", control)
                page.locator("#refresh").click()
                expect(page.locator("#run-state")).to_have_text("Daemon not running")
                expect(page.locator("#connection")).to_have_text("Board connected")
                expect(page.locator("#run-meta")).to_contain_text("codex · test-model · Cycle 4")
                expect(page.locator("#run-notice")).to_contain_text("Consecutive failed or uncheckpointed cycles: 2")
                expect(page.locator("#run-notice")).to_contain_text("Saved account-limit retry")
                expect(page.locator("#warning")).to_contain_text("awaits supervisor confirmation")
                control.update(outcome="halted: verification unavailable", summary_pending=True, phase="ended")
                board.atomic_json(state / "supervisor.json", control)
                page.locator("#refresh").click()
                expect(page.locator("#run-notice")).to_contain_text("Final report pending")
                expect(page.locator("#run-state")).to_have_text("halted: verification unavailable")
                page.screenshot(path=str(artifacts / "mobile-health.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Health panel overflows mobile viewport"
                with board.repo_lock(state / "daemon.lock"):
                    control["phase"] = "finalize"
                    board.atomic_json(state / "supervisor.json", control)
                    page.locator("#refresh").click()
                    expect(page.locator("#run-state")).to_have_text("Writing final report")
                (state / "supervisor.json").unlink()
                (state / "run.json").unlink()
                page.locator("#refresh").click()
                expect(page.locator("#run-health")).not_to_be_visible()

                # Fail a request, preserve the last snapshot, and recover on the next refresh.
                page.route("**/api/board?*", lambda route: route.abort())
                page.locator("#refresh").click()
                expect(page.locator("#connection")).to_have_text("Disconnected · retrying")
                expect(page.locator(".task-card")).to_have_count(6)
                page.unroute("**/api/board?*")
                page.locator("#refresh").click()
                expect(page.locator("#connection")).to_have_text("Board connected")
                tasks.clear()
                write_tasks()
                page.locator("#refresh").click()
                expect(page.locator("#empty")).to_be_visible()
                expect(page.locator(".task-card")).to_have_count(0)
                assert not errors, errors
                browser.close()
        finally:
            board.stop_board(repo)
    print(f"Browser checks passed. Screenshots: {artifacts}")


if __name__ == "__main__":
    main()
