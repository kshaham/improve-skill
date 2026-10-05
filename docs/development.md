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
Screenshots are written to a temporary directory
unless `--artifacts PATH` is supplied. Playwright is only a development dependency.

The README uses [actual project-board screenshots](screenshots/README.md). Keep that gallery
separate from test fixtures; its capture instructions preserve the recorded task data.
