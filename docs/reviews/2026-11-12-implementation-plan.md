# Implementation plan: full-codebase review fixes and Graphify second pass

Date: 2026-11-12. Input: `2026-11-12-full-codebase-review.md` (sections 2-7). Constraint:
Python 3.9+, standard library only. Every item names the review section it closes, its owner,
the files it changes, and the test that proves it. Status is recorded at the end.

## Work split

The work runs as four parallel streams with **disjoint file ownership**, so no two streams
edit the same file:

| Stream | Owns |
| --- | --- |
| **S** supervisor | `scripts/improve_runtime.py`, `improve_resilience.py`, `improve_control.py`, `improve_learning.py`, `scripts/*.sh`; `tests/test_runtime.py`, `test_learning.py`, `test_control.py` (+ new `tests/test_supervisor_recovery.py`); `docs/running.md`, `docs/workflow.md`, `README.md` |
| **B** board | `scripts/improve_board.py`, `assets/board/*`; `tests/test_board.py`, `test_graph_server.py`, `tests/browser_*.py`; `docs/board.md`, `references/board.md`, `references/board-control.md` |
| **C** code layer | `scripts/improve_code.py`, `scripts/improve_code_cli.py` (split out of the graph CLI for this), `scripts/improve_analysis.py`; `tests/test_code_graph.py` (+ new `tests/test_code_resolution.py`, `tests/test_code_features.py`) |
| **G** graph, helpers, instructions, test infrastructure | `scripts/improve_graph.py`, `improve_graph_store.py`, `improve_graph_journal.py`, `improve_graph_cli.py`, `improve_dream.py`, `improve_toon.py`, `improve_ledger.py`; `SKILL.md`, `max/**`, `references/*` (except board), `docs/development.md`; `tests/support.py`, `tests/graph_support.py`, every other `tests/test_graph*.py`, `test_ledger*.py`, `test_toon.py`; `.github/workflows/tests.yml` |

Cross-stream contracts (fixed before the streams start):

- `improve_graph.GraphAmbiguous` / `GraphNotFound` are the exit-4/5 errors (moved out of the CLI).
- `improve_code_cli.ACTIONS`, `add_commands(command, shared)`, `run(args, repo)` are the only
  hooks the graph CLI calls for code commands.
- `improve_graph.sync(..., code_index=...)` stays the code-refresh entry point; stream G changes
  *when* it runs (every sync), stream C changes *what* `improve_code.index` does.
- `improve_graph.context()` result keys `code`, `groups`, `insights[].code_changed`,
  `insights[].contradicted_by`, `blockers[].resolved`, links' `tier` are the board's inputs.
- `improve_ledger.py run` gains fields; the supervisor prompt (stream S) refers to helper
  commands by the names listed in G6.

## Stream G: graph, helpers, instructions, test infrastructure

| # | Change | Closes | Test |
| --- | --- | --- | --- |
| G1 | **Pending queue without loss.** `drain_pending` atomically renames `graph-pending.jsonl` to `graph-pending.draining` (appending any rows left there by an interrupted drain), applies it, and the session deletes the draining file only after commit; new appends go to a fresh file. Applied ids stay in `meta.pending_applied`, so a crash after commit does not re-apply. | 3.1 | a note queued while a drain session is open survives and is applied by the next session; crash between commit and delete applies nothing twice |
| G2 | **Reads journal without loss.** Same rename-then-drain for `graph-reads.jsonl`; rows carry a unique id; the ledger hint reads both files; `reads_applied_at` timestamp comparison removed. | 3.1 | concurrent appends during a drain all count |
| G3 | **Recovery commits on its own.** On damage (or a missing db) the session first runs recover + journal replay in a dedicated write transaction and commits it, then begins the caller's transaction. A failure in the caller's work can no longer roll back the replay; `journal_seq` therefore never regresses. Also `meta.recovery_pending` is set by `recover()` and cleared only when a replay commits, so a crash during replay retries next time. | 3.2 | failing first write after damage keeps the replayed notes; sequence numbers stay unique |
| G4 | **Path escapes closed.** `safe_path` converts `\` to `/`, normalizes, then rejects absolute results, `..`, and any `../` prefix; NUL, newline, and control characters rejected. `file_fingerprint` additionally refuses any resolved path outside the repository. | 3.3 | `src\..\..\x`, `a/../../x`, `/etc/passwd`, symlink leaving the repo |
| G5 | **Linear budgets.** `budgeted` renders once, measures each row's rendered cost once, trims lowest-ranked rows from the longest lists by cumulative cost, verifies with one final render (and at most a few corrective passes). | 3.4 | 1000-row search with `--budget 100` under 2 s; output unchanged for small cases |
| G6 | **Ledger helper completes the run view.** `run` always includes `preflight_complete`, `gates`, `gate_seconds`, `authorization`, `continuation`; `--full` prints the whole `run.json`. New `run-set KEY=VALUE` / `--json-field` (atomic, validated keys: `phase`, `cycle`, `active_item`, `next_action`, `preflight_complete`, `dry_sweep`, `benched_areas`, `lanes`, `continuation`, `journeys`, `artifact_paths`, `gates`, `gate_seconds`, `authorization`, `last_report_hour`; refuses `started_at`, `deadline`, `outcome`) and `discovery-add --json` (append, validated). `queue` applies `run.json.focus.board_areas` prefer/skip and puts `severity >= 5` security items first, and marks skipped areas instead of silently dropping them (`--all-areas` shows them). | 5.1, 5.2 | run view fields; run-set rejects deadline; queue honours prefer/skip |
| G7 | **Ledger field safety.** `--field`/`--json-field` refuse `id` and `created_at`; `--file` filter uses `improve_graph.task_files` (non-list `paths` no longer crashes). | 3.5, 3.6 | `set t2 --field id=t1` exits 2; `paths: 5` queue works |
| G8 | **Surrogates.** `clean()` replaces lone surrogates; the post-commit block catches `ValueError` too; TOON `normalize` replaces surrogates instead of failing a whole answer. | 3.5 | surrogate note commits once, exit 0, searchable |
| G9 | **Restore through merge chains.** `restore_node` resolves `superseded_by` before subtracting merged counts. | 3.5 | A->B->C chain restores to the right totals |
| G10 | **Content-word swap guard.** `duplicate()` refuses a merge when the two notes, aligned word by word (equal length, or one insertion), differ at a position where both words are content words (not stop words). | 3.5 | read/write lock, clock/network stay apart; paraphrases with a dropped article still merge |
| G11 | **Code layer refreshed every sync** (time-bounded, unchanged index costs ~0.05 s); `sync` reports `code`; reads print `code_stale` when the indexed HEAD differs from `git rev-parse HEAD`. | 3.5, Graphify 5 | sync without refine refreshes; stale flag |
| G12 | **Redact agent notes.** `add_insight` passes text through `improve_code.redact` before storing. | 3.6 | a token in a note is stored as `[redacted]` |
| G13 | **Insertion-ordered trimming.** `aliases` and imported `lessons` keep an insertion sequence (`meta.alias_order`, lesson values `{strength, seq}` with backward-compatible reads), and trimming drops the oldest, not the alphabetically first; trimmed lessons are remembered by hash so they are not re-imported every sync. | 3.6 | oldest alias trimmed; trimmed lesson not re-imported |
| G14 | **Determinism.** `board_view` focus selection ordered by BFS order with sorted ties; `co_changed` cut breaks weight ties by key. | 3.6 | repeated calls under different `PYTHONHASHSEED` give identical output |
| G15 | **Batched forgetting.** `forget` removes all doomed nodes in one `remove_nodes` pass per reason. | 3.6 | 1000 forgets over 30k edges under 0.5 s |
| G16 | **Lock fd leak.** `acquire_lock` waiter closes the fd when `flock` raises and the wait was abandoned. | 3.6 | unit test with a mocked flock error |
| G17 | **Recency-weighted verdicts** (Graphify `reflect._decay`). `assess` weights each citing task's outcome by `0.5 ** (age_in_runs / VERDICT_HALF_LIFE_RUNS)` (8 runs); folded counts (`cite_base`) count at the oldest weight; `proven` needs at least 2 distinct done tasks and weighted ratio >= 0.8; `contested` uses the weighted ratio. | Graphify 13 | recent failures outweigh old successes |
| G18 | **Gate names.** `ingest_run` flattens nested gates (`go: {fast, batch}` -> `go.fast`, `go.batch`) and nested `gate_seconds`, so costs reach the brief whatever shape `verification.md` uses. | 5.3 | nested gate cost in the brief |
| G19 | **Truncation marker.** Shortened cells end in `…[+N]` (N characters cut), unambiguous against text ending in `...`; prompt/SKILL wording updated. | 3.6, 5.3 | marker and count |
| G20 | **Instructions.** SKILL.md: name the helpers that take `--format json` (graph, ledger, control, learning; clock and board use `--json`; `context` uses `--json`); exit-code guidance for every helper (graph 4/5 are answers; ledger/control 2 means fix input, 3 retry); state-read through `ledger run`, writes through `run-set`/`discovery-add`; one TOON grammar paragraph (the prompt points at it); placeholder consistency; baseline-longer-than-cycle guidance (record a scoped baseline, note `IMPROVE_CYCLE_TIMEOUT`); daemon handling of requests needing clarification (decline with the question; never leave pending in a daemon cycle). `references/ledger.md`: jq rewrite recipe takes the lock (`flock`) or is removed; queue ordering semantics. `references/intake.md`, `references/verification.md`: `blast` naming, focus ranking, gate shapes. `max/SKILL.md` + `max/references/bets.md`: branch names `improve-max/<date>-bet-<id>` / `-spike-<id>`; corpus in a tracked path `tests/characterization/<journey>/` committed with the replay test; spike timebox below the cycle limit (3h with the 4h default); late-run `/improve-max <remaining>` note. | 5.1-5.3 | doc-consistency test (G22) |
| G21 | **Test infrastructure.** `tests/support.py`: strip every `IMPROVE_*` variable at import (except the test knobs), `atexit` cleanup of the shared git home, `budget()` helper documented and applied; fix sqlite connection leaks in `test_graph_store.py` (290, 424, 465); scale every wall-clock ceiling and HTTP timeout in stream-G test files with `budget()`; tests run with `-W error::ResourceWarning` clean for stream-G modules. | 7.2 | warnings gone; `IMPROVE_FORMAT=json` exported does not break tests |
| G22 | **Instruction-consistency test** (`tests/test_instructions.py`): every `python3 .../scripts/X.py ... SUBCOMMAND --flag` in SKILL.md, max/SKILL.md, references, and the cycle prompt parses with that helper's parser; every env var named in docs exists in code; SKILL version equals runtime `VERSION`; bet branch names are creatable next to the run branch. | 5.x | the test |
| G23 | **CI.** Matrix `ubuntu-latest` + `macos-latest` x Python `3.9` + `3.13` for the fast suite; slow suites on both Pythons nightly; `timeout-minutes` on every job; a lint job: `python -m pyflakes`-free stdlib check (`compileall` + a small AST lint for unused imports/undefined names via `tests/lint.py`) and `node --check assets/board/*.js`; Playwright pinned; screenshots uploaded on failure; `browser_graph.py` not run twice. | 7.2 | workflow is valid YAML (parsed in a test) |
| G24 | **Shared-fixture order dependence.** Mutating tests in `test_graph_queries.py`, `test_graph_store.py`, `test_graph_lifecycle.py` work on deep copies (or per-test fixtures). | 7.2 | suites pass in reverse order (`unittest` with a reversed loader in a test) |

## Stream S: supervisor, recovery, control, learning

| # | Change | Closes | Test |
| --- | --- | --- | --- |
| S1 | `iterate()` checks `stop_requested()` and the deadline before `reconcile()`; `reconcile()` failures that are state errors repair or clear the offending field and journal it once (rate-limited) instead of crashing every iteration; `repair_run` receives skill + args and clears unauthorized terminal outcomes; `run.json` is repaired at the top of each iteration; the prompt lists outcomes per skill (`target reached` only for improve-max with `--stop-at-target`). | 2.1 | resilient-mode daemon with an agent writing "target reached" under plain improve reaches finalization; corrupt run.json repaired; stop during a persistent error exits promptly with bounded journal size |
| S2 | Dirty tree on relaunch of an existing run (supervisor.json present, no outcome): settle the stale child, then quarantine (`between=True`), then continue; dirty refusal kept for first launch and `--new-run`. `--dry-run` reports dirty instead of failing. | 2.2, 5.3 | relaunch with a dirty tree quarantines and continues; `--supervise` survives |
| S3 | ENOSPC: `crashed_iteration` and its log/journal/save writes suppress `OSError`; ENOSPC anywhere in an iteration parks (low-space reason) instead of halting. | 2.3 | simulated ENOSPC parks and resumes |
| S4 | Final-report retry uses `final_retry_at`, which `prepare()` accepts past the deadline for report-only runs; relaunch, `--finalize`, `--supervise` recover. | 2.4 | kill during final retry wait, relaunch finalizes |
| S5 | Watchdog strips `--resume`/`--finalize` after the first launch; checks the stop flag after each backoff sleep; `--stop` while the daemon is down writes `stop.request` when the run has no outcome. | 2.4, 2.5 | resume+supervise relaunch; stop during backoff |
| S6 | Repeated worker halts **park** (consistent with the docs and every other breaker); `test_repeated_worker_halts_are_eventually_honored` updated. Docs list every way a run ends (finalization modified work, quarantine-save failure, strict mode, `IMPROVE_PARK=0`); `IMPROVE_MAX_RECOVERIES` row corrected. | 2.4, 5.2 | test |
| S7 | Wall-clock deadline check in the child poll loop (`time.time() >= deadline + finish_grace` stops the child) so sleep/suspend cannot extend a cycle past the deadline. | 2.4 | simulated clock jump |
| S8 | Artifact quarantine records the file list and removes the artifacts (`git clean` of the listed paths) without committing them; size-capped. | 2.4 | artifacts not in any quarantine ref |
| S9 | `block_count` via `coerce_count`; `--new-run` reads `child_pid` from the old control and settles that child before archiving; corpus kept on `--new-run` (keep set). | 2.4, 5.2 | malformed block_count; new-run with live old child |
| S10 | `memory_brief` builds learning, graph, recovery briefs in separate `try`s; `guidance()` tolerates malformed lessons; `learning.json` writes take a lock (`learning.lock`) for both repo and global stores. | 2.5 | bad lesson keeps the graph brief; concurrent lesson writes keep both |
| S11 | Shell wrappers: `CDPATH= cd -- ... >/dev/null` and symlink resolution. | 2.5 | wrapper via symlink and with CDPATH |
| S12 | `halted:` left in run.json across a crash goes through `soften_worker_halt`; final-report retry matches the limit pattern regardless of rc; `LIMIT_PATTERN` matched only against the agent's own final message / last 4 KiB stderr region rather than echoed tool output. | 2.5 | three unit tests |
| S13 | `repair_ledgers` takes `ledger_lock` and fsyncs, keeps a `.bak` of lines it rewrote; dead `write_rows` removed. | 2.5 | lock held during repair |
| S14 | Withdraw refuses a request that already has a receipt (`improve_control.prepare_request`). | 4.3 | withdraw after ack refused |
| S15 | Cycle prompt: read state through `ledger run`/`queue`/`discovery`; no raw `supervisor.json` read; "persist cycle + 1 only after real work" aligned with continuation.md; TOON grammar points to SKILL.md's paragraph (one short line in the prompt); per-skill outcome list (S1); late improve-max note; `VERSION` from a single constant matching SKILL.md (1.17.0). | 5.2, 5.3 | prompt assertions |
| S16 | Tests: a resilient-mode subclass running engine routing, pause, intake, finalize, and limit tests in the default configuration; account-limit budget exhaustion; `--supervise` relaunch cap; `IMPROVE_FINAL_TIMEOUT` kill; custom `artifact_paths`; quarantine-save failure; scale every wall-clock ceiling with `budget()`; fix the tautological `poll() or wait()` assertion and the orphan timing test. | 7.1, 7.2 | the tests |

## Stream B: board

| # | Change | Closes | Test |
| --- | --- | --- | --- |
| B1 | Drafts: `retry` and `withdraw` types accepted; empty means delete for every type; invalid rows dropped one at a time (the store stays writable). | 4.1 | node-free unit test via a Python-side JS harness is not available, so a browser test plus a JS self-check run by `node --check`-style harness in `tests/js/drafts_check.mjs` when node exists (skipped otherwise) |
| B2 | Show the new graph signals: `node_detail(..., repo=repo)`; task dialog shows code-changed, contradicted-by, resolved badges, gates, module group, code dependents; node detail shows link tier; `rel-contradicts` style; Knowledge tab lists dream gaps and suggested questions (new `/api/graph/questions`). | 4.2 | API tests for fields; browser check for badges |
| B3 | Pause polling: liveness (`heartbeat_at`, ages) served by `/api/health` and excluded from the snapshot ETag. | 4.2 | unchanged paused snapshot returns 304 |
| B4 | Quarantine restore commands shell-quoted, and only refs matching the supervisor's naming are listed. | 4.3 | hostile ref name not shown / quoted |
| B5 | Ledger cache sized from the number of run directories, LRU eviction. | 4.3 | second poll re-parses nothing with 40 runs |
| B6 | Catch-all JSON 500 for graph GETs and POSTs; bounded `offset`/`limit`. | 4.3 | huge offset -> 400 JSON |
| B7 | `Sec-Fetch-Site` cross-site/same-site refused on every `/api/*` GET; `/api/report` takes `operator_lock` with a bounded wait. | 4.3 | cross-site GET 403 |
| B8 | Task dialog passes `exclude=` to `context`; `focus.js` excludes withdrawn requests; unused `counts`/`runs`/`dropped` either rendered or no longer sent (gaps rendered per B2). | 4.3 | API test |
| B9 | Scale every timeout/ceiling in `test_board.py`/`test_graph_server.py` with `budget()`; shared `LargeBoardGraph` fixture not mutated across tests. | 7.2 | — |

## Stream C: code layer and Graphify features

| # | Change | Closes | Test |
| --- | --- | --- | --- |
| C1 | **Scoped binding**: every import alias binds its name even when the target is outside the repo; builtins (`dir(builtins)`) never resolve unless rebound; per-function scopes record parameters, assignment/for/with/walrus/except targets, nested defs and classes, comprehension variables, `global`/`nonlocal`; a locally bound name never falls through to repository-wide inference. | 3.5 | open/loads/callback/inner probes produce no edge |
| C2 | **Star imports** resolve through the source module's top-level names (honouring `__all__`), observed; test callers prefer same file, other tests, then path proximity among candidates. | 6 fidelity | star helper resolves to tests/helpers.py |
| C3 | **Inheritance source** is the class symbol. | 3.5 | `inherits` src is `pkg/svc.py::Service` |
| C4 | **Import roots from package boundaries** (every ancestor directory without `__init__.py` is a candidate root; ambiguous names refused) replacing the fixed `SOURCE_ROOTS` (kept as extra hints). | 6 fidelity | `backend/app/api.py: from app.models import User` resolves |
| C5 | **Re-exports**: names imported into an `__init__.py` (and JS `export … from`) are followed up to 8 hops; ambiguity refused. | 6 fidelity | `from pkg import make` -> `pkg/svc.py::make` |
| C6 | **Keep the last good parse**: on a syntax error the previous symbols/imports/calls stay, the file row records `error` and `stale_parse=1`; `impact`/`symbols` report it. **Shrink guard**: if `git ls-files` returns under half the indexed files with HEAD unchanged, the index is not pruned and the report says why. | Graphify 6, 3.5 | syntax error keeps symbols; shrink refused |
| C7 | **Instance, inherited, and super() calls**: local class bindings from `x = Cls(...)`, `with Cls() as x`, annotations (`x: Cls`, parameters), `self.attr = Cls()` in methods (class-level attribute types); single-inheritance MRO walk across files for `self.m()`, `cls.m()`, `super().m()`, `obj.m()`; rebound names get no edge; edges inferred 0.85 unless the class is local/imported (observed). | Graphify 3 | probes from the review all produce edges |
| C8 | **Symbol references**: `uses` edges for `from m import name` (file -> symbol, observed), decorators, annotations, `isinstance`/`issubclass`/`except` class references, and callables passed as arguments (`indirect`, inferred 0.6); `impact` walks them. | Graphify 4 | impact of a decorator finds its users |
| C9 | **Overrides and class members in impact**: a class seed includes its methods; callers of a base method reach overrides. | Graphify 7 | impact of Base.ping includes subclass callers |
| C10 | **All call sites**: edges keep `sites` (count) and up to 5 `lines`. | Graphify 14 | three calls -> sites 3 |
| C11 | **Change-set impact**: `impact --changed [REV]` (default: working tree vs HEAD, including untracked source files) maps `git diff -U0` hunks onto symbol line ranges (whole file when outside any symbol), unions impact, and reports changed symbols, dependents, tests, and groups touched. | Graphify 1 | edit one function -> its callers listed |
| C12 | **Test selection**: `tests-for TARGET|--changed` returns the test files that reach the target through calls/imports/uses (including `conftest.py` fixtures used by name), ranked by distance, with a ready-to-run hint per ecosystem (`pytest path::name` where a test function is known). | Graphify 2 | tests reaching a helper listed, unrelated tests not |
| C13 | **Code search**: `code search WORDS` over symbol names (identifier splitting on `_`, camelCase, digits), qualnames, docstrings, and code notes, IDF-weighted, test/doc paths demoted unless the query mentions tests, near-miss suggestions on no match; `code find pkg.mod.name` resolves dotted module paths. | Graphify 8, 6 fidelity | `retry upload manager` finds `UploadManager.retry_upload` |
| C14 | **Coverage report**: per-language counts of tracked files indexed vs not indexable, parse failures; printed with `impact`, `code stats`, and in `report`; `impact` on an unindexed language says so instead of "not found". | Graphify 9 | Go file -> unsupported language message |
| C15 | **JS/TS**: tsconfig/jsconfig `paths` + `baseUrl` (JSONC comments and trailing commas stripped, `extends` followed, bounded), package.json `workspaces` package names, named-import call edges (`import {a as b}` then `b(`), comments and strings stripped before scanning; note-comment extraction fixed for lines with URLs. | Graphify 10, 6 fidelity | alias import resolves; call edge; URL+NOTE line |
| C16 | **More languages (imports only)**: Go (`go.mod` module path -> package directories), Java/Kotlin (`package` + `import a.b.C` -> path), Ruby `require_relative`, C/C++ quoted `#include` (relative then include roots). | Graphify 11 | each language fixture produces import edges |
| C17 | **Generated and vendored code**: markers (`DO NOT EDIT`, `@generated`, `Code generated`, protobuf headers, Alembic/Django migrations) and directories (`node_modules`, `dist`, `build`, `vendor`, `site-packages`, `.venv`, minified `*.min.js`): generated files kept as import targets without their own calls/symbols; vendored/minified skipped; reported in coverage. | Graphify 12 | generated module not a call source; vendored skipped |
| C18 | **Test-path detection** widened: path segments `test`, `tests`, `spec`, `specs`, `__tests__`; names `test_*`, `*_test.*`, `*Test.java`, `*Tests.cs`, `*_spec.rb`, `*.spec.*`, `*.test.*`, `conftest.py`. | 6 fidelity | table test |
| C19 | **Bounded cycles**: enumeration stops after `limit` cycles found and after a work budget; report says "at least N". | 3.6 | dense 30-module graph returns in < 1 s |
| C20 | **Communities include code-only files** (`file_weights` adds code edges between files even without knowledge nodes; groups store paths for those) and **split oversized groups** (> 25% of files with >= 10 members, or >= 50 members with cohesion < 0.05) by re-running propagation on the subgraph. | 3.5, Graphify 15 | code-only files grouped; giant group split |
| C21 | `improve_code.CODE_VERSION` bumped (re-index once); `stats` gains coverage and tiers per relation; docs for every new command in `references/knowledge-graph.md` provided as a ready-to-paste section to stream G (C does not edit that file). | — | — |

## Order and integration

1. Streams S, B, C run in parallel; stream G proceeds at the same time.
2. Each stream runs only its own test modules while working (the full suite is run once at the
   end, by stream G, on Python 3.13 and 3.9, plus the slow suites).
3. Stream G integrates: CLI documentation for C's commands, prompt/SKILL consistency with S,
   final full-suite runs, warnings sweep, and the status section below.

## Status (2026-11-13)

Every item in streams G, S, B, and C is implemented and tested. Per-stream detail:

| Stream | Result | New tests |
| --- | --- | --- |
| G | G1-G24 done. G2 needed no per-row ids: renaming the reads file under a lock before draining makes every row count once. | `test_graph_regressions.py`, `test_instructions.py`, `lint.py`, `reverse_order.py`; updates across the graph, ledger, and TOON suites |
| S | S1-S16 done. Stop is checked first and `reconcile` no longer raises for state errors, rather than checking the deadline before `reconcile` literally, which would have skipped quarantine at the deadline. | `test_supervisor_recovery.py`, `DefaultModeTests` in `test_runtime.py`, updates to `test_control.py` and `test_learning.py` |
| B | B1-B9 done. The browser checks were extended but not run here (Playwright is not installed); CI's nightly browser job runs them. | `test_board_hardening.py`, `test_board_graph_signals.py`, `test_board_js.py` (+ `tests/js/drafts_check.mjs`) |
| C | C1-C21 done. Non-Python languages live in `scripts/improve_code_lang.py`. Measured: this repository 0.8 s cold / 0.1 s warm; a synthetic 2,000-file repository 6-8 s cold (10.7 s on 3.9) / 0.15 s warm. | `test_code_resolution.py`, `test_code_features.py`, `test_code_languages.py` |

Found and fixed during integration and the documentation review (not in the review):

- A note containing an undecodable argv byte was committed but its journal line was silently
  empty (`json.dumps(..., ensure_ascii=False)` cannot encode a lone surrogate); journal lines
  are ASCII-escaped now.
- One replacement in the stream-G edit script wrote a literal placeholder into
  `improve_graph.py`; caught by the next compile and restored (no test ever ran on it).
- The swap guard first treated connectives ("because"/"since") as content words; they are
  neutral now, and the existing provenance tests pass unchanged.
- `run-set` accepted only a fixed list of keys, while the references tell the agent to write
  `last_activity_at`, `discovery`, `focus.board_areas` (when applying a board focus request),
  and improve-max's `mode`/`target_stack`; it now refuses only the supervisor's `started_at`,
  `deadline`, and `outcome`. Its `KEY=VALUE` pairs are also accepted after `--json-field`
  (argparse alone rejected them there).
- G11's stale flag asks the code layer for the commit it indexed (`improve_code.freshness`)
  instead of keeping a second copy in graph meta.
- Documentation: README capability rows (code layer, safety), boundaries (no new tooling),
  docs table, development commands; `docs/running.md` state table (every file under
  `.improve/`), graph and output sections; `docs/development.md` test tables, CI, test
  isolation; SKILL.md tooling guardrail, `run-set` scope, helper exit codes, TOON paragraph;
  references for the ledger, knowledge graph (code layer section, `contradicts`),
  continuation, intake, verification, board control; improve-max branch names, tracked
  corpus, spike timebox.

Verification: see the final section, added after the last full runs.
