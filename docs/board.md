# Using the local board

[← Back to README](../README.md)

Track work and steer a session from the browser. The board reads the project’s saved improvement state.

## Open the board

The skill creates a board and shares its local URL when a run starts. Daemon runs start
it automatically with either Codex or Claude; foreground runs launch the same helper.
You can also open a board for any repository with an existing improvement ledger:

```sh
improve_board="$HOME/Code/improve-skill/scripts/improve-board.sh"
improve_repo="$HOME/Code/my-app"
"$improve_board" --repo "$improve_repo" --start
# Prints the actual URL, normally http://127.0.0.1:8765
"$improve_board" --repo "$improve_repo" --status --json
"$improve_board" --repo "$improve_repo" --stop
```

## Views and task tools

The board includes:

- **Four working columns:** Queued, In progress, Blocked, and Proposed.
- **Active worklist:** switch Columns to Worklist for twenty compact active rows per page.
  Sort either layout by priority, recent update, or title; filter high/normal/low priorities.
  New arrivals keep your position while you browse older worklist pages.
- **Bulk priorities:** select up to fifty unfinished current tasks across worklist pages
  and filters, then send one priority change. The form lists every target; a stale target
  rejects the entire submission. Each saved request gets its own checkpoint response.
- **Recent completions:** five compact rows below the board, with a link to the full history.
- **History view:** completed and rejected tasks in rows with title, outcome, area, recorded
  date, and commit. Twenty rows per page keep long runs manageable; click any row for details.
- **Task details:** evidence, acceptance checks, verification, files, notes, and commits.
- **Task notes and request history:** add context to current tasks, including completed
  work, and read linked requests and responses in their details. Notes preserve existing
  verification; follow-up implementation gets a separate task.
- **Run overview:** compare task totals by area, see high-priority queued work, pending
  requests, and how many completed tasks have recorded verification. Browse twenty saved
  activity events at a time, filter task/request events, and open the associated details.
  The overview uses the selected runs, independent of task search and priority filters.
- **Task links and bookmarks:** copy a task link or bookmark a view. The URL preserves
  the tab, run, search, area, priority, layout, sort, and outcome/status filters.
  Missing archives recover gracefully; old current-run links cannot open reused task IDs.
- **Search and filters:** find tasks by text, area, or run, including archived history.
- **Live progress:** task counts, deadline, latest checkpoint, and recent investigations.
- **Run health:** engine/model, cycle, checkpoint times, retry schedule, failure count,
  and pending final report. A connected board is separate from a running daemon.
- **Run controls:** pause after a clean checkpoint, resume, or stop a running daemon.
  Pause keeps the original wall-clock deadline; it works with either saved engine.
- **New tasks and guidance:** send desired outcomes, acceptance checks, or updated focus
  directly from the board. Change a current task's priority or approve/decline a proposal
  from its details.
- **Follow-up tasks:** create a separate request from any task's details, including archived
  or completed work. The form retains its source task and commit for context.
- **Recoverable drafts:** forms save locally as you type. Close and reopen them or use
  Resume draft after a reload. Different request contexts keep separate drafts; discard
  removes one, and successful submission clears it. Drafts from old runs cannot be sent
  into a replacement run.
- **Requests:** search current and archived requests, including the skill's responses.
  Filter by run and status, browse twenty rows per page, and export every matching page.
  Live updates preserve your place on older pages.
- **Related work:** open a request's resulting tasks or target bet to see its status,
  evidence, and verification. Links stay within their original run when IDs are reused.
- **Reports:** browse and download saved final reports across runs. The selected run's
  report opens first when available; the picker lets you switch without leaving the board.
- **Export:** download matching tasks as JSON. Worklist exports all matching active rows;
  History exports all matching finished rows; Columns includes finished work as well.
  Every export covers all pages.
  Overview export includes area totals, summary counts, and all matching recorded activity.

## Saved work and request responses

It reads `.improve/backlog.jsonl` directly, includes `improve-max` experiments from
`bets.jsonl`, and refreshes every three seconds while visible.
There is no second task database to maintain. The skill records tasks before starting,
updates their status as work progresses, and retains finished/rejected work. Existing
ledgers work without migration; older work that was never recorded cannot be reconstructed.
Opening task details does not approve work. Explicit controls save user intent separately
from worker-owned ledgers, and the skill applies it at a checkpoint. A saved request stays
**Pending** until the skill records a response. **Applied** means its plan or task ledger
was updated; normal task verification determines whether implementation reaches Done.
Completed work stays in the ledger. History searches every recorded task, including rows
outside the current page. New results keep your place while you browse older pages; use
**Latest** to return to the newest results. Page controls stay visible as you scroll.

## Run controls

Run controls require a live daemon started with version 1.11.0 or newer. **Pause requested**
means its current cycle is still settling; **Paused** means no new improvement cycle is
running. The deadline keeps counting down, and expiry still permits the bounded final
report. **Resume** also cancels a pending pause. **Stop run** ends work after cleanup and
leaves the board online; restarting an ended/halted run uses the existing CLI recovery
checks. The board never launches a model or changes its saved engine, model, or sandbox.
Foreground sessions can read the same task/guidance requests through the skill's helper.
Requests apply to the current run; new-run archival preserves them with that run's files.
Archived pending requests stay in history and are never automatically applied to a new run.
Submitting a task or guidance while browsing an archive saves it to the current run and
returns the Requests view there. Search includes receipt text as well as request details.
The worker reads its pending requests and run identity together under the archival lock.
See [request handling and controls](../references/board-control.md) for the checkpoint contract.

## Drafts and links

Drafts are stored in this browser for the board's local origin and repository, with a
maximum of fifty request contexts. A different browser profile or port has separate
storage. If browser storage is blocked, the form explains that it cannot survive a reload;
you can still submit it. Lost-response retry IDs survive with the draft, preventing duplicate
requests after reloading. A service restart refreshes the control capability once; other
failures remain visible for an explicit retry. Task links need the board running at that
local URL and are not externally hosted links.

## Priorities and progress

The skill ranks eligible tasks by explicit board priority, then the intake
focus order and evidence-based importance. Scope, critical-security checks, dependencies,
verification requirements, and the original deadline still apply. Board sorting is a way
to browse work; it does not by itself change priorities or launch tasks.
Priority changes, task notes, and request bookkeeping do not count as improvement evidence
or reset the daemon's no-progress breaker. Real findings, verification, and measured work
still drive progress checks.

## Reading the overview

Overview activity comes from each task's recorded completion/update/creation timestamp
and request submission/response timestamps. It is not a complete audit log; undated tasks
stay in the totals but are omitted from activity. Recorded verification means a nonempty
verification entry exists, not that the board independently ran or passed a check.

The current run panel flags an inactive daemon and missing final report, and shows when a
report-only retry is running. It uses the supervisor's confirmed outcome, so a worker's
premature completion cannot make an active supervised run look finished. Checkpoint times
show the latest saved observations, not a guarantee that an agent is still making progress.

Malformed records and non-finite benchmark values such as `NaN` produce warnings while
valid task history remains visible. The viewer never rewrites those source records.

## Service lifecycle

The board stays available after the improvement run ends. Stopping it does not stop the
daemon, and daemon `--stop` leaves the board available. Restarting the computer stops the
service; `--start` brings the saved history back. Daemon `--new-run` archives old tasks
without changing a running board's URL. Multiple repositories receive separate ports;
if 8765 is busy the helper chooses a free port. Use helper `--port 9000` or daemon
`--board-port 9000` for a specific port, or `0` to choose any free port. Repeated starts
reuse the existing board. Daemon `--no-board` skips startup; an unavailable board never
prevents improvement work.

Only Python 3.9+ is required. The server binds to `127.0.0.1` and uses local assets, with
no npm setup or hosted service. Keep `.improve/` untracked. See
[board lifecycle and task schema](../references/board.md) for details and remote-host access.
