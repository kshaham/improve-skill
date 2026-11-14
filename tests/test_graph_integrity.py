"""Knowledge integrity: what counts as the same knowledge, whose wording wins, and that proof
and evidence are neither lost nor counted twice (review 2026-11-10, section 1 and 5)."""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_support import environment, load, make_repo, restore_environment, write_ledger, write_run  # noqa: E402
import improve_dream as dreaming  # noqa: E402
import improve_graph as graph  # noqa: E402
import improve_graph_store as store  # noqa: E402
import improve_learning as learning  # noqa: E402


class GraphCase(unittest.TestCase):
    files = ["src/a.py", "src/b.py", "src/api/db.py", "src/web/db.py"]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="improve graph integrity ")
        self.previous = environment(Path(self.temp.name) / "home")
        self.repo = make_repo(Path(self.temp.name) / "repo", self.files).resolve()
        self.state = self.repo / ".improve"
        write_run(self.state, "live", {"test": 3})

    def tearDown(self):
        restore_environment(self.previous)
        self.temp.cleanup()

    def note(self, text, kind="fact", source="agent", about=()):
        with graph.locked(self.repo) as value:
            return graph.add_insight(value, text, kind, source, list(about), repo=self.repo)["id"]

    def insights(self):
        return {key: node for key, node in load(self.repo)["nodes"].items() if node["type"] == "insight"}


class DuplicateDetectionTests(unittest.TestCase):
    """`duplicate` must agree with word overlap only when the notes say the same thing."""

    def test_should_not_merge_notes_with_opposite_polarity(self):
        cases = [
            ("Always wrap the connection pool acquire in the retry helper because deadlocks recur under load",
             "Never wrap the connection pool acquire in the retry helper because deadlocks recur under load"),
            ("Mock the payment gateway client in integration tests because the sandbox is flaky and slow",
             "Do not mock the payment gateway client in integration tests because the sandbox is flaky and slow"),
            ("Run the migrations before the seed script when the schema changes in staging",
             "Run the migrations after the seed script when the schema changes in staging"),
            ("Enable the query cache for the reporting endpoints in production deployments",
             "Disable the query cache for the reporting endpoints in production deployments"),
            ("Don't retry the upload when the storage service returns a quota error",
             "Retry the upload when the storage service returns a quota error"),
        ]
        for left, right in cases:
            with self.subTest(left=left):
                self.assertGreaterEqual(graph.similarity(left, right), 0.6, "the wording really does overlap")
                self.assertFalse(graph.duplicate(left, right))

    def test_should_not_merge_notes_with_different_numbers_or_files(self):
        self.assertFalse(graph.duplicate("Set the gate timeout to 5 seconds for the slow suite",
                                         "Set the gate timeout to 50 seconds for the slow suite"))
        self.assertFalse(graph.duplicate("Run the schema migration before the tests in src/api/db.py",
                                         "Run the schema migration before the tests in src/web/db.py"))
        self.assertFalse(graph.duplicate("Pitfall number 1: the cache in module 1 must be invalidated first",
                                         "Pitfall number 2: the cache in module 2 must be invalidated first"))

    def test_should_merge_true_paraphrases(self):
        self.assertTrue(graph.duplicate("Run the full unit suite before committing changes to the scheduler",
                                        "Run the full unit suite before committing changes to the scheduler module"))
        self.assertTrue(graph.duplicate("Use the retry helper for the pool", "Use the retry helper for the pool!"))

    def test_should_tokenize_scripts_other_than_latin(self):
        self.assertTrue(graph.tokens("Кэш нужно сбрасывать перед записью"))
        self.assertTrue(graph.tokens("缓存必须先清除"))
        self.assertTrue(graph.duplicate("Кэш нужно сбрасывать перед записью", "Кэш нужно сбрасывать перед записью."))
        self.assertNotEqual(graph.normalized("Кэш нужно"), "", "a non-Latin note gets its own id")

    def test_should_keep_digits_as_tokens(self):
        self.assertIn("5", graph.tokens("wait 5 seconds"))
        self.assertIn("50", graph.tokens("wait 50 seconds"))


class NoteWritesTests(GraphCase):
    def test_should_keep_numbered_notes_apart(self):
        ids = {self.note(f"Pitfall number {k}: the cache layer in module {k % 4} must be invalidated before the writer "
                         "flushes", "pitfall") for k in range(8)}
        self.assertEqual(len(ids), 8)
        self.assertEqual(len(self.insights()), 8)

    def test_should_record_a_negated_note_as_new_knowledge(self):
        first = self.note("Always wrap the pool acquire in the retry helper because deadlocks recur", "technique")
        second = self.note("Never wrap the pool acquire in the retry helper because deadlocks recur", "technique")
        self.assertNotEqual(first, second)
        self.assertEqual(self.insights()[first]["reinforced"], 1, "a contradiction is not confirmation")

    def test_should_keep_the_users_wording_when_it_rewords_an_agent_note(self):
        agent = self.note("Mock the payment gateway client in integration tests because the sandbox is slow and flaky",
                          "convention")
        user = self.note("Mock the payment gateway client in integration tests since the sandbox is slow and flaky",
                         "convention", source="user")
        nodes = self.insights()
        self.assertNotEqual(agent, user)
        self.assertNotIn(agent, nodes, "the agent's note was folded into the user's")
        self.assertEqual(nodes[user]["source"], "user")
        self.assertIn("since the sandbox", nodes[user]["summary"])
        self.assertEqual(nodes[user]["reinforced"], 2)
        value = load(self.repo)
        self.assertEqual(graph.resolve(value, agent), user, "citations of the old id still count")

    def test_should_not_relabel_an_agent_note_when_the_user_contradicts_it(self):
        agent = self.note("Mock the payment gateway client in integration tests because the sandbox is flaky", "convention")
        user = self.note("Do not mock the payment gateway client in integration tests because the sandbox is flaky",
                         "convention", source="user")
        nodes = self.insights()
        self.assertEqual(nodes[agent]["source"], "agent")
        self.assertEqual(nodes[user]["source"], "user")
        self.assertTrue(nodes[user]["summary"].startswith("Do not mock"))
        text = graph.brief(load(self.repo), "g", "r")
        self.assertNotIn("Mock the payment gateway client in integration tests because the sandbox is flaky [from the user",
                         text)

    def test_should_only_reinforce_when_an_agent_repeats_user_knowledge(self):
        user = self.note("Payments code is frozen until the audit is finished", "decision", source="user")
        again = self.note("Payments code is frozen until the audit is finished.", "decision")
        self.assertEqual(user, again)
        node = self.insights()[user]
        self.assertEqual((node["source"], node["reinforced"]), ("user", 2))

    def test_should_strip_control_and_bidi_characters(self):
        node_id = self.note("Use the\x1b[31m retry‮ helper for flaky pool acquires", "technique")
        summary = self.insights()[node_id]["summary"]
        self.assertNotIn("\x1b", summary)
        self.assertNotIn("‮", summary)

    def test_should_protect_a_learning_lesson_that_rewords_an_agent_lesson(self):
        agent = self.note("Run the integration suite serially because parallel runs flake on the shared database",
                          "lesson")
        learning.add_lesson(self.repo, "Run the integration suite serially since parallel runs flake on the shared database")
        graph.sync(self.repo)
        lessons = [node for node in self.insights().values() if node.get("kind") == "lesson"]
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]["source"], "learning", "the learning store's copy is protected")
        self.assertNotIn(agent, self.insights())


class RefineMergeTests(GraphCase):
    def test_should_keep_user_wording_and_citation_credit_through_refine(self):
        with graph.locked(self.repo) as value:
            graph.upsert(value, "insight:agent", "insight", "x", 0, kind="fact", source="agent", reinforced=5, useful=1,
                         wrong=0, summary="Seed the cache before the cart benchmark runs", cite_base={"done": 4})
            graph.upsert(value, "insight:user", "insight", "x", 0, kind="fact", source="user", reinforced=1, useful=0,
                         wrong=0, summary="Seed the cache before the cart benchmark runs!")
            report = graph.refine(value, self.repo)
        self.assertEqual(report["merged"], 1)
        nodes = self.insights()
        self.assertEqual(list(nodes), ["insight:user"])
        self.assertEqual(nodes["insight:user"]["cite_base"], {"done": 4}, "proof moves with the knowledge")
        self.assertEqual(nodes["insight:user"]["cited_done"], 4)

    def test_should_prefer_proven_knowledge_as_the_keeper(self):
        with graph.locked(self.repo) as value:
            graph.upsert(value, "insight:proven", "insight", "x", 0, kind="fact", source="agent", reinforced=1, useful=0,
                         wrong=0, summary="Seed the cache before the cart benchmark runs", cite_base={"done": 6})
            graph.upsert(value, "insight:popular", "insight", "x", 0, kind="fact", source="agent", reinforced=4,
                         useful=0, wrong=0, summary="Seed the cache before the cart benchmark runs.")
            graph.refine(value, self.repo)
        self.assertIn("insight:proven", self.insights())

    def test_should_return_citation_credit_when_a_merged_note_is_restored(self):
        with graph.locked(self.repo) as value:
            graph.upsert(value, "insight:keep", "insight", "x", 0, kind="fact", source="user", reinforced=1, useful=0,
                         wrong=0, summary="Seed the cache before the cart benchmark runs")
            graph.upsert(value, "insight:drop", "insight", "x", 0, kind="fact", source="agent", reinforced=2, useful=0,
                         wrong=0, summary="Seed the cache before the cart benchmark runs.", cite_base={"done": 3})
        with graph.locked(self.repo) as value:  # Stored first: history keeps only what was saved.
            graph.refine(value, self.repo)
        with graph.locked(self.repo) as value:
            self.assertEqual(value["nodes"]["insight:keep"]["cite_base"], {"done": 3})
            graph.restore_node(value, "insight:drop", "test")
            self.assertEqual(value["nodes"]["insight:keep"]["cite_base"], {"done": 0})
            self.assertEqual(value["nodes"]["insight:drop"]["cite_base"], {"done": 3})


class EvidenceAccountingTests(GraphCase):
    def cited_tasks(self, insight, count):
        return [{"id": f"t{index}", "status": "done", "area": "quality", "title": f"task {index}",
                 "paths": ["src/a.py"], "graph_refs": [insight]} for index in range(count)]

    def support(self, insight):
        edge = load(self.repo)["edges"].get(f"{insight}|applies_to|file:src/a.py")
        return edge and edge["support"]

    def test_should_not_double_count_a_restored_pruned_task(self):
        insight = self.note("Always close the cursor before returning from the pool helper", "technique")
        write_ledger(self.state, self.cited_tasks(insight, 6))
        graph.sync(self.repo, dream_after=True)
        self.assertEqual(self.support(insight), {"done": 6, "failed": 0})
        bound = graph.MAX_TASKS
        try:
            graph.MAX_TASKS = 3
            with graph.locked(self.repo) as value:
                dreaming.dream(value, self.repo)
            self.assertEqual(self.support(insight), {"done": 6, "failed": 0}, "pruned outcomes are retained")
            graph.MAX_TASKS = bound
            with graph.locked(self.repo) as value:
                graph.restore_node(value, "task:live:t0", "probe")
            with graph.locked(self.repo) as value:
                dreaming.dream(value, self.repo)
            self.assertEqual(self.support(insight), {"done": 6, "failed": 0}, "a restored task is counted once")
        finally:
            graph.MAX_TASKS = bound

    def test_should_keep_outcomes_beyond_two_hundred_tasks(self):
        self.assertGreaterEqual(dreaming.MAX_EVIDENCE, graph.MAX_TASKS)


class RejectedLinkTests(GraphCase):
    def test_should_follow_a_rejection_through_a_merge(self):
        with graph.locked(self.repo) as value:
            graph.upsert(value, "insight:old", "insight", "x", 0, kind="fact", summary="old wording here", reinforced=1)
            graph.upsert(value, "insight:new", "insight", "x", 0, kind="fact", summary="new wording here", reinforced=1)
            graph.upsert(value, "file:src/a.py", "file", "src/a.py")
            key = graph.edge_key("insight:old", "applies_to", "file:src/a.py")
            value["dream"]["rejected"][key] = {"by": "user", "at": "2026-01-01T00:00:00Z", "reason": "unrelated"}
            graph.merge_into(value, "insight:new", "insight:old")
            dreaming.dream(value, self.repo)
            self.assertIn(graph.edge_key("insight:new", "applies_to", "file:src/a.py"), value["dream"]["rejected"])
            self.assertNotIn(key, value["dream"]["rejected"])


class HygieneTests(GraphCase):
    def test_should_normalize_path_spellings(self):
        self.assertEqual(graph.safe_path("src//a.py"), "src/a.py")
        self.assertEqual(graph.safe_path("./src/./a.py"), "src/a.py")
        self.assertEqual(graph.safe_path("src/a.py:12"), "src/a.py")
        self.assertEqual(graph.safe_path(".improvements/notes.md"), ".improvements/notes.md")
        self.assertIsNone(graph.safe_path(".improve/graph.db"))
        self.assertIsNone(graph.safe_path("../outside.py"))
        self.assertIsNone(graph.safe_path("."))

    def test_should_keep_hex_looking_words_in_blocker_keys(self):
        self.assertIn("defaced", graph.blocker_key("the page was defaced by the deploy"))
        self.assertEqual(graph.blocker_key("push of a1b2c3d4e5 rejected"), graph.blocker_key("push of 9f8e7d6c5b rejected"))
        self.assertIn("defaced", learning.blocker_key("the page was defaced by the deploy"))

    def test_should_treat_like_wildcards_in_history_queries_literally(self):
        with graph.locked(self.repo) as value:
            graph.upsert(value, "insight:pct", "insight", "100% coverage pitfall", 0, kind="fact", summary="x")
            graph.upsert(value, "insight:any", "insight", "1000 coverage pitfall", 0, kind="fact", summary="x")
        with graph.locked(self.repo) as value:
            graph.remove_nodes(value, ["insight:pct", "insight:any"], reason="test", op="forgotten")
        self.assertEqual([row["id"] for row in graph.history(self.repo, query="100%")["rows"]], ["insight:pct"])
        self.assertEqual(graph.history(self.repo, query="_")["total"], 0)

    def test_should_rebuild_a_search_index_built_by_older_tokenization(self):
        self.note("Кэш нужно сбрасывать перед записью в журнал", "pitfall")
        path = self.state / "graph.db"
        con = sqlite3.connect(path)
        con.execute("DELETE FROM meta WHERE key='fts_version'")
        con.execute("DELETE FROM node_fts")
        con.commit()
        con.close()
        self.note("Another note forces a write so the index is rebuilt", "fact")
        con = sqlite3.connect(path)
        version = con.execute("SELECT value FROM meta WHERE key='fts_version'").fetchone()
        rows = con.execute("SELECT count(*) FROM node_fts").fetchone()[0]
        con.close()
        self.assertEqual(int(version[0]), store.INDEX_VERSION)
        self.assertGreaterEqual(rows, 2)
        with graph.reading(self.repo) as value:
            graph.load_search(value, "сбрасывать")
            self.assertTrue(graph.search(value, "сбрасывать"))


if __name__ == "__main__":
    unittest.main()
