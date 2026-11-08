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
for improve_script in scripts/*.sh; do
  bash -n "$improve_script"
done
```

The regression suite uses temporary git repositories and fake Codex and Claude CLIs. It tests intake,
deadlines, early completion, discovery continuity, restart, rate limits, lock ownership,
timeouts, dirty state, recovery, repeated-scan detection, stop/deadline cleanup, report-only
retries, engine/model persistence, authoritative clocks, rate-limit restart accounting,
failed-success claims, stale reports, missing CLI failures, board startup/stop/history,
malformed ledgers, HTTP boundaries, live task data, experiment evidence, bookkeeping-only
churn, intake metadata preservation, authoritative board outcomes, and finalization without
launching a real model or editing a real app.
It does not establish that every model will find useful changes for a multi-hour run.

### Knowledge graph suites

| Suite | What it proves |
| --- | --- |
| `tests/test_graph_queries.py` | Every query (`context`, `search`, `node`, `path`, `hotspots`, `brief`, board views, CLI) against a fixture with known answers, including path spellings, unsafe input, limits, aliases, and ordering |
| `tests/test_graph_lifecycle.py` | Ledger edits update and delete graph links, vanished tasks are removed (unreadable ledgers never cause mass deletion), pruned outcomes are counted exactly once, refinement runs on schedule and is idempotent, damaged stores recover from the refined backup, concurrent processes lose no writes and readers never see a torn file |
| `tests/test_graph_lifecycle.py` soak | A compressed 48-hour run (288 ten-minute cycles over six archived runs): tasks move through every status, get unblocked, change files and areas, cite and drop knowledge, leave the ledger, and files are deleted. After **every** cycle it checks structural invariants and that the graph matches the ledger exactly, runs every query, and at the end checks every file and area count against the ledgers. Set `IMPROVE_GRAPH_SOAK_CYCLES` to run longer |
| `tests/test_graph_lifecycle.py` scale | 6,000 archived tasks, 3,000 files, and 450 notes: size caps, conservation of outcomes across pruning, and latency ceilings for every query |
| `tests/test_graph_server.py` | Board endpoints over a 3,000-task graph: bounds, payload sizes, parameter abuse, authentication, validation of every action, concurrent retries applied once, concurrent notes all kept, cache freshness, and reads during live daemon writes |

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
connection recovery, run-health/report states, and mobile/tablet overflow. A fixture with
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
with a retry-safe id, search, refinement, and phone overflow.
Focus checks cover per-area prefer/normal/skip selection, presets, unsaved choices surviving
live refresh, pending/applied states, area filtering from a tile, and mobile overflow.
Screenshots are written to a temporary directory
unless `--artifacts PATH` is supplied. Playwright is only a development dependency.

The README uses [actual project-board screenshots](screenshots/README.md). Keep that gallery
separate from test fixtures; its capture instructions preserve the recorded task data.
