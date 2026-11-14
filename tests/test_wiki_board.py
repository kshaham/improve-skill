"""The board's Wiki view: /api/wiki serves the wiki built at session end; the page renders it."""
import json
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
        self.assertIn('if (name === "sessions")', script)
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
