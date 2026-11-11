"""Shared fixtures for knowledge-graph tests: repositories, ledgers, a long-session
simulator, and an invariant checker. No agents are launched."""
import json
import os
from pathlib import Path
import random
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from support import budget, slow  # noqa: E402,F401

import improve_graph as graph  # noqa: E402

ID_PREFIX = {kind: kind + ":" for kind in graph.TYPES}
GIT_IDENTITY = ["-c", "user.name=Improve tests", "-c", "user.email=tests@example.invalid"]


def make_repo(root, files):
    """A git repository whose tracked files are `files`, with an empty .improve directory."""
    root = Path(root)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n")
    if files:
        subprocess.run(["git", "-C", str(root), "add", "--", *files], check=True)
        subprocess.run(["git", "-C", str(root), *GIT_IDENTITY, "commit", "-qm", "fixture"], check=True)
    (root / ".improve").mkdir(exist_ok=True)
    return root


def untrack(repo, *names):
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "--", *names], check=True)
    subprocess.run(["git", "-C", str(repo), *GIT_IDENTITY, "commit", "-qm", "remove"], check=True)


def write_ledger(directory, rows):
    """Atomically replace backlog.jsonl, as the skill does."""
    temp = Path(directory) / "backlog.tmp"
    temp.write_text("".join(json.dumps(row) + "\n" for row in rows))
    temp.replace(Path(directory) / "backlog.jsonl")


def write_run(directory, run_id, gates=None, outcome=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "supervisor.json").write_text(json.dumps({"run_id": run_id, "outcome": outcome}))
    gates = gates or {}
    (directory / "run.json").write_text(json.dumps({
        "gates": {name: f"run {name}" for name in gates}, "gate_seconds": gates}))


def archive(repo, name, run_id, rows, gates=None):
    directory = Path(repo) / ".improve" / "history" / name
    write_run(directory, run_id, gates, outcome="completed")
    write_ledger(directory, rows)
    return directory


def load(repo):
    return graph.load(graph.graph_path(repo), repair=False)


def store_bytes(repo):
    """The live graph on disk (database plus write-ahead log), history included."""
    path = graph.graph_path(repo)
    return sum(name.stat().st_size for name in (path, Path(str(path) + "-wal")) if name.exists())


def check_disk(test, repo, limit_mb=10):
    """The live store stays bounded, and its checkpoint is a copy, not a second growth path."""
    size = store_bytes(repo)
    test.assertLess(size, limit_mb * 1024 * 1024, "database and log, history included")
    backup = graph.backup_path(graph.graph_path(repo))
    if backup.exists():
        test.assertLessEqual(backup.stat().st_size, graph.graph_path(repo).stat().st_size * 1.1 + 65536)


def copy_store(source_repo, target_repo):
    """A consistent copy of a live graph database, the way SQLite itself copies one."""
    import sqlite3
    source = sqlite3.connect(str(graph.graph_path(source_repo)))
    target = sqlite3.connect(str(graph.graph_path(target_repo)))
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()


def finished_counts(rows_by_file):
    return {path: {status: rows.count(status) for status in graph.FINISHED} for path, rows in rows_by_file.items()}


def check_invariants(test, value, refined=False):
    """Structural truths that must hold after any sequence of graph operations.
    Problems are collected and asserted once, so a failure lists every violation."""
    nodes, edges, aliases = value["nodes"], value["edges"], value["aliases"]
    problems = []
    for node_id, node in nodes.items():
        if node.get("id") != node_id or node.get("type") not in graph.TYPES:
            problems.append(f"malformed node {node_id}")
        elif not node_id.startswith(ID_PREFIX[node["type"]]):
            problems.append(f"{node_id} has type {node['type']}")
        if node_id in aliases:
            problems.append(f"{node_id} is both a node and an alias")
    for key, edge in edges.items():
        src, rel, dst = edge.get("src"), edge.get("rel"), edge.get("dst")
        if key != graph.edge_key(src, rel, dst):
            problems.append(f"edge key {key} does not match its endpoints")
        if rel not in graph.RELATIONS:
            problems.append(f"{key}: unknown relation")
        if src not in nodes or dst not in nodes:
            problems.append(f"{key}: dangling endpoint")
        if src == dst:
            problems.append(f"{key}: self link")
        if rel == "co_changed" and not src < dst:
            problems.append(f"{key}: unordered co_changed")
    for alias, target in aliases.items():
        if graph.resolve(value, target) not in nodes:
            problems.append(f"alias {alias} does not resolve")
    if refined:
        counts = {kind: 0 for kind in graph.TYPES}
        pinned = {kind: 0 for kind in graph.TYPES}
        for node in nodes.values():
            counts[node["type"]] += 1
            pinned[node["type"]] += bool(node.get("pinned"))
            if node["type"] in ("file", "area"):
                stats = node.get("stats", {})
                if stats.get("attempts") != stats.get("done", 0) + stats.get("rejected", 0) + stats.get("blocked", 0):
                    problems.append(f"{node['id']}: attempts do not add up")
                if not 0 < stats.get("success", 0) < 1:
                    problems.append(f"{node['id']}: success outside (0, 1)")
        for kind, limit in (("task", graph.MAX_TASKS), ("file", graph.MAX_FILES), ("insight", graph.MAX_INSIGHTS),
                            ("blocker", graph.MAX_BLOCKERS)):
            if counts[kind] - (pinned[kind] if kind != "task" else 0) > limit:
                problems.append(f"{counts[kind]} {kind} nodes exceed {limit}")
        if len(aliases) > graph.MAX_ALIASES or len(value["refine"]["history"]) > graph.MAX_REFINE_LOG:
            problems.append("aliases or refine log unbounded")
        # Persisted state must be strict JSON (no NaN/Infinity) and survive a round trip.
        if json.loads(json.dumps(value, allow_nan=False)) != value:
            problems.append("graph does not survive a JSON round trip")
    test.assertEqual(problems, [])


def check_matches_ledger(test, value, run_id, rows, repo, pruned=()):
    """Every task in the ledger is in the graph with exactly the links the ledger implies."""
    tasks = {}
    for row in rows:
        tasks[row["id"]] = {**tasks.get(row["id"], {}), **row}
    out, users = {}, {}
    for edge in value["edges"].values():
        out.setdefault(edge["src"], {}).setdefault(edge["rel"], set()).add(edge["dst"])
        if edge["rel"] == "used_in":
            users.setdefault(edge["dst"], set()).add(edge["src"])
    graph_tasks = {node["task_id"] for node in value["nodes"].values()
                   if node["type"] == "task" and node.get("run_id") == run_id}
    test.assertEqual(graph_tasks, set(tasks) - set(pruned), "graph tasks must equal ledger tasks")
    problems = []
    for task_id, task in tasks.items():
        if task_id in pruned:
            continue
        node_id = f"task:{run_id}:{task_id}"
        node, links = value["nodes"][node_id], out.get(node_id, {})
        expected = {
            "status": task.get("status", "ready"),
            "in_area": {"area:" + task.get("area", "general")},
            "touches": {"file:" + path for path in graph.task_files(task, repo)},
            "blocked": task.get("status") == "blocked",
            "used_in": {ref for ref in ({graph.resolve(value, ref) for ref in task.get("graph_refs", [])} & set(value["nodes"]))
                        if value["nodes"][ref]["type"] == "insight"},
        }
        actual = {"status": node["status"], "in_area": links.get("in_area"), "touches": links.get("touches", set()),
                  "blocked": len(links.get("blocked_by", ())) == 1 if links.get("blocked_by") else False,
                  "used_in": users.get(node_id, set())}
        if links.get("blocked_by") and len(links["blocked_by"]) > 1:
            problems.append(f"{node_id}: several blockers")
        for key in expected:
            if expected[key] != actual[key]:
                problems.append(f"{node_id} {key}: ledger {expected[key]!r}, graph {actual[key]!r}")
    test.assertEqual(problems, [])


class LongSession:
    """A deterministic, compressed improvement run: tasks appear, move through every status,
    get unblocked, change files and areas, cite knowledge, lose ledger lines, and files get
    deleted, while the daemon syncs every cycle and refines on a schedule."""

    AREAS = ("ui", "security", "performance", "quality", "resilience", "docs")
    STATUSES = ("done", "done", "done", "rejected", "blocked")

    def __init__(self, repo, files, run_id="live", seed=48, tasks_per_cycle=4):
        self.repo, self.files, self.run_id = Path(repo), list(files), run_id
        self.random = random.Random(seed)
        self.tasks_per_cycle = tasks_per_cycle
        self.rows, self.vanished, self.insights, self.removed_files = {}, set(), [], set()
        self.state = self.repo / ".improve"
        write_run(self.state, run_id, {"py.fast": 9, "ts.fast": 31})

    def live_files(self):
        return [name for name in self.files if name not in self.removed_files]

    def new_task(self, number):
        return dict(id=f"L{number}", title=f"Live task {number}", claim=f"Improve module {number % 37}",
                    area=self.random.choice(self.AREAS), status="ready",
                    paths=self.random.sample(self.live_files()[:300], self.random.randint(1, 4)))

    def step(self, cycle):
        """Apply one cycle of ledger and knowledge changes. Returns what changed."""
        rand = self.random
        for _ in range(self.tasks_per_cycle):
            task = self.new_task(len(self.rows) + len(self.vanished))
            self.rows[task["id"]] = task
        for task in rand.sample(list(self.rows.values()), min(10, len(self.rows))):
            if task["status"] == "ready":
                task["status"] = "in_progress"
            elif task["status"] == "in_progress":
                task["status"] = rand.choice(self.STATUSES)
                task["verification"] = rand.choice(["py.fast passed", "ts.fast passed", "manual"])
                if task["status"] == "blocked":
                    task["blocked_reason"] = f"Simulator {rand.randint(14, 19)} unavailable at {rand.randint(1000, 9999)}"
                elif task["status"] == "rejected":
                    task["note"] = rand.choice(["benchmark noise", "counter-scenario failed", "gate timed out"])
            elif task["status"] == "blocked" and rand.random() < 0.4:
                task["status"] = "done"  # Unblocked: the blocked_by link must disappear.
                task.pop("blocked_reason", None)
            if rand.random() < 0.08:
                task["paths"] = rand.sample(self.live_files()[:300], rand.randint(1, 3))
            if rand.random() < 0.05:
                task["area"] = rand.choice(self.AREAS)
            if self.insights and rand.random() < 0.15:
                task["graph_refs"] = rand.sample(self.insights, min(len(self.insights), rand.randint(1, 2)))
            elif task.get("graph_refs") and rand.random() < 0.1:
                task["graph_refs"] = []
        if cycle % 23 == 11 and len(self.rows) > 20:
            # A damaged ledger line set aside by repair: the task leaves the ledger.
            victim = rand.choice(sorted(self.rows))
            self.vanished.add(victim)
            del self.rows[victim]
        write_ledger(self.state, list(self.rows.values()))
        if cycle % 3 == 0:
            self.note(cycle)
        if cycle % 41 == 40:
            candidates = [name for name in self.live_files()[300:] if name not in self.removed_files]
            if candidates:
                name = rand.choice(candidates)
                untrack(self.repo, name)
                self.removed_files.add(name)

    def note(self, cycle):
        rand = self.random
        with graph.locked(self.repo) as value:
            if self.insights and rand.random() < 0.3:
                # A paraphrase of known knowledge must reinforce, not duplicate.
                original = value["nodes"].get(graph.resolve(value, rand.choice(self.insights)))
                if original:
                    graph.add_insight(value, original["summary"].upper() + "!", original["kind"])
                    return
            module = rand.randint(0, 60)
            node = graph.add_insight(
                value, f"Module {module} cycle {cycle} pitfall: run gate {rand.choice(['alpha', 'beta', 'gamma'])} serially",
                rand.choice(["pitfall", "technique", "fact"]), about=[rand.choice(self.live_files()[:300])], repo=self.repo)
            self.insights.append(node["id"])
            if rand.random() < 0.2:
                graph.rate(value, rand.choice(self.insights), rand.choice(["useful", "wrong", "pin", "archive", "restore"]))

    def ledger_rows(self):
        return list(self.rows.values())


def environment(home):
    """Isolate the global learning store for a test."""
    previous = os.environ.get("IMPROVE_LEARNING_HOME")
    os.environ["IMPROVE_LEARNING_HOME"] = str(home)
    return previous


def restore_environment(previous):
    if previous is None:
        os.environ.pop("IMPROVE_LEARNING_HOME", None)
    else:
        os.environ["IMPROVE_LEARNING_HOME"] = previous


class World:
    """A repository with a known ground truth, for measuring how accurate dreaming is.

    Files belong to modules; modules belong to areas. Each module has knowledge that truly
    applies to it, worded with the module's own vocabulary, and an environment blocker that
    stops some of its tasks. Agents mostly cite the right knowledge (and then succeed more
    often) but sometimes cite knowledge from another module (noise that fails more often).
    Every inferred link can then be graded against the truth.
    """

    VOCABULARY = ("cache", "queue", "schema", "token", "socket", "render", "parser", "ledger", "upload",
                  "search", "billing", "session", "export", "thumbnail", "webhook", "locale")
    TOPICS = ("needs a warm fixture before its tests", "breaks when run in parallel with others",
              "must be migrated before deploys", "times out unless the retry budget is raised")

    def __init__(self, root, modules=12, files_per_module=8, insights_per_module=3, seed=7,
                 true_cite=0.6, noise_cite=0.08):
        self.random = random.Random(seed)
        self.modules = modules
        self.true_cite, self.noise_cite = true_cite, noise_cite
        self.files = {module: [f"mod{module:02d}/part{index}.py" for index in range(files_per_module)]
                      for module in range(modules)}
        self.module_of_file = {path: module for module, paths in self.files.items() for path in paths}
        self.area_of_module = {module: LongSession.AREAS[module % len(LongSession.AREAS)] for module in range(modules)}
        self.repo = make_repo(root, [path for paths in self.files.values() for path in paths])
        self.state = self.repo / ".improve"
        write_run(self.state, "world", {"py.fast": 7})
        self.rows, self.insights, self.module_of_insight = [], {}, {}
        self.blocker_of_module = {module: f"Device farm {self.VOCABULARY[module % 16]} lane offline"
                                  for module in range(modules)}
        with graph.locked(self.repo) as value:
            for module in range(modules):
                word = self.VOCABULARY[module % len(self.VOCABULARY)]
                for index in range(insights_per_module):
                    text = (f"The {word} module {self.TOPICS[index % len(self.TOPICS)]}; "
                            f"{word} {word}-store and {word}-worker share state m{module}")
                    node = graph.add_insight(value, text, ("pitfall", "technique", "fact")[index % 3])
                    self.insights.setdefault(module, []).append(node["id"])
                    self.module_of_insight[node["id"]] = module

    def task(self, number):
        rand = self.random
        module = rand.randrange(self.modules)
        files = self.files[module]
        anchor = rand.randrange(len(files))
        # Neighboring parts of a module change together, which co-change edges should learn.
        touched = sorted({files[anchor], *(files[(anchor + 1) % len(files)] for _ in range(rand.random() < 0.6))})
        cited = [node_id for node_id in self.insights[module] if rand.random() < self.true_cite]
        noise = rand.random() < self.noise_cite * 3
        if noise:
            cited.append(rand.choice(self.insights[rand.choice([m for m in range(self.modules) if m != module])]))
        blocked = rand.random() < 0.1
        success = 0.45 + (0.35 if cited and not noise else 0) - (0.3 if noise else 0)
        status = "blocked" if blocked else ("done" if rand.random() < success else "rejected")
        row = dict(id=f"w{number}", title=f"World task {number}", area=self.area_of_module[module], status=status,
                   paths=touched, graph_refs=cited, verification="py.fast ok")
        if blocked:
            row["blocked_reason"] = self.blocker_of_module[module] if rand.random() < 0.9 else "Random flake"
        return row

    def advance(self, tasks=5):
        for _ in range(tasks):
            self.rows.append(self.task(len(self.rows)))
        write_ledger(self.state, self.rows)

    # -------------------------------------------------------- grading

    def truth(self, value, edge):
        """True, False, or None (no ground truth for this kind of link)."""
        nodes = value["nodes"]
        src, dst = nodes.get(edge["src"], {}), nodes.get(edge["dst"], {})
        module = self.module_of_insight.get(edge["src"])
        if edge["rel"] == "applies_to":
            if dst.get("type") == "file":
                return self.module_of_file.get(dst["label"]) == module
            if dst.get("type") == "area":
                return self.area_of_module.get(module) == dst["label"]
        if edge["rel"] in ("co_cited", "similar_to"):
            other = self.module_of_insight.get(edge["dst"])
            return None if module is None or other is None else module == other
        if edge["rel"] == "affects":
            blocker_module = next((m for m, text in self.blocker_of_module.items() if text == src.get("label")), None)
            return None if blocker_module is None else blocker_module == self.module_of_file.get(dst.get("label"))
        return None

    def grade(self, value):
        """Precision of inferred links per rule, and coverage of true insight-to-file links."""
        scores = {}
        for edge in value["edges"].values():
            if not edge.get("inferred"):
                continue
            verdict = self.truth(value, edge)
            if verdict is None:
                continue
            bucket = scores.setdefault(edge["rule"], [0, 0])
            bucket[0] += verdict
            bucket[1] += 1
        precision = {rule: right / total for rule, (right, total) in scores.items()}
        counts = {rule: total for rule, (_, total) in scores.items()}
        inferred_pairs = {(edge["src"], edge["dst"]) for edge in value["edges"].values()
                          if edge.get("inferred") and edge["rel"] == "applies_to"}
        worked = {}
        for row in self.rows:
            for path in row["paths"]:
                worked[path] = worked.get(path, 0) + (row["status"] == "done")
        eligible = [(insight, "file:" + path) for module, insights in self.insights.items() for insight in insights
                    for path in self.files[module] if worked.get(path, 0) >= 4 and insight in value["nodes"]]
        recall = sum(pair in inferred_pairs for pair in eligible) / len(eligible) if eligible else 0.0
        return precision, counts, recall
