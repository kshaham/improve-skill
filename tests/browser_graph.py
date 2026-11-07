"""Knowledge view checks; called by browser_board.py or run alone. No model launches."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import improve_board as board  # noqa: E402
import improve_graph as graph  # noqa: E402


def seed_graph(repo, tasks, write_tasks):
    state = repo / ".improve"
    for name in ("src/auth.py", "src/db.py", "tests/test_auth.py"):
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("# fixture\n")
    subprocess.run(["git", "-C", str(repo), "add", "src", "tests"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Improve", "-c", "user.email=improve@example.invalid",
                    "commit", "-qm", "graph fixture"], check=True)
    (state / "run.json").write_text(json.dumps({"run_id": "graph-run", "gates": {"py.fast": "pytest -q"},
                                                "gate_seconds": {"py.fast": 9}}))
    tasks[:] = [dict(id="g-1", title="Harden session refresh", area="security", status="done",
                     paths=["src/auth.py", "tests/test_auth.py"], verification="py.fast passed"),
                dict(id="g-2", title="Pool database handles", area="performance", status="rejected",
                     file="src/db.py", note="Benchmark noise exceeded the gain"),
                dict(id="g-3", title="Retry database deadlocks", area="resilience", status="blocked",
                     file="src/db.py", blocked_reason="Simulator 18 unavailable"),
                dict(id="g-4", title="Check database timeouts", area="resilience", status="ready", file="src/db.py")]
    write_tasks()
    graph.sync(repo, refine_after=True)
    with graph.locked(repo) as value:
        node = graph.add_insight(value, "Database tests flake under parallel pytest; run them serially",
                                 "pitfall", about=["file:src/db.py"], repo=repo)
        graph.refine(value, repo)
    return node["id"], graph.run_identity(state)[0]


def check_graph(page, repo, tasks, write_tasks, artifacts):
    insight, run_id = seed_graph(repo, tasks, write_tasks)
    pooled = f"task:{run_id}:g-2"
    page.set_viewport_size({"width": 1500, "height": 1000})
    page.locator("#run").select_option("all")
    page.locator("#refresh").click()
    page.locator("#graph-tab").click()
    expect(page.locator("#graph-view")).to_be_visible()
    active = graph.summary(graph.load(graph.graph_path(repo)))["insights"]
    expect(page.locator("#graph-count")).to_have_text(str(active))
    expect(page.locator(".graph-node").first).to_be_visible()
    expect(page.locator(f'.graph-node[data-id="{insight}"]')).to_have_count(1)
    expect(page.locator("#export")).to_have_text("Export graph ↗")
    expect(page.locator("#graph-runs")).to_have_text(str(len(graph.load(graph.graph_path(repo))["runs"])))
    expect(page.locator("#graph-hotspots")).to_contain_text("src/db.py")

    # Select knowledge, curate it, and confirm the change is saved for the skill.
    page.locator(f'.graph-node[data-id="{insight}"] circle').click(force=True)
    expect(page.locator("#graph-detail h3")).to_contain_text("Database tests flake")
    expect(page).to_have_url(re.compile(r"view=graph.*node=insight"))
    page.locator("#graph-detail button", has_text="Pin").click()
    expect(page.locator("#graph-status")).to_have_text("Pinned; the skill sees it every cycle.")
    expect(page.locator("#graph-detail .pinned-chip")).to_be_visible()
    assert graph.load(graph.graph_path(repo))["nodes"][insight]["pinned"] is True
    page.screenshot(path=str(artifacts / "graph-desktop.png"), full_page=True)

    # A linked file opens its neighborhood, including nodes outside the strongest set.
    page.locator("#graph-detail .graph-link", has_text="src/db.py").click()
    expect(page.locator("#graph-detail h3")).to_have_text("src/db.py")
    page.locator("#graph-detail button", has_text="Focus neighborhood").click()
    expect(page.locator("#graph-status")).to_contain_text("neighborhood")
    expect(page.locator(f'.graph-node[data-id="{pooled}"]')).to_have_count(1)

    # Tasks open in the board's task dialog, which shows what the graph knows.
    page.locator(f'.graph-node[data-id="{pooled}"] circle').click(force=True)
    page.locator("#graph-detail button", has_text="Open task").click()
    expect(page.locator("#task-dialog")).to_be_visible()
    expect(page.locator("#detail-title")).to_have_text("Pool database handles")
    expect(page.locator("#task-knowledge")).to_be_visible()
    expect(page.locator("#task-knowledge")).to_contain_text("Database tests flake")
    expect(page.locator("#task-knowledge")).to_contain_text("Retry database deadlocks")
    page.locator("#task-knowledge button", has_text="Database tests flake").click()
    expect(page.locator("#task-dialog")).not_to_be_visible()
    expect(page.locator("#graph-detail h3")).to_contain_text("Database tests flake")

    # Users can teach the skill directly; a retry of the same draft applies once.
    page.locator("#graph-reset").click()
    page.locator("#graph-add").click()
    page.locator("#graph-note-text").fill("Payments code is frozen until the audit ends.")
    page.locator("#graph-note-kind").select_option("decision")
    page.locator("#graph-note-about").fill("src/payments.py, area:security")
    page.locator("#graph-note-submit").click()
    expect(page.locator("#graph-note-dialog")).not_to_be_visible()
    expect(page.locator("#graph-detail h3")).to_have_text("Payments code is frozen until the audit ends.")
    saved = [node for node in graph.load(graph.graph_path(repo))["nodes"].values() if node.get("kind") == "decision"]
    assert len(saved) == 1 and saved[0]["source"] == "user", saved
    assert "From the user" in graph.brief(graph.load(graph.graph_path(repo)), "graph.py", str(repo)) or \
        "Payments code is frozen" in graph.brief(graph.load(graph.graph_path(repo)), "graph.py", str(repo))

    # Search narrows the picture; archiving hides knowledge from the skill.
    page.locator("#graph-query").fill("simulator")
    expect(page.locator("#graph-status")).to_contain_text("match")
    expect(page.locator('.graph-node[data-id^="blocker:"]')).to_have_count(1)
    page.locator("#graph-reset").click()
    page.locator(f'.graph-node[data-id="{insight}"] circle').click(force=True)
    page.locator("#graph-detail button", has_text="Archive").click()
    expect(page.locator("#graph-status")).to_have_text("Archived; hidden from the skill.")
    assert "Database tests flake" not in graph.brief(graph.load(graph.graph_path(repo)), "graph.py", str(repo))
    page.locator("#graph-refine").click()
    expect(page.locator("#graph-status")).to_contain_text("Refined:")
    expect(page.locator("#graph-refines li").first).to_contain_text("nodes")

    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#graph-fit").click()
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 1, f"knowledge view overflows a phone by {overflow}px"
    page.screenshot(path=str(artifacts / "graph-mobile.png"), full_page=True)
    page.set_viewport_size({"width": 1500, "height": 1000})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    parser.add_argument("--chrome", default=str(chrome) if chrome.exists() else None)
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args()
    artifacts = args.artifacts or Path(tempfile.mkdtemp(prefix="improve-graph-browser-"))
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="improve graph repo ") as directory:
        repo = Path(directory).resolve()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        state = repo / ".improve"
        state.mkdir()
        tasks = []

        def write_tasks():
            temp = state / "backlog.tmp"
            temp.write_text("".join(json.dumps(task) + "\n" for task in tasks))
            temp.replace(state / "backlog.jsonl")

        info = board.start_board(repo, 0)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=args.chrome, headless=True)
                page = browser.new_page(viewport={"width": 1500, "height": 1000}, locale="en-US")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(info["url"])
                expect(page.locator("#connection")).to_have_text("Board connected")
                check_graph(page, repo, tasks, write_tasks, artifacts)
                assert not errors, errors
                browser.close()
        finally:
            board.stop_board(repo)
    print(f"Knowledge view checks passed. Screenshots: {artifacts}")


if __name__ == "__main__":
    main()
