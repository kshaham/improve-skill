"""The knowledge wiki: built from the knowledge graph only when an improve session finishes.

The graph is what a run relies on while it works; the wiki is a readable, token-cheap digest
of it for people and later sessions. It grows with new knowledge, cleans itself up (archived,
forgotten, and merged knowledge leaves the pages but stays in the change log), and shows what
the graph's dreaming found.
"""
import json
from pathlib import Path
import tempfile
import unittest

from graph_support import graph, make_repo

import improve_toon
import improve_wiki as wiki


def finish_run(repo, run_id, outcome="completed"):
    state = Path(repo) / ".improve"
    (state / "run.json").write_text(json.dumps({"started_at": run_id, "run_id": run_id, "outcome": outcome}))


def start_run(repo, run_id):
    finish_run(repo, run_id, outcome=None)


def note(repo, text, kind="technique", about=()):
    with graph.locked(repo) as value:
        return graph.add_insight(value, text, kind=kind, about=about, repo=repo)["id"]


class SessionEndOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(self.tmp.name, ["src/app.py"])
        self.wiki = self.repo / ".improve" / "wiki"

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_live_run_never_builds_the_wiki_even_when_the_graph_dreams(self):
        start_run(self.repo, "run-1")
        note(self.repo, "Retry the upload twice before reporting a network failure", about=["file:src/app.py"])
        result = graph.sync(self.repo, dream_after=True)
        self.assertFalse(self.wiki.exists(), "the graph is the in-run source of truth")
        self.assertEqual(result.get("wiki", {}).get("skipped"), "a run is still live")

    def test_a_sync_without_the_end_of_session_dream_does_not_build_it(self):
        finish_run(self.repo, "run-1")
        graph.sync(self.repo)
        self.assertFalse(self.wiki.exists())

    def test_the_end_of_session_dream_builds_the_wiki(self):
        note(self.repo, "Retry the upload twice before reporting a network failure", about=["file:src/app.py"])
        finish_run(self.repo, "run-1")
        result = graph.sync(self.repo, dream_after=True)
        for name in ("index.md", "knowledge.md", "changes.md", "dreams.md", "wiki.toon", "manifest.json"):
            self.assertTrue((self.wiki / name).is_file(), name)
        self.assertIn("Retry the upload twice", (self.wiki / "knowledge.md").read_text())
        self.assertGreaterEqual(result["wiki"]["pages"], 4)
        self.assertEqual(json.loads((self.wiki / "manifest.json").read_text())["run_id"], "run-1")

    def test_the_supervisor_decides_when_it_exists(self):
        # An agent may write "completed" to run.json early; the supervisor rejects it and the run
        # goes on, so only the supervisor's outcome can end a supervised session.
        state = self.repo / ".improve"
        (state / "supervisor.json").write_text(json.dumps({"run_id": "run-1", "outcome": None}))
        finish_run(self.repo, "run-1")
        note(self.repo, "Retry the upload twice before reporting a network failure", about=["file:src/app.py"])
        result = graph.sync(self.repo, dream_after=True)
        self.assertFalse(self.wiki.exists())
        self.assertEqual(result["wiki"].get("skipped"), "a run is still live")
        (state / "supervisor.json").write_text(json.dumps({"run_id": "run-1", "outcome": "completed"}))
        graph.sync(self.repo, dream_after=True)
        self.assertTrue((self.wiki / "index.md").is_file())

    def test_a_wiki_failure_never_fails_the_sync(self):
        finish_run(self.repo, "run-1")
        original = wiki.build
        wiki.build = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full"))
        try:
            result = graph.sync(self.repo, dream_after=True)
        finally:
            wiki.build = original
        self.assertIn("disk full", result["wiki"]["error"])


class GrowAndCleanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(self.tmp.name, ["src/app.py", "web/view.js"])
        self.wiki = self.repo / ".improve" / "wiki"

    def tearDown(self):
        self.tmp.cleanup()

    def end_session(self, run_id):
        finish_run(self.repo, run_id)
        return graph.sync(self.repo, dream_after=True)

    def test_new_knowledge_appears_and_the_change_log_says_so(self):
        note(self.repo, "Retry the upload twice before reporting a network failure", about=["file:src/app.py"])
        self.end_session("run-1")
        later = note(self.repo, "Render the board list with keyed rows to keep focus on refresh",
                     kind="pitfall", about=["file:web/view.js"])
        self.end_session("run-2")
        self.assertIn("keyed rows", (self.wiki / "knowledge.md").read_text())
        changes = (self.wiki / "changes.md").read_text()
        self.assertIn("run-2", changes)
        self.assertIn("keyed rows", changes[changes.index("run-2"):])
        manifest = json.loads((self.wiki / "manifest.json").read_text())
        self.assertIn(later, manifest["notes"])

    def test_archived_knowledge_leaves_the_pages_but_stays_in_the_change_log(self):
        kept = note(self.repo, "Retry the upload twice before reporting a network failure", about=["file:src/app.py"])
        gone = note(self.repo, "Cache the board snapshot for five seconds between refreshes",
                    about=["file:web/view.js"])
        self.end_session("run-1")
        with graph.locked(self.repo) as value:
            value["nodes"][gone]["archived"] = "marked wrong by the user"
        self.end_session("run-2")
        knowledge = (self.wiki / "knowledge.md").read_text()
        self.assertIn("Retry the upload twice", knowledge)
        self.assertNotIn("Cache the board snapshot", knowledge)
        changes = (self.wiki / "changes.md").read_text()
        self.assertIn("Cache the board snapshot", changes)
        self.assertIn("marked wrong by the user", changes)
        self.assertIn(kept, json.loads((self.wiki / "manifest.json").read_text())["notes"])

    def test_a_rebuild_in_the_same_session_replaces_its_change_log_entry(self):
        note(self.repo, "Retry the upload twice before reporting a network failure", about=["file:src/app.py"])
        self.end_session("run-1")
        note(self.repo, "Render the board list with keyed rows to keep focus on refresh", about=["file:web/view.js"])
        self.end_session("run-2")
        self.end_session("run-2")
        manifest = json.loads((self.wiki / "manifest.json").read_text())
        self.assertEqual([row["run_id"] for row in manifest["sessions"]], ["run-1", "run-2"])
        self.assertEqual(manifest["sessions"][-1]["counts"]["added"], 1, "still compared with run-1's build")

    def test_pages_for_areas_that_no_longer_exist_are_removed(self):
        pages, manifest = wiki.pages(wiki_graph(areas=["ui", "docs"]), None, "t1", "run-1")
        wiki.write(self.wiki, pages, manifest)
        self.assertTrue((self.wiki / "areas" / "docs.md").is_file())
        pages, manifest = wiki.pages(wiki_graph(areas=["ui"]), manifest, "t2", "run-2")
        wiki.write(self.wiki, pages, manifest)
        self.assertTrue((self.wiki / "areas" / "ui.md").is_file())
        self.assertFalse((self.wiki / "areas" / "docs.md").exists(), "a stale page cleans itself up")

    def test_the_first_build_counts_knowledge_without_listing_it_as_changes(self):
        value = wiki_graph(areas=["ui"], notes={f"insight:{i}": f"Knowledge number {i} for the wiki" for i in range(9)})
        pages, manifest = wiki.pages(value, None, "t1", "run-1")
        first = manifest["sessions"][-1]
        self.assertTrue(first["first"])
        self.assertEqual(first["counts"]["added"], 9)
        self.assertEqual(first["added"], [], "a first build is not a list of changes")

    def test_the_change_log_is_bounded(self):
        manifest = None
        for index in range(wiki.CHANGE_SESSIONS + 5):
            value = wiki_graph(areas=["ui"], notes={f"insight:{index:04d}": f"Knowledge number {index} for the wiki"})
            _, manifest = wiki.pages(value, manifest, f"t{index}", f"run-{index}")
        self.assertEqual(len(manifest["sessions"]), wiki.CHANGE_SESSIONS)
        self.assertEqual(manifest["sessions"][-1]["run_id"], f"run-{wiki.CHANGE_SESSIONS + 4}")


class DreamsAndToonTests(unittest.TestCase):
    def test_dream_results_and_verdicts_are_shown(self):
        value = wiki_graph(areas=["ui"], notes={"insight:a1": "Proven way to keep labels steady while dragging"})
        value["nodes"]["insight:a1"]["verdict"] = "proven"
        value["dream"]["history"] = [{"at": "t1", "tick": 3, "inferred": {"new": 4, "kept": 9, "dropped": 1},
                                      "archived": 2, "forgotten": 1, "proposals": 1, "gaps": 2, "proven": 1,
                                      "contested": 0}]
        value["dream"]["proposals"] = [{"id": "dream:abc", "members": ["insight:a1"], "text": "merge these"}]
        pages, _ = wiki.pages(value, None, "t1", "run-1")
        dreams = wiki.markdown("dreams", pages["dreams"])
        self.assertIn("4 new", dreams)
        self.assertIn("dream:abc", dreams)
        self.assertIn("proven", wiki.markdown("knowledge", pages["knowledge"]))

    def test_the_toon_form_round_trips_and_is_smaller_than_json(self):
        notes = {f"insight:{index:04d}": f"Knowledge number {index} about the board and its graph view"
                 for index in range(60)}
        pages, _ = wiki.pages(wiki_graph(areas=["ui", "docs", "quality"], notes=notes), None, "t1", "run-1")
        text = improve_toon.dumps(pages)
        self.assertEqual(improve_toon.loads(text), json.loads(json.dumps(pages)))
        self.assertLess(len(text), 0.75 * len(json.dumps(pages, indent=1)), "TOON must cut tokens")
        compact = json.dumps(pages, separators=(",", ":"))
        self.assertLess(len(text), 0.8 * len(compact), "smaller even than compact JSON")

    def test_markdown_never_carries_raw_html_from_notes(self):
        value = wiki_graph(areas=["ui"], notes={"insight:x": "Escape <script>alert(1)</script> in labels"})
        page = wiki.markdown("knowledge", wiki.pages(value, None, "t1", "run-1")[0]["knowledge"])
        self.assertNotIn("<script>", page)


class AreaPageTests(unittest.TestCase):
    def test_area_pages_reach_notes_and_files_through_the_tasks_that_touched_them(self):
        # In a real graph a note is about a file, and only tasks link files to areas.
        value = wiki_graph(areas=["ui", "docs"])
        value["edges"] = {}
        value["nodes"]["file:web/view.js"] = {"id": "file:web/view.js", "type": "file", "label": "web/view.js",
                                              "stats": {"attempts": 3, "success": 0.5}}
        value["nodes"]["task:r:1"] = {"id": "task:r:1", "type": "task", "label": "Fix the list"}
        value["nodes"]["insight:n1"] = {"id": "insight:n1", "type": "insight", "kind": "pitfall", "score": 0.5,
                                        "summary": "Keyed rows keep focus when the list refreshes"}
        for src, rel, dst in [("task:r:1", "touches", "file:web/view.js"), ("task:r:1", "in_area", "area:ui"),
                              ("insight:n1", "about", "file:web/view.js")]:
            value["edges"][f"{src}|{rel}|{dst}"] = {"src": src, "rel": rel, "dst": dst}
        pages, _ = wiki.pages(value, None, "t1", "run-1")
        rendered = wiki.render(pages)
        ui = rendered["areas/ui"]
        self.assertIn("Keyed rows keep focus", ui)
        self.assertIn("web/view.js", ui)
        self.assertNotIn("Keyed rows", rendered["areas/docs"])
        self.assertEqual(pages["areas/ui"]["knowledge"], ["insight:n1"], "area pages carry ids, not copies")
        self.assertEqual(pages["index"]["areas"][1]["notes"], 1, "the index counts the area's notes")

class SessionsAndHotspotsTests(unittest.TestCase):
    def test_the_sessions_page_lists_each_run_newest_first_with_what_it_learned(self):
        value = wiki_graph(areas=["ui"], notes={"insight:a": "Keyed rows keep focus when the list refreshes",
                                                 "insight:b": "Retry the upload twice before reporting failure"})
        value["nodes"]["insight:a"]["run_id"] = "run-2"
        value["runs"] = [{"run_id": "run-1", "outcome": "completed", "tasks": 5, "done": 3, "rejected": 2,
                          "blocked": 0, "ingested_at": "2026-11-13T10:00:00Z"},
                         {"run_id": "run-2", "outcome": "halted: gate failed", "tasks": 4, "done": 1, "rejected": 1,
                          "blocked": 2, "ingested_at": "2026-11-14T10:00:00Z"}]
        pages, _ = wiki.pages(value, None, "t1", "run-2")
        rows = pages["sessions"]["runs"]
        self.assertEqual([row["run_id"] for row in rows], ["run-2", "run-1"])
        self.assertEqual(rows[0]["notes"], 1)
        self.assertEqual(rows[0]["outcome"], "halted: gate failed")
        text = wiki.render(pages)["sessions"]
        self.assertIn("run-2", text)
        self.assertIn("1/4 done", text)
        self.assertIn("Sessions", wiki.render(pages)["index"])

    def test_a_run_without_an_outcome_is_not_called_live(self):
        # The wiki is only built once the current session has finished, so a run with no outcome
        # crashed or was interrupted; it must not read "live" in every later build.
        value = wiki_graph()
        value["runs"] = [{"run_id": "crashed", "outcome": None, "tasks": 3, "done": 1}]
        pages, _ = wiki.pages(value, None, "t1", "later")
        self.assertEqual(pages["sessions"]["runs"][0]["outcome"], "ended without an outcome")
        self.assertNotIn("live", wiki.render(pages)["sessions"])

    def test_the_sessions_page_is_bounded(self):
        value = wiki_graph()
        value["runs"] = [{"run_id": f"run-{i}", "tasks": 1, "done": 1} for i in range(wiki.SESSION_ROWS + 10)]
        pages, _ = wiki.pages(value, None, "t1", "x")
        self.assertEqual(len(pages["sessions"]["runs"]), wiki.SESSION_ROWS)

    def test_the_index_lists_files_that_resist_change(self):
        value = wiki_graph(areas=["ui"])
        value["nodes"]["file:web/hard.js"] = {"id": "file:web/hard.js", "type": "file", "label": "web/hard.js",
                                              "stats": {"attempts": 9, "done": 1, "rejected": 6, "blocked": 2,
                                                        "open": 0, "success": 0.12}}
        value["nodes"]["file:web/easy.js"] = {"id": "file:web/easy.js", "type": "file", "label": "web/easy.js",
                                              "stats": {"attempts": 9, "done": 9, "rejected": 0, "blocked": 0,
                                                        "open": 0, "success": 0.95}}
        pages, _ = wiki.pages(value, None, "t1", "run-1")
        files = [row["file"] for row in pages["index"]["hotspots"]]
        self.assertIn("web/hard.js", files)
        self.assertNotIn("web/easy.js", files)
        self.assertIn("web/hard.js", wiki.render(pages)["index"])

class DreamRelationsTests(unittest.TestCase):
    def graph(self):
        value = wiki_graph(areas=["ui"], notes={
            "insight:always": "Always cache the board snapshot between refreshes",
            "insight:never": "Never cache the board snapshot between refreshes",
            "insight:keyed": "Render the list with keyed rows to keep focus",
            "insight:focus": "Restore focus after the list re-renders"})
        for left, rel, right in [("insight:always", "contradicts", "insight:never"),
                                 ("insight:keyed", "similar_to", "insight:focus"),
                                 ("insight:keyed", "co_cited", "insight:always")]:
            value["edges"][f"{left}|{rel}|{right}"] = {"src": left, "rel": rel, "dst": right, "inferred": True,
                                                       "confidence": 0.7}
        return value

    def test_contradicting_notes_are_listed_for_a_person_to_settle(self):
        pages, _ = wiki.pages(self.graph(), None, "t1", "run-1")
        pairs = pages["dreams"]["contradictions"]
        self.assertEqual(len(pairs), 1)
        self.assertEqual({pairs[0]["a"], pairs[0]["b"]}, {"insight:always", "insight:never"})
        text = wiki.render(pages)["dreams"]
        self.assertIn("Always cache the board snapshot", text[text.index("## Contradictions"):])

    def test_each_note_lists_the_knowledge_dreaming_relates_to_it(self):
        pages, _ = wiki.pages(self.graph(), None, "t1", "run-1")
        rows = {row["id"]: row for group in pages["knowledge"]["groups"] for row in group["notes"]}
        self.assertEqual(sorted(rows["insight:keyed"]["related"].split()), ["insight:always", "insight:focus"])
        self.assertEqual(rows["insight:focus"]["related"], "insight:keyed", "relations run both ways")
        self.assertEqual(rows["insight:never"]["related"], "", "a contradiction is not a relation")
        self.assertIn("related:", wiki.render(pages)["knowledge"])

    def test_a_contradicting_pair_is_never_listed_as_related(self):
        # Dreaming can infer both similar_to and contradicts for two short opposite notes.
        value = self.graph()
        value["edges"]["insight:always|similar_to|insight:never"] = {
            "src": "insight:always", "rel": "similar_to", "dst": "insight:never", "inferred": True, "confidence": 0.9}
        pages, _ = wiki.pages(value, None, "t1", "run-1")
        rows = {row["id"]: row for group in pages["knowledge"]["groups"] for row in group["notes"]}
        self.assertEqual(rows["insight:never"]["related"], "")
        self.assertEqual(rows["insight:always"]["related"], "insight:keyed")
        self.assertEqual(len(pages["dreams"]["contradictions"]), 1)

    def test_archived_notes_are_left_out_of_relations(self):
        value = self.graph()
        value["nodes"]["insight:never"]["archived"] = "marked wrong by the user"
        pages, _ = wiki.pages(value, None, "t1", "run-1")
        self.assertEqual(pages["dreams"]["contradictions"], [])

class CappedPageTests(unittest.TestCase):
    def test_a_capped_page_says_how_many_notes_it_leaves_out(self):
        notes = {f"insight:{i:04d}": f"Knowledge number {i} for a large graph" for i in range(wiki.NOTES_PER_PAGE + 25)}
        pages, _ = wiki.pages(wiki_graph(areas=["ui"], notes=notes), None, "t1", "run-1")
        group = pages["knowledge"]["groups"][0]
        self.assertEqual(len(group["notes"]), wiki.NOTES_PER_PAGE)
        self.assertEqual(group["total"], wiki.NOTES_PER_PAGE + 25)
        self.assertEqual(pages["areas/ui"]["total"], wiki.NOTES_PER_PAGE + 25)
        text = wiki.render(pages)
        self.assertIn(f"Showing the strongest {wiki.NOTES_PER_PAGE} of {wiki.NOTES_PER_PAGE + 25}", text["knowledge"])
        self.assertIn(f"Showing the strongest {wiki.AREA_NOTES} of {wiki.NOTES_PER_PAGE + 25}", text["areas/ui"])

    def test_every_note_an_area_lists_has_a_row(self):
        # Area notes can rank below the kind cap of the knowledge page; they must still render.
        cap = wiki.NOTES_PER_PAGE
        notes = {f"insight:{i:04d}": f"Fact number {i} recorded for the wiki" for i in range(cap * 2)}
        area_cap = wiki.AREA_NOTES
        value = wiki_graph(areas=["ui", "low"], notes=notes)
        value["edges"] = {}
        for i in range(cap - area_cap // 2, cap + area_cap // 2):
            key = f"insight:{i:04d}"
            value["edges"][f"{key}|about|area:low"] = {"src": key, "rel": "about", "dst": "area:low"}
        pages, _ = wiki.pages(value, None, "t1", "run-1")
        listed = pages["areas/low"]["knowledge"]
        self.assertEqual(len(listed), area_cap)
        text = wiki.render(pages)["areas/low"]
        for key in listed:
            self.assertIn(notes[key], text)
        self.assertNotIn("Showing the strongest", text, "nothing is left out of this area")
        knowledge = wiki.render(pages)["knowledge"]
        self.assertNotIn(notes[f"insight:{cap + 10:04d}"], knowledge, "the knowledge page keeps its cap")

    def test_the_lookup_table_for_area_notes_stays_small(self):
        # 2,000 facts over 10 areas, no overlap: the extra rows must stay bounded by the area cap.
        notes = {f"insight:{i:05d}": f"Fact number {i} recorded for the wiki" for i in range(2000)}
        value = wiki_graph(areas=[f"a{n}" for n in range(10)], notes=notes)
        value["edges"] = {}
        for i in range(2000):
            key = f"insight:{i:05d}"
            value["edges"][f"{key}|about|area:a{i % 10}"] = {"src": key, "rel": "about", "dst": f"area:a{i % 10}"}
        pages, _ = wiki.pages(value, None, "t1", "run-1")
        self.assertLessEqual(len(pages["knowledge"]["more"]), 10 * wiki.AREA_NOTES)
        self.assertEqual(len(pages["areas/a3"]["knowledge"]), wiki.AREA_NOTES)
        self.assertEqual(pages["areas/a3"]["total"], 200)
        self.assertIn(f"Showing the strongest {wiki.AREA_NOTES} of 200", wiki.render(pages)["areas/a3"])

    def test_an_uncapped_page_says_nothing_extra(self):
        pages, _ = wiki.pages(wiki_graph(areas=["ui"], notes={"insight:a": "One small note for the wiki"}), None, "t", "r")
        self.assertNotIn("Showing the strongest", "".join(wiki.render(pages).values()))

class WordingTests(unittest.TestCase):
    def test_counts_use_singular_and_plural(self):
        value = wiki_graph(areas=["ui"], notes={"insight:a": "One note for the wiki"})
        value["runs"] = [{"run_id": "run-1", "outcome": "completed", "tasks": 1, "done": 1}]
        value["nodes"]["insight:a"]["run_id"] = "run-1"
        value["nodes"]["area:ui"]["stats"] = {"attempts": 1, "done": 1, "rejected": 0, "blocked": 0, "success": 1}
        text = "".join(wiki.render(wiki.pages(value, None, "t1", "run-1")[0]).values())
        for wrong in ("1 notes", "1 live notes", "1 areas", "1/1 attempts"):
            self.assertNotIn(wrong, text)
        for right in ("1 note recorded", "1 live note", "1 area", "1/1 attempt verified"):
            self.assertIn(right, text)

def wiki_graph(areas=(), notes=None):
    """A minimal in-memory graph with the fields the wiki reads."""
    nodes, edges = {}, {}
    for name in areas:
        nodes[f"area:{name}"] = {"id": f"area:{name}", "type": "area", "label": name,
                                 "stats": {"attempts": 4, "done": 3, "rejected": 1, "blocked": 0, "success": 0.7}}
    for index, (node_id, text) in enumerate((notes or {}).items()):
        nodes[node_id] = {"id": node_id, "type": "insight", "kind": "technique", "label": text[:60],
                          "summary": text, "score": 1 - index / 100, "source": "agent",
                          "created_at": "2026-11-14T00:00:00Z"}
        if areas:
            edges[f"{node_id}|about|area:{areas[0]}"] = {"src": node_id, "rel": "about", "dst": f"area:{areas[0]}"}
    return {"nodes": nodes, "edges": edges, "revision": 7, "tick": 3, "runs": [],
            "dream": {"history": [], "proposals": [], "gaps": []}, "aliases": {}}


if __name__ == "__main__":
    unittest.main()
