"""Knowledge view with a large graph and a live daemon writing during interaction.

Run alone (it builds its own repository):
    uv run --with playwright python tests/browser_graph_scale.py --artifacts DIR
No model is launched.
"""
import argparse
import json
from pathlib import Path
import random
import sys
import tempfile
import threading
import time

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_support import LongSession, archive, environment, load, make_repo, restore_environment, write_ledger, write_run  # noqa: E402
import improve_board as board  # noqa: E402
import improve_graph as graph  # noqa: E402

VIEW_LIMIT = 220


def build(root):
    files = [f"web/s{index // 50}/c{index}.tsx" for index in range(2000)]
    repo = make_repo(root, files).resolve()
    rand = random.Random(21)
    for run in range(15):
        archive(repo, f"2026-07-{run:02d}", f"r{run}", [
            dict(id=f"t{index}", title=f"Task {index} of run {run}", area=rand.choice(LongSession.AREAS),
                 status=rand.choice(("done", "rejected", "blocked")), blocked_reason=f"Emulator {index % 6} offline",
                 paths=rand.sample(files[:900], rand.randint(1, 4)), verification="ts.fast ok")
            for index in range(300)], gates={"ts.fast": 25, "e2e": 400})
    write_run(repo / ".improve", "live")
    rows = [dict(id=f"L{index}", title=f"Live {index}", area="ui", status="ready", file=files[index]) for index in range(20)]
    write_ledger(repo / ".improve", rows)
    graph.sync(repo, refine_after=True)
    with graph.locked(repo) as value:
        notes = [graph.add_insight(value, f"Screen {index} needs the fixture f{index} before snapshot s{index % 9}",
                                   ("pitfall", "technique", "fact")[index % 3], about=[files[index]], repo=repo)["id"]
                 for index in range(350)]
        value["aliases"]["insight:retired-by-merge"] = notes[3]
        graph.refine(value, repo)
    return repo, files, rows, notes


def transform(page, selector):
    return page.locator(selector).first.get_attribute("transform")


def node_count(page):
    return page.locator(".graph-node").count()


def check_scale(page, url, repo, files, rows, notes, artifacts):
    started = time.perf_counter()
    page.goto(url + "/#view=graph")
    expect(page.locator("#graph-view")).to_be_visible()
    expect(page.locator(".graph-node").first).to_be_visible(timeout=15000)
    first_paint = time.perf_counter() - started
    assert first_paint < 10, f"first graph paint took {first_paint:.1f}s"
    assert 10 <= node_count(page) <= VIEW_LIMIT, node_count(page)
    expect(page.locator("#graph-status")).to_contain_text("strongest")
    expect(page.locator("#visible-count")).to_contain_text("nodes shown")
    total = graph.summary(load(repo))["nodes"]
    expect(page.locator("#graph-total")).to_contain_text(f"{total:,}")

    # The force layout must not freeze the page: sample frame times while it settles.
    frames = page.evaluate("""() => new Promise((done) => {
        const times = []; let last = performance.now();
        const tick = (now) => { times.push(now - last); last = now;
          times.length < 40 ? requestAnimationFrame(tick) : done(times); };
        requestAnimationFrame(tick); })""")
    average = sum(frames) / len(frames)
    assert average < 120, f"average frame {average:.0f}ms while laying out {node_count(page)} nodes"
    page.locator(".graph-layout").screenshot(path=str(artifacts / "graph-scale.png"))

    # Type chips: add tasks, and the last remaining type cannot be switched off.
    page.locator(".graph-type", has_text="Tasks").click()
    expect(page.locator(".graph-node.type-task").first).to_be_visible()
    for label in ("Knowledge", "Files", "Areas", "Gates", "Blockers", "Runs", "Tasks"):
        chip = page.locator(".graph-type", has_text=label)
        if chip.get_attribute("aria-pressed") == "true":
            chip.click()
    pressed = page.locator('.graph-type[aria-pressed="true"]')
    expect(pressed).to_have_count(1)
    for label in ("Knowledge", "Files", "Areas", "Gates", "Blockers"):
        if page.locator(".graph-type", has_text=label).get_attribute("aria-pressed") != "true":
            page.locator(".graph-type", has_text=label).click()
    if page.locator(".graph-type", has_text="Tasks").get_attribute("aria-pressed") == "true":
        page.locator(".graph-type", has_text="Tasks").click()

    # Search narrows to matches and their neighbors; clearing restores the strongest view.
    page.locator("#graph-query").fill("emulator offline")
    expect(page.locator("#graph-status")).to_contain_text("match", timeout=10000)
    expect(page.locator('.graph-node[data-id^="blocker:"]').first).to_be_visible()
    assert node_count(page) <= VIEW_LIMIT
    page.locator("#graph-query").fill("")
    expect(page.locator("#graph-status")).to_contain_text("strongest", timeout=10000)

    # Deep links: a busy node, a merged node's old id, and a removed node.
    page.goto(url + "/#view=graph&node=area:ui")
    expect(page.locator("#graph-detail h3")).to_have_text("ui", timeout=10000)
    expect(page.locator("#graph-detail")).to_contain_text("more. Focus this node to see them all.")
    expect(page.locator("#graph-status")).to_contain_text("neighborhood")
    assert node_count(page) <= VIEW_LIMIT
    page.goto(url + "/#view=graph&node=insight:retired-by-merge")
    expect(page.locator("#graph-detail h3")).to_contain_text("Screen 3 needs", timeout=10000)
    page.goto(url + "/#view=graph&node=insight:never-existed")
    expect(page.locator("#graph-status")).to_contain_text("merged or removed", timeout=10000)
    page.locator("#graph-reset").click()
    expect(page.locator("#graph-status")).to_contain_text("strongest", timeout=10000)

    # Zoom, wheel, pan, node drag, and keyboard selection.
    viewport = transform(page, ".graph-viewport")
    page.locator("#graph-zoom-in").click()
    assert transform(page, ".graph-viewport") != viewport, "zoom in changes the view"
    zoomed = transform(page, ".graph-viewport")
    box = page.locator("#graph-canvas").bounding_box()
    page.mouse.move(box["x"] + 30, box["y"] + 30)
    page.mouse.wheel(0, 400)
    assert transform(page, ".graph-viewport") != zoomed, "wheel zoom changes the view"
    page.locator("#graph-fit").click()
    fitted = transform(page, ".graph-viewport")
    # Start the pan on empty canvas: a dense graph has nodes almost everywhere.
    empty = page.evaluate("""() => {
        const box = document.getElementById("graph-canvas").getBoundingClientRect();
        for (let y = box.top + 20; y < box.bottom - 40; y += 9)
          for (let x = box.left + 10; x < box.right - 150; x += 9)
            if (document.elementFromPoint(x, y)?.id === "graph-canvas") return { x, y };
        return null; }""")
    assert empty, "no empty canvas to pan from"
    page.mouse.move(empty["x"], empty["y"])
    page.mouse.down()
    page.mouse.move(empty["x"] + 120, empty["y"] + 50, steps=6)
    page.mouse.up()
    assert transform(page, ".graph-viewport") != fitted, "dragging the background pans"
    target = page.locator(f'.graph-node[data-id="{notes[0]}"]')
    if target.count():
        before = target.get_attribute("transform")
        handle = target.locator("circle").bounding_box()
        page.mouse.move(handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2)
        page.mouse.down()
        page.mouse.move(handle["x"] + 80, handle["y"] + 60, steps=6)
        page.mouse.up()
        assert target.get_attribute("transform") != before, "dragging a node moves it"
    keyed = page.locator(".graph-node").nth(3)
    keyed_id = keyed.get_attribute("data-id")
    keyed.focus()
    page.keyboard.press("Enter")
    expect(page.locator(f'.graph-node.selected[data-id="{keyed_id}"]')).to_have_count(1)

    # Live session: the daemon syncs and agents write while the user keeps working.
    stop, errors = threading.Event(), []

    def daemon():
        cycle = 0
        while not stop.is_set():
            try:
                rows.append(dict(id=f"D{cycle}", title=f"Daemon {cycle}", area="security", status="done",
                                 paths=[files[cycle % 900], files[(cycle * 13) % 900]]))
                write_ledger(repo / ".improve", rows)
                graph.sync(repo, refine_after=cycle % 4 == 3)
                with graph.locked(repo) as value:
                    graph.add_insight(value, f"Live agent note {cycle}: module z{cycle} requires order o{cycle}", "fact")
            except Exception as exc:  # Surface any daemon-side failure in the final assertion.
                errors.append(repr(exc))
            cycle += 1
            stop.wait(0.3)

    page.goto(url + f"/#view=graph&node={notes[1]}")
    expect(page.locator("#graph-detail h3")).to_contain_text("Screen 1 needs", timeout=10000)
    shown_total = page.locator("#graph-total").inner_text()
    thread = threading.Thread(target=daemon)
    thread.start()
    try:
        expect(page.locator("#graph-total")).not_to_have_text(shown_total, timeout=15000)
        expect(page.locator("#graph-detail h3")).to_contain_text("Screen 1 needs")
        page.locator("#graph-detail button", has_text="Pin").click()
        expect(page.locator("#graph-status")).to_have_text("Pinned; the skill sees it every cycle.")
        page.locator("#graph-add").click()
        page.locator("#graph-note-text").fill("Release builds must run the e2e gate on a cold cache.")
        page.locator("#graph-note-kind").select_option("convention")
        page.locator("#graph-note-about").fill("area:quality")
        page.locator("#graph-note-submit").click()
        expect(page.locator("#graph-note-dialog")).not_to_be_visible(timeout=10000)
        expect(page.locator("#graph-detail h3")).to_have_text("Release builds must run the e2e gate on a cold cache.")
        time.sleep(3.5)  # Let at least one more board poll land during the daemon's writes.
        expect(page.locator("#graph-detail h3")).to_have_text("Release builds must run the e2e gate on a cold cache.")
    finally:
        stop.set()
        thread.join(timeout=60)
    assert errors == [], errors
    value = load(repo)
    assert value["nodes"][notes[1]]["pinned"] is True
    assert any(node.get("kind") == "convention" and node.get("source") == "user" for node in value["nodes"].values())

    with page.expect_download() as download:
        page.locator("#export").click()
    exported = json.loads(Path(download.value.path()).read_text())
    assert 0 < len(exported["nodes"]) <= 800 and exported["total_nodes"] > len(exported["nodes"])

    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url + "/#view=graph")
    expect(page.locator(".graph-node").first).to_be_visible(timeout=15000)
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 1, f"large graph overflows a phone by {overflow}px"
    page.screenshot(path=str(artifacts / "graph-scale-mobile.png"), full_page=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    parser.add_argument("--chrome", default=str(chrome) if chrome.exists() else None)
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args()
    artifacts = args.artifacts or Path(tempfile.mkdtemp(prefix="improve-graph-scale-"))
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="improve graph scale browser ") as directory:
        previous = environment(Path(directory) / "home")
        repo, files, rows, notes = build(Path(directory) / "repo")
        info = board.start_board(repo, 0)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=args.chrome, headless=True)
                page = browser.new_page(viewport={"width": 1500, "height": 1000}, locale="en-US", accept_downloads=True)
                page_errors = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                check_scale(page, info["url"], repo, files, rows, notes, artifacts)
                assert not page_errors, page_errors
                browser.close()
        finally:
            board.stop_board(repo)
            restore_environment(previous)
    print(f"Large knowledge graph checks passed. Screenshots: {artifacts}")


if __name__ == "__main__":
    main()
