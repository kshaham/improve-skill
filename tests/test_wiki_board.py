"""The board's Wiki view: /api/wiki serves the wiki built at session end; the page renders it."""
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest

from graph_support import ROOT, graph, make_repo

import improve_board
import improve_wiki

ASSETS = ROOT / "assets" / "board"


class WikiEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(self.tmp.name, ["src/app.py"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_wiki_yet_is_an_empty_answer_not_an_error(self):
        self.assertEqual(improve_board.wiki_view(self.repo), {"built_at": None, "pages": {}})

    def test_the_built_wiki_is_served_as_page_data(self):
        with graph.locked(self.repo) as value:
            graph.add_insight(value, "Retry the upload twice before reporting a network failure",
                              about=["file:src/app.py"], repo=self.repo)
            (self.repo / ".improve" / "run.json").write_text(json.dumps({"run_id": "run-1", "outcome": "completed"}))
            improve_wiki.build(value, self.repo, built_at="2026-11-14T22:00:00Z")
        view = improve_board.wiki_view(self.repo)
        self.assertEqual(view["built_at"], "2026-11-14T22:00:00Z")
        self.assertEqual(view["pages"]["index"]["run_id"], "run-1")
        texts = [row["text"] for group in view["pages"]["knowledge"]["groups"] for row in group["notes"]]
        self.assertIn("Retry the upload twice before reporting a network failure", texts)

    def test_a_symlinked_wiki_is_not_followed(self):
        target = Path(self.tmp.name) / "elsewhere.toon"
        target.write_text("built_at: x\npages:\n  index:\n    title: leaked\n")
        (self.repo / ".improve" / "wiki").mkdir()
        (self.repo / ".improve" / "wiki" / "wiki.toon").symlink_to(target)
        self.assertEqual(improve_board.wiki_view(self.repo)["pages"], {})


class WikiFolderLinkTests(unittest.TestCase):
    def test_a_symlinked_wiki_folder_is_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp, ["src/app.py"])
            elsewhere = Path(tmp) / "elsewhere"
            elsewhere.mkdir()
            (elsewhere / "wiki.toon").write_text("built_at: x\npages:\n  index:\n    title: leaked\n")
            (repo / ".improve" / "wiki").symlink_to(elsewhere, target_is_directory=True)
            self.assertEqual(improve_board.wiki_view(repo)["pages"], {})
            result = subprocess.run([sys.executable, str(ROOT / "scripts" / "improve_graph.py"), "--repo", str(repo),
                                     "wiki", "index"], capture_output=True, text=True, timeout=60)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("leaked", result.stdout)

    def test_a_symlinked_state_folder_is_not_read(self):
        # Every other state helper refuses a symlinked .improve; the wiki readers do too.
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp, ["src/app.py"])
            elsewhere = Path(tmp) / "elsewhere"
            (elsewhere / "wiki").mkdir(parents=True)
            (elsewhere / "wiki" / "wiki.toon").write_text("built_at: x\npages:\n  index:\n    title: leaked\n")
            (repo / ".improve").rmdir()
            (repo / ".improve").symlink_to(elsewhere, target_is_directory=True)
            self.assertEqual(improve_board.wiki_view(repo)["pages"], {})
            result = subprocess.run([sys.executable, str(ROOT / "scripts" / "improve_graph.py"), "--repo", str(repo),
                                     "wiki", "index"], capture_output=True, text=True, timeout=60)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("leaked", result.stdout)

class WikiPageSourceTests(unittest.TestCase):
    def test_the_route_and_script_are_wired(self):
        server = (ROOT / "scripts" / "improve_board.py").read_text()
        self.assertIn('if target.path == "/api/wiki":', server)
        self.assertIn('"/wiki.js": ("wiki.js", "text/javascript; charset=utf-8")', server)
        page = (ASSETS / "index.html").read_text()
        self.assertIn('<script src="/wiki.js" defer></script>', page)
        for marker in ('id="knowledge-graph-mode"', 'id="knowledge-wiki-mode"', 'id="graph-panel"',
                       'id="wiki-panel"', 'id="wiki-pages"', 'id="wiki-page"'):
            self.assertIn(marker, page)

    def test_wiki_pages_are_built_from_data_never_from_html(self):
        script = (ASSETS / "wiki.js").read_text()
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("insertAdjacentHTML", script)
        self.assertIn('boardJSON("/api/wiki")', script)
        self.assertIn("bindWiki();", (ASSETS / "board.js").read_text())


class WikiModeTests(unittest.TestCase):
    """A graph link always shows the graph; a hidden graph is never drawn or animated."""

    def test_graph_links_and_bookmarks_switch_back_to_the_graph(self):
        graph_js = (ASSETS / "graph.js").read_text()
        opener = graph_js[graph_js.index("function openGraphNode(id) {"):]
        self.assertIn("showGraphMode();", opener[:opener.index("\n}")])
        location = (ASSETS / "workflow.js").read_text()
        reader = location[location.index("function readViewLocation"):]
        self.assertIn("showGraphMode();", reader[:reader.index("loadNodeDetail(node, true);")])

    def test_a_hidden_graph_is_not_rendered_or_animated(self):
        self.assertIn('else if (activeView === "graph" && !wikiMode) renderGraphView();',
                      (ASSETS / "board.js").read_text())
        graph_js = (ASSETS / "graph.js").read_text()
        self.assertIn('if (graphFrame || activeView !== "graph" || wikiMode) return;', graph_js)
        self.assertIn('if (activeView !== "graph" || wikiMode || document.hidden) return;', graph_js)

class WikiSessionsViewTests(unittest.TestCase):
    def test_sessions_and_hotspots_are_shown_on_the_board(self):
        script = (ASSETS / "wiki.js").read_text()
        self.assertIn('["sessions", "Sessions"]', script)
        # The Sessions page has its own renderer (the source pin moved with the per-page split).
        self.assertIn("sessions: wikiSessionsPage,", script)
        self.assertIn('wikiSection("Files that resist change"', script)

class WikiRelationsViewTests(unittest.TestCase):
    def test_notes_open_in_the_graph_and_dream_relations_are_shown(self):
        script = (ASSETS / "wiki.js").read_text()
        helper = script[script.index("function wikiGraphButton("):]
        self.assertIn("openGraphNode(id)", helper[:helper.index("\n}")])
        self.assertIn('wikiGraphButton(row.id, row.text, "wiki-text wiki-open")', script)
        self.assertIn('wikiSection("Contradictions", wikiContradictions(page.contradictions || []))', script)
        self.assertIn('element("small", "wiki-related", "Related: ")', script)

class WikiFilterTests(unittest.TestCase):
    def test_a_filter_narrows_notes_by_text_area_or_file(self):
        page = (ASSETS / "index.html").read_text()
        self.assertIn('id="wiki-filter"', page)
        self.assertIn('type="search"', page[page.index('id="wiki-filter"') - 200:page.index('id="wiki-filter"') + 200])
        script = (ASSETS / "wiki.js").read_text()
        matcher = script[script.index("function wikiMatches("):]
        matcher = matcher[:matcher.index("\n}")]
        for field in ("row.text", "row.areas", "row.files"):
            self.assertIn(field, matcher)
        self.assertIn("wikiMatches(row)", script[script.index("function wikiNoteRows("):])
        self.assertIn('$("wiki-filter").addEventListener("input"', script)
        self.assertIn('id="wiki-filter-count"', page)
        # While filtering, kinds with no match are left out rather than shown empty.
        self.assertIn("if (!wikiFilter || wikiShown > before)", script)
        # Pages without notes (changes, sessions, dreams) must not claim "0 of 0 notes match".
        self.assertIn("The filter applies to notes; this page has none.", script)
        self.assertIn("wikiFilter && wikiTotal", script)

class WikiCappedViewTests(unittest.TestCase):
    def test_capped_lists_say_how_many_they_leave_out(self):
        script = (ASSETS / "wiki.js").read_text()
        helper = script[script.index("function wikiCapped("):]
        # Capped lists keep the strongest by default; the source pin moved with the `kept` word.
        self.assertIn('kept = "strongest"', helper[:helper.index("\n}")])
        self.assertIn("Showing the ${kept}", helper[:helper.index("\n}")])
        self.assertIn("wikiCapped(group.notes.length, group.total)", script)
        self.assertIn("wikiCapped((page.knowledge || []).length, page.total)", script)
        # Area pages resolve their ids against every row, including those beyond the kind caps.
        self.assertIn("...(knowledge.more || [])", script)
        self.assertIn("const rows = new Map(wikiAllNotes(pages).map((row) => [row.id, row]));", script)

class WikiWordingTests(unittest.TestCase):
    def test_board_counts_use_singular_and_plural(self):
        script = (ASSETS / "wiki.js").read_text()
        self.assertIn('const wikiPlural = (count, word) => `${countText(count)} ${count === 1 ? word : word + "s"}`;', script)
        self.assertNotRegex(script, r"\$\{row\.notes\} notes")
        self.assertNotIn("attempts verified", script, "attempts go through wikiPlural")

class WikiAnnouncementTests(unittest.TestCase):
    def test_the_page_is_not_a_live_region_but_the_filter_count_is(self):
        # A live page would be read out in full on every filter keystroke.
        page = (ASSETS / "index.html").read_text()
        article = page[page.index('<article id="wiki-page"'):]
        article = article[:article.index(">")]
        self.assertNotIn("aria-live", article)
        count = page[page.index('id="wiki-filter-count"') - 20:]
        self.assertIn('aria-live="polite"', count[:count.index(">")])
        script = (ASSETS / "wiki.js").read_text()
        self.assertIn('$("wiki-page").focus({ preventScroll: true });', script, "opening a page moves focus there")

class WikiTopicsViewTests(unittest.TestCase):
    def test_topics_are_listed_and_rendered(self):
        script = (ASSETS / "wiki.js").read_text()
        self.assertIn('["topics", "Topics"]', script)
        self.assertIn("topics: wikiTopicsPage,", script)
        renderer = script[script.index("function wikiTopicsPage("):]
        renderer = renderer[:renderer.index("\n}")]
        self.assertIn("wikiAllNotes(pages)", renderer, "topic notes resolve like area notes")
        self.assertIn("No topics yet", renderer)
        self.assertIn('wikiCapped(page.groups.length, page.total, "largest")', renderer)
        # A topic is named after a file path: its heading keeps the path's case.
        self.assertIn('section.classList.add("wiki-topic")', renderer)
        css = (ASSETS / "board.css").read_text()
        rule = css[css.index(".wiki-topic h4 {"):]
        self.assertIn("text-transform: none;", rule[:rule.index("}")])

class WikiLocationTests(unittest.TestCase):
    def test_a_refresh_in_wiki_mode_renders_the_wiki_not_the_requests(self):
        board = (ASSETS / "board.js").read_text()
        chain = board[board.index('if (activeView === "overview") renderOverview();'):]
        chain = chain[:chain.index("else renderRequests();")]
        self.assertIn('else if (activeView === "graph" && wikiMode) renderWikiView();', chain)
        script = (ASSETS / "wiki.js").read_text()
        view = script[script.index("function renderWikiView() {"):]
        self.assertIn("if (!wikiData) loadWiki();", view[:view.index("\n}")])
        # A bookmark that switches pages while the wiki is loaded must re-render it.
        self.assertIn("else if (wikiOnScreen !== wikiPage) renderWiki();", view[:view.index("\n}")])
        self.assertIn("wikiOnScreen = wikiPage;", script[script.index("function renderWiki() {"):])

    def test_wiki_pages_are_bookmarkable(self):
        location = (ASSETS / "workflow.js").read_text()
        writer = location[location.index("function writeViewLocation() {"):location.index("function readViewLocation() {")]
        self.assertIn('if (activeView === "graph" && wikiMode) params.set("wiki", wikiPage);', writer)
        reader = location[location.index("function readViewLocation() {"):]
        reader = reader[:reader.index("resetHistory();")]
        self.assertIn('pendingView.get("wiki")', reader)
        self.assertIn("applyKnowledgeMode(true);", reader)
        # A node bookmark shows the graph and wins over a wiki page.
        self.assertLess(reader.index("showGraphMode();"), reader.index("applyKnowledgeMode(true);"))

    def test_only_built_pages_can_be_opened(self):
        # Inherited names (constructor, __proto__, toString) must not count as pages or renderers.
        script = (ASSETS / "wiki.js").read_text()
        self.assertIn("const wikiOwn = (object, key) => Object.prototype.hasOwnProperty.call(object, key);", script)
        self.assertIn("if (!wikiOwn(pages, wikiPage)) {", script)
        self.assertIn("const render = wikiOwn(WIKI_PAGE_RENDERERS, name) ? WIKI_PAGE_RENDERERS[name] : null;", script)
        fallback = script[script.index("if (!wikiOwn(pages, wikiPage)) {"):]
        self.assertIn("writeViewLocation();", fallback[:fallback.index("}")])

class WikiLiveBoardTests(unittest.TestCase):
    def test_an_open_board_reloads_the_wiki_when_a_session_ends(self):
        # The run-end build happens in the same sync that bumps the graph revision.
        script = (ASSETS / "wiki.js").read_text()
        view = script[script.index("function renderWikiView() {"):]
        view = view[:view.index("\n}")]
        self.assertIn("snapshot?.graph?.revision", view)
        self.assertIn("wikiRevision", view)
        loader = script[script.index("async function loadWiki() {"):]
        self.assertIn("wikiRevision = snapshot?.graph?.revision", loader[:loader.index("\n}")])
        # Every graph write during a session bumps the revision; the wiki changes only at session
        # end, so an unchanged document keeps the page (and keyboard focus) as it is.
        self.assertIn("fresh.built_at === wikiData.built_at && wikiOnScreen === wikiPage", loader[:loader.index("\n}")])
        self.assertIn("if (!unchanged) renderWiki();", loader[:loader.index("\n}")])
        self.assertNotIn("wikiData = null", view)

    def test_metrics_and_export_follow_the_wiki(self):
        graph_js = (ASSETS / "graph.js").read_text()
        self.assertIn("function renderKnowledgeMetrics() {", graph_js)
        self.assertIn("renderKnowledgeMetrics();", graph_js[graph_js.index("function renderGraphView() {"):])
        script = (ASSETS / "wiki.js").read_text()
        view = script[script.index("function renderWikiView() {"):]
        self.assertIn("renderKnowledgeMetrics();", view[:view.index("\n}")])
        self.assertIn("function exportWiki() {", script)
        board = (ASSETS / "board.js").read_text()
        self.assertIn('if (activeView === "graph") return wikiMode ? exportWiki() : exportGraph();', board)
        self.assertIn('wikiMode ? "Export wiki ↗" : "Export graph ↗"', board)
        self.assertIn('wikiMode ? "Export every wiki page as JSON" : "Export the nodes and links currently shown"', board)
        # Switching modes re-renders through renderCards, so the export label and title follow at once.
        switch = script[script.index("function setKnowledgeMode(wiki) {"):]
        self.assertIn("renderCards();", switch[:switch.index("\n}")])

class WikiStyleTests(unittest.TestCase):
    def rule(self, css, selector):
        start = css.index("\n" + selector + " {")
        return css[start:css.index("}", start)]

    def test_the_wiki_uses_board_tokens_and_stacks_on_phones(self):
        css = (ASSETS / "board.css").read_text()
        self.assertIn("grid-template-columns: minmax(180px, 220px) minmax(0, 1fr);", self.rule(css, ".wiki-panel"))
        pressed = self.rule(css, '.knowledge-switch button[aria-pressed="true"]')
        self.assertIn("var(--accent)", pressed)
        page = self.rule(css, ".wiki-page")
        for token in ("var(--surface)", "var(--radius-lg)", "var(--shadow-1)"):
            self.assertIn(token, page)
        self.assertIn(".mobile .wiki-panel", css)
        self.assertIn("grid-template-columns: minmax(0, 1fr);", self.rule(css, ".mobile .wiki-panel"))
        # Colours come from theme tokens only, so both themes work.
        block = css[css.index("/* Knowledge wiki */"):css.index("/* End knowledge wiki */")]
        self.assertNotRegex(block, r"#[0-9a-fA-F]{3,8}\b")

if __name__ == "__main__":
    unittest.main()
