# Implementation plan: knowledge graph integrity, TOON output, and Graphify-derived features

Date: 2026-11-10. Input: `2026-11-10-knowledge-graph-toon-review.md` (findings numbered 1.1-7.7
and Graphify section 8). Constraint: Python 3.9+, standard library only, no new dependencies.
Storage formats stay backwards compatible: an existing `graph.db` opens, migrates in place, and
keeps every node.

Each item names the finding it closes, the files it changes, and the test that proves it.
Status is recorded at the end.

## Phase A: knowledge integrity (review section 1)

| # | Change | Closes | Files | Test |
| --- | --- | --- | --- | --- |
| A1 | `tokens()` keeps digits and Unicode word characters (`\w`, so CJK/Cyrillic notes tokenize). New `duplicate(a, b)` used by `add_insight`, `recall_text`, and `refine`: Jaccard >= 0.75 **and** the same negation polarity (not/no/never/don't/avoid/without/cannot/...), the same set of numbers, and the same set of path-like tokens. The FTS indexer version is stored in `meta.fts_version`; a mismatch rebuilds the index once. | 1.1, 1.3 | `improve_graph.py`, `improve_graph_store.py` | always/never stay apart; eight numbered notes stay eight; api/db.py vs web/db.py stay apart; 5 s vs 50 s stay apart; a Cyrillic note is searchable and deduplicates with itself; an old index is rebuilt |
| A2 | Provenance-first writes. Source rank user > learning > agent. When a higher-ranked source writes a near-duplicate of a lower-ranked note, the new text becomes its own node and the old note merges **into** it (old id aliases to the new one, so citations still count). A lower-ranked near-duplicate only reinforces. `refine` picks the merge winner by source rank, then pinned, then citation credit, then reinforcement, so a user note is never the merge loser to an agent note. | 1.2 | `improve_graph.py` | the board's "Do not mock..." case shows the user's text; agent note id resolves to it; refine never drops user text |
| A3 | `merge_into` carries `cite_base` and records it in `merged_counts` so restore subtracts it exactly; the refine sort key includes `cited_done`. | 1.4 | `improve_graph.py` | `cite_base` survives a merge and a consolidation; restoring the merged note gives the credit back |
| A4 | Dream evidence keeps `retained_tasks` (task id to outcome, bounded at 500) beside the `retained` counts. `tally` moves a pruned task into it and takes a restored, live-again task back out, so nothing counts twice. `MAX_EVIDENCE` rises to the task bound so outcomes beyond 200 are not lost at prune time. | 1.5 | `improve_dream.py` | support stays 6 across prune and restore |

## Phase B: reliability and data loss (review section 2)

| # | Change | Closes | Files | Test |
| --- | --- | --- | --- | --- |
| B1 | **Write journal.** Every curated write (agent `note`/`link`/`rate`/`unlink`/`restore`/`consolidate`, and board `note`/`rate`/`restore`/`reject_link`) is appended after commit to `.improve/graph-writes.jsonl` with a sequence number held in `meta.journal_seq`. After a recovery from checkpoint (or rebuild) the session replays entries newer than the restored `journal_seq`. A checkpoint truncates the journal. | 2.1 | `improve_graph.py`, `improve_board.py` | write a note after the checkpoint, corrupt the db, next session restores and the note is back |
| B2 | **Busy writes queue instead of failing** (Graphify's pending-changes drain). When the writer lock is busy, `note`/`link`/`rate`/`unlink` append to `.improve/graph-pending.jsonl` and exit 0 with `queued: true`; every writer session drains the queue first. | 3.4 side effect; Graphify 8.3 | `improve_graph.py` | held lock: `note` exits 0 queued; the next `sync` applies it exactly once |
| B3 | `is_damage` no longer treats "no such table" as corruption (missing graph tables are still detected in `Store.open`). | 2.2 | `improve_graph_store.py` | `OperationalError("no such table: x")` is not damage |
| B4 | Co-change pairs of pruned done tasks fold into `meta.co_base` (bounded), recorded per task so restore takes them back; refine weights `co_changed` by live plus folded pairs. | 2.3 | `improve_graph.py` | a co-change link and its second-generation `applies_to` survive pruning both tasks |
| B5 | **Rename tracking.** Refine reads `git log -M --diff-filter=R --name-status` since the last seen `HEAD` (bounded to 300 commits on first use), follows rename chains, and merges `file:old` into `file:new` (links, folded counts, `about` knowledge), aliasing the old id. Ingest, `context`, and partial loads resolve file aliases, so ledgers that still name the old path land on the new node. | 2.4 | `improve_graph.py` | after `git mv` the new path carries the old statistics and knowledge; the old ledger path resolves to it; restore reverses it |
| B6 | `improve_ledger.py set --graph-ref` appends to the existing list; `--clear-graph-refs` replaces. | 2.5 | `improve_ledger.py` | two `set --graph-ref` calls keep both ids |
| B7 | Ledger writes take `.improve/ledger.lock` (also taken by the supervisor's `block_active_item`) and rewrite the file line by line: unparseable lines and rows without an id stay where they were. | 2.6 | `improve_ledger.py`, `improve_resilience.py` | damaged line survives `set`; concurrent `set`s in threads lose nothing |
| B8 | Partial-session clash raises `GraphConflict` (a `ValueError`), so the CLI exits 2 with a message, never a traceback. | 2.7 | `improve_graph_store.py` | direct test |
| B9 | Rejected inferred links are re-keyed through aliases in `repair`, so a merge cannot resurrect a rejected link; the map is bounded (2000 newest). | 2.8 | `improve_dream.py` | reject, merge the endpoint, dream: link not re-inferred |

## Phase C: TOON output (review section 3)

| # | Change | Closes | Files | Test |
| --- | --- | --- | --- | --- |
| C1 | `tabular()` tries three encodings of every object array: as is, made uniform, and made uniform after flattening one level of nested objects into `parent.child` columns. It keeps the shortest. TOON output can therefore never be longer than the encoding it replaced. | 3.1 | `improve_graph.py`, `improve_toon.py` (`flatten`, `compact_rows`) | mixed search output is no longer than compact JSON |
| C2 | README and docs quote measured savings against **compact** JSON and pretty JSON separately. | 3.2 | `README.md`, `docs/running.md` | measurement recorded below |
| C3 | Graph CLI `--width N` (default 160) and `--full` bound long text cells. `truncate` gains a `keep` set; identifier columns (`id`, `file`, `paths`, `src`, `dst`, `commit`, ...) are never cut in the graph or ledger CLI. | 3.3 | `improve_toon.py`, `improve_graph.py`, `improve_ledger.py` | long summary cut with marker; a 140-char path survives |
| C4 | `--format`, `--full`, `--width`, `--delimiter` are accepted before **or** after the subcommand in the graph, ledger, control, and learning helpers (shared parent parser with `SUPPRESS` defaults). | 3.4 | all four CLIs | `stats --format json` works |
| C5 | `IMPROVE_FORMAT` is normalized (case, whitespace); an unknown value warns on stderr and falls back to TOON. | 3.5 | `improve_toon.py` (`default_format`) and the CLIs | `JSON` gives JSON; `yaml` warns |
| C6 | The cycle-prompt hint also covers nested headers (`f{a,b}`), keyed tables (`name[N:]{...}:`), `null`, and quoted cells; it names the commands whose default output is text. | 3.6, 3.7 | `improve_runtime.py`, `SKILL.md` | prompt assertion |

## Phase D: query quality (review section 4)

| # | Change | Closes | Files | Test |
| --- | --- | --- | --- | --- |
| D1 | `brief` reserves room for the short sections (gates, proposals, hotspots, gaps) and fills the rest with knowledge, each note cut to 220 characters with its id. When notes are left out it says so ("showing 5 of 8; `search` or `node ID` for the rest"). The brief is fenced as recorded data, not instructions. | 4.1, section 5 (prompt injection) | `improve_graph.py` | gates and proposals present with eight long notes; omission line present |
| D2 | **Retrieval keeps knowledge alive.** `context`, `search`, and `node` append the ids they returned to `.improve/graph-reads.jsonl` (append only, no lock); `sync` drains the file and refreshes `last_tick` and a `retrieved` count, so read-and-followed advice does not go dormant. Retrieval is not validation: a retrieved note still fades by score. | 4.2 | `improve_graph.py` | a retrieved note is not archived as dormant after 8 dreams; an unread one is |
| D3 | **Code fingerprints on knowledge** (Graphify `code_fingerprint`). `note --about file:X` stores the sha256 (12 hex) of each file; a done task citing the note re-stamps it. `context`, `node`, and `brief` flag `code_changed` ("re-verify: the file changed since this was recorded"). | Graphify 8.1 | `improve_graph.py` | edit the file: flag appears; done citation clears it |
| D4 | `path` ignores the hub relations `in_run`/`in_area` and refuses to pass *through* nodes at or above the 99th percentile of degree (minimum 50), unless `--all`; `--via REL` restricts the relations; the answer reports its weakest hop (`confidence` and `tier` per step) like Graphify's `_path_weakest`. | 4.3 | `improve_graph.py` | file-to-file path goes through a task, not `run:`; weakest hop reported |
| D5 | `context` weighs knowledge reached through a past task by that task's outcome (done 1.0, open/blocked 0.6, rejected 0.35). | 4.4 | `improve_graph.py` | a note cited only by rejected tasks ranks below one cited by successes |
| D6 | Blockers seen on a file are marked `resolved` (and weighted down) when a task on that file finished successfully after the blocker's latest occurrence. Ingest records `finished_at` on task nodes. | 4.5 | `improve_graph.py` | resolved blocker flagged and ranked last |
| D7 | `context --exclude TASK_ID` (also accepted as a bare ledger id). | 4.6 | `improve_graph.py` | own task absent |
| D8 | Consolidation proposals are cliques (every member linked to every other), grown greedily from the strongest edge, instead of connected components. | 4.7 | `improve_dream.py` | chain A-B-C-D does not produce one four-note proposal |
| D9 | Edge tiers (Graphify's EXTRACTED/INFERRED/AMBIGUOUS): every link in `node` and `path` output carries `tier` = `recorded` (agent/user), `observed` (from ledgers/git/code), or `inferred` (dreaming). | Graphify 8.1 | `improve_graph.py` | tiers present |

## Phase E: correctness and hygiene (review section 5)

| # | Change | Test |
| --- | --- | --- |
| E1 | `safe_path` normalizes `a//b` and `a/./b` and rejects only a first component of exactly `.improve` (so `.improvements/` is a valid path). | path cases |
| E2 | `blocker_key` (graph and learning) collapses hex runs only when they contain a digit, so "defaced"/"deadbeef-free" words survive while SHAs still group. | key cases |
| E3 | History `LIKE` queries escape `%`, `_`, and `\`. | a literal `%` query |
| E4 | `set_aside` keeps the newest damaged store as `graph.damaged.db` and rotates earlier ones to `graph.damaged.<stamp>.db` (three kept). | two incidents keep both |
| E5 | `acquire_lock` docstring states what flock does and does not guarantee. | n/a |
| E6 | Refine counts gate and blocker uses in one edge pass (it was O(N x E)). | existing suites; timing |
| E7 | `adjacency()` is cached on graphs loaded by `load_cached` (read-only board graphs), keyed by revision. | board view identical; second call reuses |
| E8 | `reading()` marks a graph written by a newer format; the CLI warns on stderr. | warning test |
| E9 | `import_lessons` goes through the provenance-first path (A2), so a lesson that paraphrases an agent note makes the merged note learning-protected. | lesson near agent note is protected |
| E10 | `clean()` strips control characters (Graphify `sanitize_label`). | control chars removed |
| E11 | `tests/test_ledger.py` closes the file it appends to (ResourceWarning). | warning gone |

## Phase F: features from Graphify (review section 8)

### F1. Code structure layer (`scripts/improve_code.py`, new)

Graphify's AST pass, re-implemented with the standard library `ast` module for Python and with
conservative regexes for JavaScript/TypeScript relative imports. Stored in **separate tables** of
`graph.db` (`code_file`, `code_symbol`, `code_edge`, `code_note`), not as knowledge nodes, so
dreaming, caps, and the board's knowledge view are unaffected and a code index can be rebuilt
at any time without touching curated knowledge.

- Files come from `git ls-files` (bounded: 4000 files, 1 MB each). Change detection is
  Graphify's racily-clean stat fast path: a `(size, mtime_ns)` match is trusted only when the
  file's mtime is at least 2 s older than the time it was indexed; otherwise the content sha256
  decides. Deleted files are pruned. The extractor version is part of every row
  (`meta.code_version`); a newer extractor re-indexes everything once (Graphify's versioned cache
  namespace).
- Symbols: module, class, function, method with line ranges and the first docstring line.
- Edges: `imports` file to file (absolute and relative imports resolved against the indexed
  module paths), `calls` symbol to symbol. Calls named through an import (`from m import f`,
  `import m; m.f()`) are `observed` with confidence 1.0; a bare name that matches exactly one
  definition in the repository is `inferred` with confidence 0.85 (test files lose ties);
  anything ambiguous is skipped, never guessed. Calls inside the same module resolve first.
- Code notes (Graphify rationale nodes): `# WHY:`, `# NOTE:`, `# HACK:`, `# IMPORTANT:`,
  `# RATIONALE:`, `# FIXME:`, `# TODO:` comments, with secrets redacted (key, token, JWT,
  bearer, and URL-credential patterns).
- Commands: `code index [--max-seconds N]`, `code symbols PATH`, `code find NAME` (exit 0 one
  match, 4 ambiguous with the candidates, 5 none), `impact TARGET [--depth 2]`, `cycles`.
- `impact` (Graphify `affected_nodes`): reverse breadth-first walk over `imports` and `calls`
  from a file or symbol, reporting each dependent with the call or import **site** (file:line),
  depth, and, from the knowledge graph, its success rate and the gates that verified it.
- `cycles`: import cycles up to length 5, rotations removed, shortest first.
- `context` adds, for the seed files: dependents ("files that import this") and the code notes.
- `sync` refreshes the index when refinement runs (time-bounded, 20 s by default,
  `IMPROVE_CODE_INDEX_SECONDS`, `0` disables).

### F2. Analysis and reporting (`scripts/improve_analysis.py`, new)

- **Communities over files**: deterministic weighted label propagation on `co_changed` weights
  plus import edges (edges sorted before iteration, ties to the smallest label, a fixed number of
  passes). Each community gets a cohesion score (internal edges / possible pairs), a label from
  its highest-degree file, and an id that stays stable across dreams by matching member overlap
  with the previous result. Hub files (99th percentile of degree) are left out and then assigned
  by a vote of their neighbours. Stored in `meta.communities` at each dream; `communities`
  command; `context` shows the seed file's module group.
- **Surprising connections** (Graphify `_surprise_score`): co-change and inferred links that
  cross top-level directories, link a low-degree file to a hub, or cross communities, each with
  a `why`.
- **Bridges** by Brandes betweenness on the file graph (sampled above 1500 nodes).
- **Suggested questions**: contested knowledge, knowledge whose code changed, failing files with
  nothing recorded, bridges, low-cohesion groups, isolated files. Different kinds are interleaved
  so no single kind fills the list.
- **`report`**: a Markdown graph report (summary with tier percentages, hotspots, god files,
  contested and changed knowledge, gaps, communities, surprises, import cycles, suggested
  questions).
- **`export --as json|graphml|markdown [--out PATH]`**: GraphML through `xml.etree`, atomic
  write when `--out` is given.
- **Dream diff** (Graphify `graph_diff`): each dream records added and removed nodes and
  inferred links (counts plus up to 10 examples each) in its history entry; `diff` prints the
  latest.

### F3. Budgets and exit codes

- `context`, `search`, and `node` take `--budget TOKENS` (3.5 characters per token). Seeds are
  never cut; the lowest-ranked rows of the longest list go first. A cut output starts with
  `[!] TRUNCATED: showing X of Y rows; --budget N shows everything` (Graphify
  `_subgraph_to_text`).
- Exit codes: 0 answer, 2 error, 3 busy (retry), 4 ambiguous name, 5 not found (Graphify's
  refuse-don't-guess resolution), documented in the reference.

### F4. Agent guidance

- The ledger's `set ID --status in_progress` checks the read journal (D2) and prints an advisory
  `hint` when no `context` query covered the task's files in the last hour (Graphify's
  always-on nudge, advisory only).
- `dream` records `meta.rules_version`; when the inference rules change between skill versions,
  the first dream re-derives links without hysteresis and says so in its report.

## Decisions

- Code structure lives in its own tables instead of knowledge nodes, so the 1,500-file cap,
  dreaming, forgetting, and the board's balanced view keep their meaning. This deliberately
  departs from Graphify, which mixes the two.
- No tree-sitter, Leiden, MinHash, LLM extraction, HTML visualization, database push, or MCP
  server (dependencies, or duplicates of the board).
- The "tentative" verdict from Graphify's reflect layer was not added: the existing
  proven/contested rule already needs three distinct successful tasks, and a third label would
  add brief noise.
- Exit code 3 stays "busy"; ambiguity uses 4 and not-found 5, unlike Graphify's 3/4, because
  3 is already documented as retryable.

## Status (2026-11-10)

| Phase | Status | Where | Tests |
| --- | --- | --- | --- |
| A integrity | Done: A1-A4 | `improve_graph.py` (`tokens`, `facts`, `opposed`, `duplicate`, `add_insight`, `merge_into`, refine step 1), `improve_dream.py` (`tally`), `improve_graph_store.py` (`INDEX_VERSION`) | `tests/test_graph_integrity.py` |
| B reliability | Done: B1-B9 | `improve_graph_journal.py` (new), `session()`, `refine` (co-change folds, `track_renames`, `merge_file`), `improve_ledger.py`, `improve_resilience.py` (`ledger_lock`, `rewrite_tasks`), `improve_dream.repair` | `tests/test_graph_reliability.py`, `tests/test_ledger_safety.py` |
| C TOON output | Done: C1-C6 | `improve_toon.py` (`flatten`, `compact_rows`, `truncate(keep=)`, `default_format`, `output_options`), `improve_graph_cli.py` (new), all helper CLIs, cycle prompt, docs | `tests/test_graph_output.py`, `tests/test_runtime.py` prompt assertions |
| D query quality | Done: D1-D9 | `brief`, `context`, `shortest_path`, `node_detail`, `edge_tier`, read journal, fingerprints, `propose` | `tests/test_graph_analysis.py`, `tests/test_graph_output.py` |
| E hygiene | Done: E1-E11 | as listed | integrity, reliability, and existing suites |
| F Graphify features | Done: F1-F4 | `improve_code.py` (new), `improve_analysis.py` (new), CLI commands `code`, `impact`, `cycles`, `communities`, `questions`, `report`, `export`, `diff`; `--budget`; exit codes 4/5; ledger hint; `rules_version` | `tests/test_code_graph.py`, `tests/test_graph_analysis.py` |

Measured TOON size against compact JSON on a synced and dreamed graph (40 tasks, 8 notes,
`--full`, characters): `search` 4,787 vs 6,625 (0.72), `node` 1,914 vs 2,350 (0.81),
`context --toon` 4,774 vs 7,014 (0.68), `hotspots` 659 vs 1,301 (0.51), `stats` 242 vs 251
(0.96). The mixed search sample from the review (10 notes + 5 files) is now 2,358 characters
against 3,186 for compact JSON; before C1 it was 4,210 (32% *larger* than JSON).

Code layer on this repository: 49 files, 1,283 symbols, 2,239 links in 0.46 s cold and
0.05 s when nothing changed; no top-level import cycles (16 cycles exist only through
deliberate function-level imports, which `cycles --all` shows).

Issues found and fixed while implementing (not in the review):

- Moving the tracked-file read in `refine` exposed a loop variable `files` that shadowed it and
  marked every file stale; renamed to `tracked`.
- `context` built its code section from sibling seed files when the requested file had no graph
  node yet, so it described the wrong file; it now starts from the files asked for.
- `rewrite_tasks` dropped rows its update created (the supervisor blocking an active task that
  was never written to the ledger).
- `sqlite3.executescript` commits the open transaction, which would have broken the graph
  session's atomic write when the code tables were created inside it; the schema is created
  statement by statement.
- Two damaged-store archives in the same second overwrote each other; names carry nanoseconds.
- A brief budget smaller than one line returned half a sentence; it now returns whole lines or
  a one-line summary.
- `--width` would have cut whole text answers (`brief`) to 160 characters; text answers are
  exempt.
- Two unclosed files in `tests/test_ledger.py` (ResourceWarning).
- **Pre-existing, found by running the new suites on Python 3.9:** `restore_backup` opened the
  checkpoint as `file:{path}?mode=ro` without URI escaping, and a read-only open of a WAL-mode
  database fails on older SQLite builds when no `-shm` file exists. Either way a good
  checkpoint was rejected and recovery rebuilt the graph from nothing, losing curated
  knowledge (any repository path with a space hit the first case). The checkpoint is now
  copied first and the copy is verified and installed.
- **Pre-existing, Python 3.9 only:** `toon.normalize` used comprehensions, which before Python
  3.12 add a stack frame per level, so a value nested below `MAX_DEPTH` raised a raw
  `RecursionError` instead of `ToonError` (the existing `test_deep_nesting_is_bounded` fails on
  3.9 at `origin/main`, which CI's 3.9 job runs). `normalize` now uses loops and `dumps`
  reports any stack exhaustion as `ToonError`.

Verification: fast suite 470 tests passing on Python 3.13.3 and 3.9.6 (12 slow tests skipped),
slow graph suites (soak, scale, performance) 78 tests passing, all 564 TOON conformance
fixtures passing on both interpreters.
