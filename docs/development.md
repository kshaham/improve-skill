# Development and verification

[← Back to README](../README.md)

Run these commands from the skill checkout. Regression tests use temporary repositories and fake assistant CLIs.

## Code navigation

For structural code navigation, optionally build a local CodeGraph index with
`codegraph init -i` from this checkout. Generated `.codegraph/` data stays untracked.
When the MCP server starts from a parent directory, pass this checkout's absolute path
as `projectPath` to its tools. CodeGraph is a development aid; skill runs do not require it.

## Regression checks

From the skill checkout:

```sh
python3 -B -m unittest discover -s tests -v
IMPROVE_TEST_SLOW=1 python3 -B -m unittest discover -s tests -v   # also soak, scale, performance
for improve_script in scripts/*.sh; do
  bash -n "$improve_script"
done
python3 tests/lint.py
for improve_js in assets/board/*.js; do node --check "$improve_js"; done   # if node is installed
```

The default run is the fast suite: unit, daemon, graph, board, and control tests. The
48-hour and 72-hour soaks, the 6,000-task scale fixtures, and the CLI performance suite
run only with `IMPROVE_TEST_SLOW=1`; set `IMPROVE_TEST_SPEED_FACTOR=2` (or more) on a slow
or shared machine to scale every wall-clock ceiling. Tests isolate git from the host's
global and system configuration (`tests/support.py`), drop the host's `IMPROVE_*` settings
(only `IMPROVE_TEST_*` knobs pass through) and point the cross-project learning store at a
throwaway directory, skip FTS5-only checks when the interpreter's SQLite lacks FTS5, skip the
JavaScript drafts check when `node` is missing, and never bind a fixed port. Every wall-clock
ceiling and subprocess or HTTP timeout goes through `support.budget()`.

`.github/workflows/tests.yml` runs, on every push: a lint job (shell syntax, `tests/lint.py`,
`node --check` on the board scripts), and the fast suite with `-W error::ResourceWarning` on
Ubuntu and macOS under Python 3.9 and 3.13. Nightly (and on demand) it runs the slow suites on
the same four combinations, the reverse-order check, and the browser checks with a pinned
Playwright, keeping screenshots when a browser check fails. Every job has a time limit.

The regression suite uses temporary git repositories and fake Codex and Claude CLIs. It tests intake,
deadlines, early completion, discovery continuity, restart, rate limits, lock ownership,
timeouts, dirty state, recovery, repeated-scan detection, stop/deadline cleanup, report-only
retries, engine/model persistence, authoritative clocks, rate-limit restart accounting,
failed-success claims, stale reports, missing CLI failures, board startup/stop/history,
malformed ledgers, HTTP boundaries, live task data, experiment evidence, bookkeeping-only
churn, intake metadata preservation, authoritative board outcomes, and finalization without
launching a real model or editing a real app.
It does not establish that every model will find useful changes for a multi-hour run.
`tests/test_runtime.py` also runs the long-run scenarios: malformed agent-written fields,
ledgers with bytes that are not UTF-8, usage limits on exit code 0, build artifacts every
cycle, CLI crashes, parking after exhausted breakers, contained supervisor errors, a daemon
killed mid-cycle and relaunched by `--supervise` (stopping the stale child), SIGTERM,
finalization after a limit, cycle budgets in prompts, idle backoff, log rotation, and a
worker that uses the helper CLIs it is told to use. The fake CLI takes `FAKE_SCRIPT`, a JSON
list of one mode per launch.

### Knowledge graph suites

| Suite | What it proves |
| --- | --- |
| `tests/test_graph_queries.py` | Every query (`context`, `search`, `node`, `path`, `hotspots`, `brief`, board views, CLI) against a fixture with known answers, including path spellings, unsafe input, limits, aliases, and ordering |
| `tests/test_graph_lifecycle.py` | Ledger edits update and delete graph links, vanished tasks are removed (unreadable ledgers never cause mass deletion), pruned outcomes are counted exactly once, refinement runs on schedule and is idempotent, damaged stores recover from the refined backup, concurrent processes lose no writes and readers never see a torn file |
| `tests/test_graph_lifecycle.py` soak | A compressed 48-hour run (288 ten-minute cycles over six archived runs): tasks move through every status, get unblocked, change files and areas, cite and drop knowledge, leave the ledger, and files are deleted. After **every** cycle it checks structural invariants and that the graph matches the ledger exactly, runs every query, and at the end checks every file and area count against the ledgers. Set `IMPROVE_GRAPH_SOAK_CYCLES` to run longer |
| `tests/test_graph_lifecycle.py` scale | 6,000 archived tasks, 3,000 files, and 450 notes: size caps, conservation of outcomes across pruning, and latency ceilings for every query |
| `tests/test_graph_server.py` | Board endpoints over a 3,000-task graph: bounds, payload sizes, parameter abuse, authentication, validation of every action (including dream, restore, reject-link, and history paging), concurrent retries applied once, concurrent notes all kept, cache freshness, and reads during live daemon writes |
| `tests/test_graph_store.py` | The SQLite store: partial sessions answer `context`, `search`, and `node` exactly as a full load does (80 random queries, plus knowledge reachable only through related links), writes touch only changed rows, search without FTS5, legacy `graph.json` migration, four kinds of damage plus a missing database recovered from the checkpoint, rollback, readers that never wait, fair writers under contention, compressed and bounded history, and freed pages returned to disk |
| `tests/test_graph_dream.py` | Every inference rule at its thresholds (support, share of evidence, contradiction, hysteresis, retention across pruning, second generation never chained, area majority, co-citation overlap, shared-file wording, blockers), rejection, determinism, confidence-weighted context, dormancy and forgetting on the dream clock within one run, protected knowledge, exact restore of pruned and merged nodes, recall by citation, by re-recording, and by returning work, verdicts, gaps, and safe consolidation |
| `tests/test_graph_dream.py` 72-hour run | A world with a known truth (modules, their knowledge, noise citations, blockers): 432 cycles, a dream every 18. At every dream it checks structure, every inferred link's rule, confidence, and evidence, precision of at least 0.95 for every rule with 10 or more links, and recall of at least 0.55; at the end, unused notes were forgotten within the run, valuable knowledge was never touched, and a forgotten note restores. Set `IMPROVE_GRAPH_DREAM_SOAK_CYCLES` to run longer |
| `tests/test_toon.py` | The TOON encoder and decoder against all 564 official conformance fixtures (byte-exact encoding, decoded values and errors, strict and lenient options), round trips of every fixture and of randomized values with every delimiter, canonical numbers, quoting rules, host-type normalization, surrogate and UTF-8 refusal, the `uniform` and `truncate` view helpers, the stdin CLI, and the size reductions on representative ledger and query shapes |
| `tests/test_ledger.py` | The ledger helper: ranking identical to the SKILL.md order, status/area/file/id filters, bounded and full views, counts, discovery and run views, JSON and delimiter modes, canonical `set` rewrites that merge duplicate rows and validate fields, `add`, damaged lines, and a repository without a ledger |
| `tests/test_graph_integrity.py` | What counts as the same knowledge (negation, opposite words, numbers, file names, non-Latin scripts), the user's or learning store's wording winning a merge, citation credit surviving merge and restore, restored pruned tasks counted once, rejected links following merges, path normalization, hex-word blocker keys, literal history queries, and a search index rebuilt after a tokenizer change |
| `tests/test_graph_reliability.py` | Curated writes (agent and board) replayed from the journal after recovery from a checkpoint or a rebuild, writes queued while the graph is held and applied once, co-change weight kept through pruning and restore, renamed files keeping statistics, links, knowledge, and fingerprints, damaged-store rotation, the newer-format warning, and the dream diff |
| `tests/test_graph_output.py` | TOON never longer than compact JSON on mixed rows, flattened columns, output switches before or after the command in all four helpers, `IMPROVE_FORMAT` normalization, width bounds that never cut ids or paths, token budgets that say what they left out, and a brief that keeps its short sections and flags changed code |
| `tests/test_graph_analysis.py` | Context weighted by task outcome, resolved blockers, `--exclude`, code fingerprints set and cleared, retrieval keeping knowledge alive, paths that avoid hubs and name the weakest step, inferred contradictions, clique proposals, deterministic module groups with stable ids, surprises, bridges, questions, the Markdown report, GraphML/JSON export, and edge tiers |
| `tests/test_code_graph.py` | The code layer: symbols, relative/absolute/script/JS imports, observed and inferred calls that never guess, code notes with secrets redacted, parse errors, the racily-clean stat check, re-resolution after edits, deletions, time budgets, extractor upgrades, impact with call sites, cycles without deferred imports, exit codes 4/5, and its use in `sync` and `context` |
| `tests/test_ledger_safety.py` | Accumulating `--graph-ref`, damaged and id-less lines kept in place, concurrent ledger writers and the supervisor losing nothing under the ledger lock, ids and paths never cut, the advisory context hint, and `add` keeping damaged lines |
| `tests/test_graph_policy.py` | Protected user and learning knowledge survives fade, contradiction, and caps; only corruption is damage (a constraint error rolls back without restoring a checkpoint); a held `graph.lock` queues agent writes and fails whole-graph writes fast with exit 3 while readers continue; lessons import once and return only when reinforced; whole-token gate attribution; supervisor blocks are not knowledge; stable co-change links; fresh graphs are not due until their interval; `history --offset`; `unlink`, `proposals`, `consolidate` |
| `tests/test_graph_performance.py` | Real CLI processes on a steady-state graph (task cap reached, 4,000+ history rows): median wall time and peak memory for reads and writes, a file's `context` and `node` at least 20% faster than a whole-graph read and lighter in memory, daemon sync/refine/dream within a cycle gap, and a board poll that reuses an unchanged graph |

| `tests/test_graph_regressions.py` | The 2026-11-12 review's graph findings: a note queued while a drain is open survives, a failed drain is retried and a crash after commit applies nothing twice, concurrent queue appends lose nothing; recovery replay commits before a failing first writer and keeps journal sequences unique, and a failed replay is retried; no path spelling (backslashes, `..`, absolute, drive letters, control characters, symlinks) leaves the repository; linear token budgets; undecodable argv bytes; ledger `run`/`run-set`/`discovery-add`, protected fields, focus-aware `queue`; restore through merge chains; content-word swaps; secret redaction in notes; age-ordered alias and lesson trimming; nested gate names; deterministic focus views; linear forgetting; recency-weighted verdicts; the lock helper's descriptor |
| `tests/test_code_graph.py`, `tests/test_code_resolution.py`, `tests/test_code_features.py`, `tests/test_code_languages.py` | The code layer: symbols, imports from package roots, scoped binding (parameters, locals, builtins, third-party imports never guessed), star imports and `__all__`, re-exports, instance/inherited/`super()` calls, `uses` and `indirect` references, overrides in impact, every call site, the last good parse on a syntax error and the shrink guard, change-set impact, test selection with pytest fixtures, code search, coverage, JS/TS aliases and workspaces, Go/Java/Kotlin/Ruby/C imports, generated and vendored code, bounded cycles, code-only module groups and split oversized groups |

### Supervisor, board, and instruction suites

| Suite | What it proves |
| --- | --- |
| `tests/test_runtime.py` | The daemon end to end with a fake agent CLI (see above), in strict mode and again in the default resilient mode (`DefaultModeTests`) |
| `tests/test_supervisor_recovery.py` | Long-run recovery: stop and outcome repair before reconcile, corrupt `run.json` repaired every iteration, dirty relaunch quarantined, disk full parks, final-report retry past the deadline, `--resume --supervise`, stop during watchdog backoff or while the daemon is down, worker halts park, wall-clock deadline after sleep, artifacts cleaned without commits, `--new-run` stopping the old agent, isolated brief parts, wrappers via symlinks and `CDPATH`, limit detection on the output tail, locked ledger repair |
| `tests/test_control.py`, `tests/test_learning.py` | Board request helper (including withdraw refused after a receipt) and the learning store (malformed rows, locked concurrent writes) |
| `tests/test_board.py`, `tests/test_board_hardening.py`, `tests/test_board_graph_signals.py`, `tests/test_board_js.py` | The board server and front end: ETags (heartbeat excluded; liveness via `/api/health`), quarantine refs listed only when supervisor-named and shell-quoted, LRU ledger cache, JSON errors and bounded parameters, cross-site refusal, the new graph signals (code changed, contradicted by, resolved, tiers, gaps, questions), and the drafts store run under node |
| `tests/test_graph.py` | The graph engine end to end: ingest, refine, citations, ratings, and the CLI round trip |
| `tests/test_instructions.py` | Every helper command quoted in SKILL.md, max/, references/, docs/, README, and the cycle prompt parses with that helper's parser; every documented `IMPROVE_*` setting exists; the skill and runtime versions agree; improve-max branch names can be created; the CI workflow keeps macOS, both Pythons, lint, timeouts, and the order check |

`python3 tests/lint.py` checks every Python file against Python 3.9's grammar and for unused
imports and unused locals (CI runs it with `node --check` on the board scripts).
`python3 tests/reverse_order.py MODULE...` (from `tests/`) runs test methods in reverse order to
catch tests that depend on a shared fixture's state; the nightly job runs it.

Latency ceilings are deliberately generous; they catch quadratic regressions rather than
measure machine speed. A large-graph browser check builds its own repository:

```sh
uv run --with playwright python tests/browser_graph_scale.py
```

It covers first paint and frame time with 4,500 tasks and 350 notes, type filters, search,
deep links to busy, merged, and removed nodes, zoom/wheel/pan/node drag, keyboard selection,
pinning and adding knowledge while a background daemon syncs and writes, export, and phone
overflow.

## Browser checks

Optional browser checks use Playwright with an installed Chrome executable (the macOS
default is detected), or a Playwright-managed Chromium installation:

```sh
uv run --with playwright python tests/browser_board.py --chrome /path/to/chrome
```

These exercise filtering, task details, automatic refresh, export, escaped task text,
connection recovery, run-health/report states, heartbeat and cycle ages with their
thresholds, the cycle timeline, the journal, daemon-log, and quarantine dialogs with focus
return, the blocked-reason banner, the stale-data model (controls stay enabled for thirty
seconds without fresh data, then disable, then recover), the dark theme toggle, and
mobile/tablet overflow. A fixture with
over 2,000 finished tasks checks bounded row rendering, pagination, search across pages,
keyboard navigation, date fallbacks, and position preservation during live updates.
Control checks cover pause/resume/stop with fake Codex and Claude workers, unchanged
deadlines, stale tabs, concurrent submissions, same-origin capabilities, request
acknowledgments, lost-response retries, preserved drafts, and saved-report downloads.
Archive checks cover request search/export, links with reused IDs, task/bet status,
damaged archives, safe report selection, late report responses, and request reads during
run archival. Browser checks also verify archived views on mobile and stable pagination.
Workflow checks use 1,500 active tasks and exercise worklist sorting/filtering/export,
bookmarks, missing/stale task links, follow-up requests, draft recovery after reload,
lost-response retry IDs, repository isolation, blocked browser storage, and bounded
session-capability recovery.
Triage checks cover atomic batch validation, capacity limits, concurrent batches, durable
bulk retry IDs, selection across filters and pages, task-note recovery and archived threads,
overview counts and activity links, stable activity pagination, and complete JSON exports.
Mobile checks cover automatic phone detection and the layout override, column chips and
swipes, empty-column recovery after filtering, a scripted speech recognizer for hands-free
focus/task/guidance commands, voice cancel, the explicit stop phrase, field dictation,
unsupported browsers, and mobile overflow.
Knowledge checks (`tests/browser_graph.py`, also runnable alone) cover the graph view,
node selection and links, pin/archive curation reaching the skill's brief, neighborhood
focus, opening a task from the graph, the task dialog's knowledge section, adding knowledge
with a retry-safe id, search, refinement, dreaming from the board, an inferred link's
confidence and evidence, rejecting it, restoring a forgotten note, and phone overflow.
Focus checks cover per-area prefer/normal/skip selection, presets, unsaved choices surviving
live refresh, pending/applied states, area filtering from a tile, and mobile overflow.
Screenshots are written to a temporary directory
unless `--artifacts PATH` is supplied. Playwright is only a development dependency.

The README uses [actual project-board screenshots](screenshots/README.md). Keep that gallery
separate from test fixtures; its capture instructions preserve the recorded task data.
