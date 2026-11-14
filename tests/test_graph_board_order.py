"""The Knowledge view's ordering filters: the most important nodes only, and the most recently
created. With thousands of nodes, the default balanced view gives every type a share, so the most
valuable knowledge and the newest notes need their own server-side orderings."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import improve_board as board  # noqa: E402
import improve_graph as graph  # noqa: E402
from improve_runtime import StateError  # noqa: E402


def fixture():
    """Sixty files with high scores, forty notes with lower ones, created over forty days."""
    value = graph.empty_graph()
    for index in range(60):
        graph.upsert(value, f"file:src/f{index}.py", "file", f"src/f{index}.py", score=0.5 + index / 100,
                     created_at=f"2026-10-{1 + index % 28:02d}T00:00:00Z")
    for index in range(40):
        graph.upsert(value, f"insight:n{index}", "insight", f"Note {index}", score=index / 100,
                     created_at=f"2026-11-{1 + index % 14:02d}T{index % 24:02d}:00:00Z")
    graph.upsert(value, "insight:pinned", "insight", "Pinned decision", score=0.01, pinned=True,
                 created_at="2026-09-01T00:00:00Z")
    graph.upsert(value, "area:ui", "area", "ui", score=0.2, created_at="2026-11-15T00:00:00Z")
    graph.upsert(value, "file:src/old.py", "file", "src/old.py", score=2.0, archived=True,
                 created_at="2026-11-15T12:00:00Z")
    return value


def ids(view):
    return [node["id"] for node in view["nodes"]]


class OrderTests(unittest.TestCase):
    def test_important_is_the_strongest_nodes_with_pins_first_and_no_type_shares(self):
        value = fixture()
        view = graph.board_view(value, limit=20, order="important")
        self.assertEqual(view["order"], "important")
        self.assertEqual(ids(view)[0], "insight:pinned", "pinned knowledge always leads")
        scores = [value["nodes"][node]["score"] for node in ids(view)[1:]]
        self.assertEqual(scores, sorted(scores, reverse=True), "then strictly by importance")
        strongest = sorted((node for node in value["nodes"].values() if not node.get("archived")),
                           key=lambda node: -node["score"])[:19]
        self.assertEqual(set(ids(view)[1:]), {node["id"] for node in strongest})
        self.assertNotIn("file:src/old.py", ids(view), "archived nodes stay hidden unless asked for")

    def test_balanced_still_gives_each_type_a_share(self):
        view = graph.board_view(fixture(), limit=20)
        self.assertEqual(view["order"], "balanced")
        self.assertTrue(any(node.startswith("insight:n") for node in ids(view)), "notes get a share")
        self.assertIn("area:ui", ids(view))

    def test_recent_is_newest_first(self):
        value = fixture()
        view = graph.board_view(value, limit=15, order="recent")
        self.assertEqual(view["order"], "recent")
        self.assertEqual(ids(view)[0], "area:ui", "the newest live node comes first")
        created = [value["nodes"][node]["created_at"] for node in ids(view)]
        self.assertEqual(created, sorted(created, reverse=True))
        self.assertNotIn("insight:pinned", ids(view), "an old pin does not crowd out recent work")
        with_archived = graph.board_view(value, limit=15, order="recent", include_archived=True)
        self.assertEqual(ids(with_archived)[0], "file:src/old.py")

    def test_orders_respect_type_filters_and_search(self):
        value = fixture()
        notes = graph.board_view(value, limit=10, order="important", types=["insight"])
        self.assertTrue(all(node.startswith("insight:") for node in ids(notes)))
        found = graph.board_view(value, limit=10, order="recent", query="Note")
        self.assertTrue(ids(found))
        self.assertTrue(all(value["nodes"][node]["type"] == "insight" or node in ids(found) for node in ids(found)))

    def test_nodes_without_a_creation_time_sort_last_in_recent(self):
        value = fixture()
        graph.upsert(value, "file:src/undated.py", "file", "src/undated.py", score=3.0)
        del value["nodes"]["file:src/undated.py"]["created_at"]
        view = graph.board_view(value, limit=200, order="recent")
        self.assertEqual(ids(view)[-1], "file:src/undated.py")

    def test_a_focused_view_reports_its_order_name(self):
        value = fixture()
        graph.link(value, "file:src/f1.py", "about", "insight:n1")
        view = graph.board_view(value, focus="file:src/f1.py", depth=2, order="important")
        self.assertEqual(view["order"], "important")
        self.assertIn("insight:n1", ids(view))

    def test_unknown_order_is_refused(self):
        with self.assertRaises(graph.GraphError):
            graph.board_view(fixture(), order="random")

    def test_the_board_api_passes_the_order_and_refuses_junk(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".improve").mkdir()
            view = board.graph_get(repo, "/api/graph", {"order": ["important"], "limit": ["40"]})
            self.assertEqual(view["order"], "important")
            self.assertEqual(board.graph_get(repo, "/api/graph", {})["order"], "balanced")
            with self.assertRaises(StateError):
                board.graph_get(repo, "/api/graph", {"order": ["../../etc"]})



class KnowledgeToolbarTests(unittest.TestCase):
    """The Knowledge toolbar sends the ordering and count."""

    def test_the_toolbar_offers_the_orderings_and_counts(self):
        html = (ROOT / "assets/board/index.html").read_text()
        for value in ("balanced", "important", "recent"):
            self.assertIn(f'<option value="{value}">', html)
        self.assertIn('id="graph-show"', html)

    def test_the_strongest_knowledge_panel_never_claims_none_exists_when_filtered_out(self):
        # With Most important on a large graph the loaded nodes held no notes, and the panel said
        # "No knowledge recorded yet" beside 350 notes.
        source = (ROOT / "assets/board/graph.js").read_text()
        panel = source[source.index("function renderGraphLists() {"):source.index('$("graph-hotspots")')]
        self.assertIn("snapshot?.graph?.insights", panel)
        self.assertIn("No knowledge in this view", panel)

    def test_the_view_sends_the_order_and_count(self):
        source = (ROOT / "assets/board/graph.js").read_text()
        load = source[source.index("async function loadGraph() {"):source.index("function drawGraph() {")]
        self.assertIn('params.set("order", query.order);', load)
        self.assertIn("limit: String(query.focus ? GRAPH_LIMIT : query.show)", load,
                      "a focused neighbourhood keeps its full size whatever Top N is set to")


if __name__ == "__main__":
    unittest.main()
