# Code review: knowledge graph and TOON helper output (origin/main @ 2a756ff)

Date: 2026-11-10. Scope: `scripts/improve_graph.py`, `improve_graph_store.py`, `improve_dream.py`,
`improve_toon.py`, `improve_ledger.py`, the graph and TOON touchpoints in `improve_runtime.py`,
`improve_control.py`, `improve_board.py`, `SKILL.md`, and `references/knowledge-graph.md`.
Local HEAD matched `origin/main` after a fetch. The review is read-only: no repository files were
changed. Findings marked **Reproduced** were confirmed with a small script against the real
modules (the probes are reproduced as regression tests; see the plan). Findings marked **By inspection** come from
reading the code.

This review does not repeat items that the 2026-11-08 architecture review already raised and that
were fixed (protected sources, bounded lock, lesson re-import, and so on).

---

## 1. Critical: knowledge integrity

### 1.1 Near-duplicate detection ignores polarity, so contradictions merge (Reproduced)
`add_insight()` and `refine()` step 1 treat two notes as one when their token Jaccard is at
least 0.75 (`MERGE_SIMILARITY`). Negations ("not", "never", "always", "do not") are ordinary tokens,
so in any sentence of reasonable length one flipped word stays above the threshold.
- "Always wrap the connection pool acquire in the retry helper because ..." compared with
  "Never wrap ..." gives a similarity of **0.857**. The second note *reinforces* the first
  (`reinforced: 2`), and its text is thrown away.
- The result is that a contradiction is recorded as confirmation, and `reinforced > 1` then counts
  as "validated", so dreaming will not archive the note.

### 1.2 Provenance laundering: a user correction becomes the agent's text, labelled "from the user" (Reproduced)
This follows from 1.1. When `add_insight(source="user")` matches an existing agent note, it keeps
the agent's `summary` and sets `source="user"` (`improve_graph.py`, the `if source == "user"` branch).
`merge_into()` does the same during refine (the keep node inherits `source=user` and `pinned`).
- Board note "Do **not** mock the payment gateway client in integration tests ..." when an agent
  note "Mock the payment gateway client ..." already exists: the brief then shows
  `convention: Mock the payment gateway client ... [from the user, x2]`.
- SKILL.md tells the agent to "treat a user `decision` or `convention` like an intake constraint",
  so the user's correction is reversed and enforced. The note is also now protected from fading,
  caps, and forgetting, and the board replies "Saved."
- Fix direction: never let a user write merge into non-user text. Store the user's text as its
  own node, link it with `supersedes`, or replace the summary. Add a negation and number guard to
  the similarity check (section 1.3).

### 1.3 Over-merging of distinct knowledge (Reproduced)
`tokens()` drops every token shorter than two characters and ignores non-ASCII text.
- Eight different notes, "Pitfall number k: the cache layer in module k%4 ...", collapse into
  **one node**, because the digits 0-7 are discarded.
- "Run the schema migration before the tests in src/api/db.py" and the same sentence for
  `src/web/db.py` score **0.80** and merge. The `about` link moves, but the summary that named the
  second file is lost.
- "timeout to 5 seconds" and "timeout to 50 seconds" score **0.857** and merge.
- Notes written in non-Latin scripts produce no tokens, so they can never be deduplicated, found
  by `search`, or linked by `similar_to`.
- Fix direction: keep numeric tokens and single characters next to units. Require every file
  path mentioned in the text to match before merging. Treat a mismatch on negation words as a
  hard "not a duplicate". Use `\w` with `re.UNICODE` for tokens.

### 1.4 Merging discards citation credit from pruned tasks (Reproduced)
`merge_into()` adds `reinforced`, `useful`, and `wrong` into the keep node but not `cite_base`
(credit folded from pruned tasks). A merged node also keeps its own `cited_*`, which refine
recomputes from moved `used_in` edges plus its *own* `cite_base` only.
- In the probe, the drop node had `cite_base={"done": 5}`; after the merge, the keep node had
  `cite_base=None`.
- The refine sort key (`reinforced + 2*useful`) also ignores citation credit, so an old **proven**
  note can be the one dropped, and its proof disappears. This breaks the reference doc's promise
  that "citation credit survives the task".

### 1.5 Restoring a pruned task double-counts dream evidence (Reproduced)
When a task is pruned, `tally()` moves its outcome into the inferred edge's `evidence.retained`.
`restore_node()` reverses `base` and `cite_base` but not `retained`. When the restored task
becomes live evidence again, it is counted twice.
- Probe: 6 real tasks cite insight I on `src/a.py`. After pruning and restoring, the applies_to
  edge reports `support={"done": 8}`. The documentation says "nothing is counted twice".
- Related: `tally` stores at most `MAX_EVIDENCE=200` task ids (sorted lexically). Outcomes of
  tasks beyond 200 are counted live, but they are lost from `retained` when those tasks are pruned.

---

## 2. High: reliability and data loss

### 2.1 Graph-only writes since the last checkpoint are lost on recovery (By inspection)
`graph.backup.db` is written only in sessions where refine or dream ran (at most every 30
minutes). Recovery restores that checkpoint, and `load()` says "ledgers re-fill anything newer".
That is true for tasks, but not for agent `note`, `link`, `rate`, user board notes, pins, rejected
links, or consolidations. None of these appear in any ledger, so up to one refine interval of
curated knowledge is lost without notice.
- Fix direction: an append-only `graph-writes.jsonl` journal of user and agent writes, replayed
  after a restore, or a backup after every user write.

### 2.2 `is_damage()` still treats "no such table" as corruption (By inspection plus unit probe)
`DAMAGE_WORDS` includes `"no such table"`, and the word check runs *before* the isinstance check,
so `OperationalError("no such table: x")` returns `True`. A programming error, or a schema written
by a newer or older version that lacks one table, would set aside the live store and restore the
checkpoint (data loss as in 2.1). Remediation item 3.2 claims "corruption-only damage". Missing
tables are already caught in `Store.open()`, so this word can be removed.

### 2.3 Co-change relationships are erased by age, despite the documentation (By inspection)
`refine()` recomputes `co_changed` only from *live* done tasks. Pruned tasks fold their
`touches` into `base` but not into co-change pairs, so old co-change links vanish once tasks are
pruned. The effects spread:
- Second-generation `via-co-change` `applies_to` links are derived from `co_changed`, so they
  disappear as well.
- The co-change strength divides by `stats.done`, which *includes* folded `base` counts, while
  the weight does not. Strength therefore falls as tasks age, even with no new evidence.

`references/knowledge-graph.md` says "Evidence from pruned tasks is kept as counts, so age alone
never erases a relationship". That holds only for first-generation task-evidenced links.

### 2.4 File renames break the history (Reproduced)
After `git mv src/mod0/file1.py src/mod0/renamed.py`, the old node is flagged `stale`. The new path
starts with no statistics, no `about` knowledge, and no blockers. It sees only its siblings, which
include the stale old node. After 4 dreams the stale node is forgotten if nothing links to it.
- Fix direction: during refine, run `git log --name-status -M --diff-filter=R` from the last-seen
  commit, then alias `file:old` to `file:new` and merge stats and links (an alias mechanism
  already exists).

### 2.5 `improve_ledger.py set --graph-ref` replaces citations instead of appending (By inspection)
`changes["graph_refs"] = list(...)` overwrites the field. If an agent cites one insight early in a
task and another later, the second `set` erases the first. Ingest then deletes the first `used_in`
edge, because "the ledger is the source of truth". Citation credit, `proven` verdicts, and
`applies_to` inference all depend on these edges.
- Fix direction: merge with the existing list, and add `--clear-graph-refs` for the rare
  replacement.

### 2.6 Ledger rewrites have no lock, and they drop rows (By inspection)
`update_task()` and `add_task()` do an unlocked read, modify, and atomic replace. A concurrent
writer can lose an update: the supervisor's `block_active_item`, the board, or parallel
improve-max lanes. `read_rows()` silently skips unparseable lines, and `update_task()` also drops
rows without an `id`, so one `set` permanently deletes damaged or id-less lines that
`repair_ledgers`/quarantine might have recovered.
- Fix direction: take the same operator lock the control CLI uses. Preserve unparseable lines
  verbatim, or move them to quarantine before the rewrite.

### 2.7 Partial-session clash raises an uncaught `RuntimeError` (By inspection)
`Store.save()` raises `RuntimeError("partial session would overwrite unloaded nodes")`. `main()`
catches only `GraphBusy`, `sqlite3`, `StateError`, `OSError`, and `ValueError`, so this case
prints a traceback and exits 1. The documented contract is exit 2 or 3. Current callers fetch
first, so it is not reachable today, but any new partial write path will crash. Make it a
`GraphError`.

### 2.8 Contradiction between `rejected` links and aliases (By inspection)
`dream.rejected` is keyed by raw edge key and is never trimmed or re-keyed. After a merge or
consolidation renames an endpoint (alias), the rejected link can be inferred again under the new
id. The dict also grows without bound.

---

## 3. Medium: TOON output

### 3.1 `uniform()` makes mixed-type arrays larger than compact JSON (Reproduced)
`tabular()` calls `toon.uniform()` on every array of objects, adding `key: null` for every missing
key. If any row has a nested value (`stats` on file nodes, `merged_from`, `restored`, inferred-link
`support`), `tabular_fields()` rejects tabular form. The array then prints in list form *with* all
the padded nulls.
- A 15-row mixed `search` result (10 insights plus 5 files): TOON after `uniform` was **4,075**
  chars, TOON without `uniform` was 3,410, and compact JSON was **3,051**. TOON was 33% *larger*.
- `search` defaults to all node types, so this shape is common.
- Fix direction: call `uniform` only when every resulting cell is primitive, or flatten
  `stats` into `stats_done`/`stats_attempts`/... columns. Add a mixed-shape case to the
  savings test (which today covers only homogeneous samples).

### 3.2 The savings claim is overstated for graph output (Reproduced)
On a realistic synced and dreamed graph (40 tasks, 8 insights), TOON compared with compact JSON
(chars) gave: `search` 0.74, `node` 0.73, `context --toon` 0.74, `hotspots` 0.51, `stats` 0.94.
That is about 26% saved on the graph calls the agent makes most. The README says "35-70% fewer
tokens than JSON", and the plan measured against `indent=2` JSON. The ledger `queue` view does
reach about 70%, mostly because it is a projection, not because of the notation. The wording
should say "compared with pretty JSON" or quote the compact-JSON figure.

### 3.3 The graph CLI never bounds text, unlike the plan (By inspection)
The TOON plan says "text fields in list views are truncated with an explicit marker and a
`--full` switch". `improve_graph.py` has no `truncate` or `--full`: `search`, `node`, and
`context --toon` print full 600-character `summary` and `outcome_note` values per row. The ledger
CLI does truncate, but it applies `truncate()` to *every* string, including `id`, `file`, and
`paths`. A file path over 100 characters (common in monorepos) comes back cut and unusable.
Exclude identifier columns from truncation.

### 3.4 `--format`, `--full`, and `--width` work only before the subcommand (Reproduced)
`improve_graph.py --repo R stats --format json` and `improve_ledger.py --repo R queue --format json`
both fail with "unrecognized arguments" and exit **2**. The prompt hint says "Pass --format json
to any helper" and "`--full` shows everything", and agents naturally put flags at the end. SKILL.md
says a graph helper that exits 2 is "never a reason to block ... continue", so the agent quietly
stops using memory. Fix: add these options to every subparser through a shared parent parser.

### 3.5 `IMPROVE_FORMAT` is not validated (Reproduced)
argparse does not check defaults against `choices`. `IMPROVE_FORMAT=JSON` or `=yaml` silently
prints TOON. Normalize the value to lowercase and warn on unknown values.

### 3.6 The prompt hint does not cover all syntax the encoder emits (By inspection)
The cycle-prompt hint describes `key: value`, `name[N]{f}:`, `name[N]: a,b`, and `- ` items. It
does not describe nested tabular headers (`rows[N]{id,stats{done,attempts}}:`, emitted when a
nested object column is uniform), keyed tables (`name[N:]{...}:` with `key: cells` rows, emitted
for any object of two or more uniform objects), `null`, or quoted cells. These appear in real
outputs (`node` stats, `counts`), so a single extra sentence would avoid misreads.

### 3.7 Docs say "every helper prints TOON", but key views print prose (By inspection)
`context` (the main pre-task query) and `brief` print text by default, and `path`/`proposals` fall
back to sentences. This is reasonable, but SKILL.md and README should say so, and `context` should
probably default to `--toon`, since it is the largest structured read.

---

## 4. Medium: query quality and retrieval

### 4.1 The cycle brief silently drops whole sections (Reproduced)
`brief()` joins its sections in order and then cuts at `max_chars=1800`. With eight notes of about
200 characters, the output was 1,619 characters with **no gate costs and no proposals**. Hotspots
and gaps go next. Pinned or user knowledge longer than about 220 characters per note pushes out
everything else. Fix direction: give each section its own budget, or truncate each note to about
160 characters with its id (the agent can run `node ID`).

### 4.2 Reading does not count as use (By inspection)
Dormancy is based on `last_tick`, which only reinforce, useful, pin, restore, and *finished
citations* update. Knowledge surfaced in `context` or `brief`, read, and acted on without being
cited is archived after 8 dreams (about 24 hours at the default cadence) and forgotten after 8
more. Over a 72-hour unattended run, this loses correct but uncited advice. Consider a cheap
retrieval counter, written in batches by the daemon, rather than writes in read sessions.

### 4.3 `path` almost always routes through hub nodes (By inspection)
`shortest_path()` is an unweighted BFS over all relations, so nearly every pair connects through
`run:<id>` or `area:general` in two steps, which says nothing. Exclude `in_run`/`in_area` (or apply
a degree penalty), and accept `--via REL`.

### 4.4 Context weighting ignores outcome of the citing task (By inspection)
In `context()`, insights reached through a past task get `direct` weight whether that task was
done or rejected. A note cited mostly by rejected tasks ranks like one cited by successes, apart
from its score. Weight it by `outcome(task.status)`.

### 4.5 Blockers never resolve (By inspection)
A blocker linked to a file stays in "Blockers seen here" for as long as its tasks exist, even after
later tasks on that file succeed. There is no `resolved` state, and no rule that a blocker whose
newest occurrence is older than the newest success on the file is historical.

### 4.6 The agent sees its own in-progress task in `context` (By inspection)
The board passes `exclude` to drop the current task, but the CLI has no such option.

### 4.7 Consolidation proposals can group unrelated notes (By inspection)
`propose()` takes *connected components* of `similar_to`/`co_cited` (confidence >= 0.6) and keeps
the top 6 by strength. Chains (A~B~C~D) produce clusters whose kept members may not be related to
each other. Use cliques, a minimum internal density, or community detection (see the Graphify
section) before proposing.

---

## 5. Low: correctness and hygiene

- `safe_path()` does not normalize `a//b` or `a/./b` (they become separate file nodes). It
  rejects any path starting with `.improve`, which includes `.improvements/`.
- `blocker_key()` collapses any run of 7+ hex letters, including English words such as
  "defaced" or "acceded".
- `Store.history()` LIKE queries do not escape `%` or `_`.
- `set_aside()` overwrites an earlier `graph.damaged.db`, losing the evidence of the first
  incident. Add a timestamp suffix.
- `acquire_lock()` is described as "fair (blocking, queued)". `flock` waiters wake in an
  unspecified order on Linux and macOS. Each abandoned wait also leaves a daemon thread holding an
  fd in a long-lived board process.
- `refine()` computes gate and blocker `uses` by scanning every edge for each node (O(N x E)).
  Build the counts in the single edge pass already made.
- `board_view()` builds `adjacency()` up to three times per poll, and the board GET path recomputes
  it on every request even when `load_cached` hits. Cache the view by revision.
- `hotspots`, `brief`, `path`, `proposals`, and `stats` do a full graph load from the CLI. Only
  `stats` and `proposals` are trivial to make partial or meta-only.
- `reading()` does not apply the `version > VERSION` check, so an older skill reads a newer format
  without warning.
- `import_lessons()` near-duplicate-matches an agent lesson and leaves its `source=agent`, so the
  learning-store copy is not protected as documented.
- `tally()` labels a link that merely lacks support (done < 2) as "contradicted by later
  outcomes" in the dropped log.
- Brief text is inserted into the supervised prompt verbatim. Notes come from agent output and
  board input, so repository-derived text can end up steering later cycles. Combined with 1.2,
  agent text can gain "from the user" authority. Consider fencing the brief as data.

---

## 6. Test coverage gaps (relevant to the findings)

Baseline: the fast suite passes on Python 3.13.3 (`Ran 378 tests in 376.6s, OK (skipped=12)`), with one
ResourceWarning (unclosed file at `tests/test_ledger.py:104`). Every defect above is therefore untested:


- No test records a negated, numeric, or different-file near-duplicate and asserts that two nodes
  remain.
- No test covers a user note that matches agent text (provenance).
- No test checks `cite_base` across merge or consolidation.
- No test checks dream `support` after restoring a pruned task.
- No rename test.
- TOON size tests cover only homogeneous rows; nothing exercises a mixed `search` or `node` output.
- No CLI test places `--format` after the subcommand.

---

## 7. Missing features worth considering (independent of Graphify)

1. Code-structure awareness: symbols, imports, and test-to-source mapping (see section 8).
2. Rename and move tracking (2.4).
3. A write journal for replay after recovery (2.1).
4. Contradiction detection: a `contradicts` relation proposed when negation-flipped near-duplicates
   are found, shown to the user on the board.
5. A token budget on `context`/`search` (`--budget 1500`), not only a row `--limit`.
6. Resolved or historical blockers, and gate cost history (variance, not only the latest seconds).
7. Export of the graph (JSON/GraphML/Markdown) for inspection outside the board.

---

## 8. Graphify (Graphify-Labs/graphify) analysis: what to borrow

Reviewed commit `35d1d20` (v0.9.106, 2026-11-10), Apache-2.0 (older contributions MIT). Graphify
is a code-structure graph: tree-sitter AST extraction, optional LLM semantic extraction, Louvain
or Leiden communities, and query, path, explain, and affected commands. It is heavy (networkx,
numpy, rapidfuzz, about 25 grammars, Python 3.10+). improve-skill's graph records *work history*
(tasks, outcomes, lessons) and has no code structure. The two are complementary. Each idea below
is tagged by whether it fits the Python 3.9, standard-library-only rule.

### 8.1 Borrow first (directly fixes findings above)

| Graphify feature | Where | Fixes / improves | Stdlib? |
| --- | --- | --- | --- |
| Token-budgeted rendering: seeds never cut; the rest ordered by hop, then degree; an honest "showing X of Y, raise budget to N" header | `serve._subgraph_to_text` | 3.3 (unbounded text), 4.1 (brief drops sections silently), 7.5 | yes |
| Content-hash staleness on knowledge: store sha256 of each linked file and flag "code changed since, re-verify" at query time | `reflect.py` `code_fingerprint` | 4.2 (dormancy based only on time), stale advice after refactors; a better signal than idle ticks | yes |
| Decayed, corroborated outcome scoring: signed outcomes, 30-day half-life, at least 2 distinct successes before "preferred", "contested" kept separate | `reflect.aggregate_lessons` | Our `verdict` (proven/contested) and reinforcement; would prevent 1.1, where one paraphrase counts as corroboration | yes |
| Dedup hard blocks: numbered siblings, variants, and prefix extensions never merge; short generic labels blocked by an entropy gate; union-find with provenance-first winner selection | `dedup.py` | 1.1-1.3 (polarity, digits, different files), 1.2 (provenance-first: user text wins) | yes (Jaro-Winkler is about 30 lines; O(n^2) is fine at 400 insights) |
| Shared ID normalization (NFKC, casefold, one recipe for all producers) | `ids.normalize_id` | 5 (`a//b`, `./`), 1.3 (non-ASCII tokens) | yes |
| Shrink guard: refuse to drop nodes that this pass cannot explain; fail closed on an unreadable store; dated snapshot before destructive writes | `build.build_merge`, `watch._check_shrink`, `backup_if_protected` | 2.1, 2.2 (over-eager damage recovery), 2.6 (ledger rewrite drops rows) | yes |
| Weakest-hop path confidence, plus a refusal to traverse through 99th-percentile-degree hubs | `serve._path_weakest`, query traversal | 4.3 (`path` through `run:`/`area:` hubs) | yes |
| Refuse-don't-guess name resolution with distinct exit codes (0 answer, 3 ambiguous, 4 not found) | `serve` / CLI | Agents misreading exit 2 as "graph broken" (3.4); clearer `node`/`path` failures | yes |
| Three-tier edge confidence (`EXTRACTED`, `INFERRED`+score, `AMBIGUOUS`) and `also_seen` provenance folding on duplicate edges | `build._fold_confidence`, `_fold_also_seen` | Our inferred links already carry confidence; a uniform tier on *all* edges (recorded vs. ledger-derived vs. dream) would make `context` weighting explicit (4.4) | yes |

### 8.2 Borrow next (new capability)

1. **A Python symbol layer through `ast`** (`extract.py`, `symbol_resolution.py`). Add module,
   class, and function nodes, `imports` edges, import-guided `calls` (EXTRACTED), and unique-name
   calls (INFERRED 0.85). This gives the graph the code structure it lacks. Insights could be
   `about` a function, not only a file. Rename detection (2.4) becomes easier because symbols
   survive moves. Other languages: regex for imports only, or skip. Stdlib, Python-only at full
   fidelity.
2. **Reverse-edge blast radius** (`affected.affected_nodes`, BFS depth 2 over
   calls/imports/inherits, reporting call sites). Run it before a task to list dependent files and
   the gates that cover them. This feeds SKILL.md's "budget a smaller change" advice and
   test-gate selection. Depends on item 1.
3. **Rationale nodes from code comments** (`# WHY:`, `# NOTE:`, `# HACK:`, `# TODO:`, docstrings
   over 20 characters) with secret redaction (`security.redact_secrets`). These would be a
   free `convention`/`fact` seed set from the repo itself, marked `source=code` and kept separate
   from agent notes. Stdlib.
4. **Import-cycle detection** (bounded DFS, length 5 or less, rotations removed) as a discovery
   lane input or a hotspot signal. Stdlib once item 1 exists.
5. **Community detection over files** (label propagation, or a small Louvain) using `co_changed`
   plus `imports` edges. Uses: (a) proposal clustering in place of connected components (4.7);
   (b) "area" inference for tasks the agent labels `general`; (c) a cohesion score per module.
   Copy the determinism tricks: sort edges before partitioning, member-signature label
   stability, and hub exclusion with neighbour-vote reinsertion. Stdlib with some effort.
6. **"Surprising connections" and "suggested questions" reports** (`analyze._surprise_score`,
   bridges by betweenness, isolated nodes, low-cohesion clusters). These map onto the dream
   pass's `gaps` and would give the refill/discovery phase concrete targets: files co-changed
   across top-level directories, and god-files with a low success rate. Stdlib (Brandes is about
   40 lines).
7. **`graph_diff`** between dreams (new, removed, and reversed edges plus a summary line) for the
   board's Knowledge tab and the final report ("what the run learned"). Stdlib.
8. **Export**: JSON, GraphML, and a Markdown "graph report" (summary, hotspots, contested
   knowledge, gaps). Useful for the final report and for review outside the board. Stdlib.

### 8.3 Patterns for the daemon and helpers

- **Racily-clean stat fast path** (trust a size/mtime match only if mtime + 2 s is at or before
  the read time). Lets refine skip `git ls-files` or re-hashing when nothing changed.
- **Cache namespaces versioned by logic** (`v{pkg}-s{schema}`). If dream rules change between
  skill versions, a stored `rules_version` in `meta` lets the next dream recompute inferred links
  rather than mixing old and new evidence.
- **Lock plus `.pending_changes` drain**: a writer that cannot get the lock appends its intent
  and returns immediately, and the holder drains the queue. This fits agent `note` calls during a
  long dream better than the current 30-second wait followed by exit 3.
- **Sanitize LLM- and agent-derived text before returning it** (`sanitize_label`: strip control
  characters, cap length). Apply it to `brief` and `context` (see section 5, prompt injection).
- **Always-on nudge and hook-guard**: Graphify adds a PreToolUse hook that nudges the agent to
  query the graph before raw reads. improve-skill could use the same idea to enforce "query
  `context` before editing a task's files": the supervisor checks that the task's
  `graph_refs` or a context call happened. Advisory, never blocking.

### 8.4 Skip

Tree-sitter multi-language extraction, LLM semantic extraction and its tiebreak, MinHash/numpy,
Leiden, HTML/SVG visualization (the board already covers this), Neo4j/FalkorDB push, the MCP
server (needs the `mcp` package), and watchdog watch mode. All conflict with the standard-library
rule or duplicate the board.

### 8.5 Caveats about Graphify itself

- The README says queries are logged by default; the code is opt-in only.
- The published benchmarks (LOCOMO, "71.5x fewer tokens") have no harness in the repo, and the
  71.5x figure compares the whole corpus against a single answer.
- A silent fallback from Leiden to Louvain changes every community.
- A lot of its complexity comes from storing a directed graph as undirected JSON and from LLM ID
  drift. improve-skill's SQLite `src/rel/dst` schema avoids both, so copy algorithms, not storage.
