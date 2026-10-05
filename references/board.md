# Local Kanban board

The board shows the target repository's `.improve/` ledger and lets the user control a
running daemon or submit requests for the skill. It needs
Python 3.9+ and a browser, with no npm install, hosted account, or external assets.
It does not launch agents or rewrite task status directly. Requests and responses live
separately from the worker-owned task ledger; see [board-control.md](board-control.md).

## Launch and share the URL

Use the script belonging to the installed skill; do not assume it exists in the target
application repository. For example, with the checkout under `~/Code/improve-skill`:

```sh
improve_board="$HOME/Code/improve-skill/scripts/improve-board.sh"
improve_repo="$HOME/Code/my-app"
"$improve_board" --repo "$improve_repo" --start
"$improve_board" --repo "$improve_repo" --status --json
"$improve_board" --repo "$improve_repo" --stop
```

`--start` launches a detached service and returns its actual local URL. Repeated starts
reuse the same healthy service for that repository. The preferred port is 8765; an
occupied preferred port falls back to an available one. Use `--port 0` to request a free
port or `--port 9000` for a specific port. An occupied explicit port is an error. To change
an existing service's port, stop it first. Omitting `--start` runs the server in the
foreground until Ctrl-C. All modes require the Git repository root.

For foreground/scheduler runs, start it after intake and repo selection. Daemon runs
auto-start it after preparing valid run state, using either Codex or Claude. Daemon
`--board-port` controls the port; `--no-board` skips automatic startup without stopping an
existing board. Child cycles never manage the service. Starting or stopping the board
does not restart, extend, finalize, or stop the improvement run. A startup error should
be reported while improvement work continues.

Share the URL only after start/status confirms it is serving. Include it in opening,
hourly, and final reports. The service survives terminal closure and run completion;
it ends when explicitly stopped, killed, or the machine restarts. After a restart,
`--start` restores the view from disk. A remote machine's loopback URL is local to that
machine; access it through an SSH local port forward when needed.

## Keep the board faithful to the work

| Location | Ledger status | Meaning |
|---|---|---|
| Queued | `ready` | Accepted work waiting to begin |
| In progress | `in_progress` | The current task is being investigated or edited |
| History / Completed | `done` | Verification or investigation finished; evidence recorded |
| Blocked | `blocked`, legacy `benched` | Work cannot proceed; note explains recovery |
| Proposed | `proposed` | A concrete proposal awaiting a user decision |
| History / Rejected | `rejected` | An idea failed verification or was ruled out; reason retained |

Create the row before work begins, using a stable ID, useful title/claim, area, acceptance
checks, and status. On each transition update its UTC `updated_at`, evidence/verification,
note, and actual commit when one exists. Keep one canonical row per ID, atomically rewrite
the ledger, and retain every finished/rejected item. Clear `run.json.active_item` only
after the task is settled. Legacy active items appear in In progress even when an older
backlog still says ready; an unresolved active item in a terminal run appears Blocked.
Explicitly `in_progress` tasks also appear Blocked once the run ends, with a note that
they were not settled. Unknown statuses appear Blocked with a warning rather than disappearing.

For `improve-max`, the board also reads `bets.jsonl`: `spiking`/`placed` map to In progress,
`landed` to Completed history, `killed` to Rejected history, and `proposed` to Proposed. Each bet's details show
the original phase, journey, spike/landed measurements, implementation pieces with their
statuses/commits, and kill criteria. Maintain the original bet ledger; do not duplicate
bets in the ordinary backlog. Bet IDs and ordinary task IDs remain separate in the view.

Track meaningful investigations in `discovery.jsonl`, including empty and failed scans;
the board shows recent passes. A substantial standalone investigation can also have a
task card with `kind: "investigation"`, clearly identified and backed by findings. Do not
turn every tool call into a card, invent retrospective outcomes, or create busywork.
This board can show only recorded work; it does not reconstruct missing tasks from chat.

The browser defaults to **Board** and **All runs**. Four columns hold active work. Below
them, **Recently completed** shows the five newest matching completions as compact rows.
The completed counter and **View completed history** open the full **History** view;
a rejected-count shortcut opens rejected history. Moving work into History is only a
display choice: no records are moved, removed, or rewritten.

History shows completed and rejected work in rows, twenty per page, with title, outcome,
area, recorded date, and commit. Narrow screens use fewer fields; all details remain
available by opening a row. Rows sort newest first using the first valid `completed_at`,
`updated_at`, or `created_at`. The date tooltip identifies which timestamp was used;
undated records appear last with **Date not recorded**. Equal dates sort by stable task key.

Search covers task text, files, evidence, and commits across every page. Filters narrow
by run and area, and History adds an outcome filter. Filter changes return to the first
page. On older pages, background updates preserve the first visible task when it still
matches; **Latest** returns to the newest results. Page controls remain visible while
scrolling. Tab arrows switch Board/History/Overview/Requests; Enter opens the focused card or row, and
Escape closes its details.

Open a card or row for evidence, acceptance checks, verification, files, and commit
details. Export downloads JSON: History exports every matching finished task across all
pages with its outcome filter; Board exports all matching tasks, including finished work.
The page refreshes every three seconds while visible and every ten seconds while hidden. Connection
failures retain the last view and show a warning. Partial/malformed ledger rows generate
warnings while valid rows remain visible.
Non-finite measurements (`NaN`, infinities, or overflowing numbers) are invalid records;
they produce warnings rather than breaking the browser's entire task response.

## State and boundaries

Switch **View** from Columns to Worklist to browse active work in twenty-row pages. Both
layouts support priority, update-time, and title sorting. Priority means `user_priority`
high/normal/low (absent is normal), then numeric `focus_priority`; sorting does not execute
work or override the skill's scope and verification gates. Worklist export includes all
matching active rows, including off-page work.

Worklist checkboxes select up to fifty unfinished current tasks across pages and filters.
**Select page** toggles that page; the selection count identifies hidden matches. **Change
selected priorities** lists every target before submission. Selection clears when the run
changes and drops tasks that finish. The server validates the whole batch before saving;
each priority request then receives its own checkpoint response. A changed target can
reject a new batch without saving a subset. Lost-response retries preserve the original IDs.

Task details include linked requests and their responses, ten initially with **Show more**.
**Add note** submits context for a current task, even when finished. The worker appends it
to `user_notes` without replacing evidence or reopening work. Archived threads stay
readable; adding a note requires a current target and an active request-accepting run.

**Overview** uses all tasks and requests in the selected runs, independently of task search,
area, and priority filters. It shows area totals, active work, completed work, and attention
counts (blocked/proposed, also included in active). Metrics show high-priority queued tasks,
pending requests, and completed tasks with nonempty verification entries. This last count
does not assert that checks passed. Rejected work contributes to totals but not active/done.

Recorded activity uses one saved timestamp per task (completion, update, then creation)
and each request's submission/response timestamps. It is not a complete audit log and does
not reconstruct earlier task transitions. Undated tasks remain in totals but are omitted
from activity, with a notice. Filter task/request events, browse twenty per page, and click
an event to open its task or request. Live updates preserve an older page's first event.
Overview JSON export includes summary/area totals and all matching events across pages;
its activity filter is bookmarkable.

The URL fragment records view filters and layout. **Copy task link** adds its exact task
key and, for current work, run identity. Links to a removed archive fall back to available
runs; links to a replaced current run never silently open reused IDs. These links require
the local board and the same port. A task's **Create follow-up** action starts a separate
request with its original context, leaving completed evidence untouched.

The **Current run** health panel shows the saved engine/model, cycle/phase, work and
supervisor checkpoint times, account-limit retry, consecutive failures, and pending final
report. Checkpoint timestamps are saved observations, not proof of continuous activity.
**Board connected** means the viewer is responding; daemon liveness comes from its OS lock.
An active supervisor's null outcome takes precedence over an unconfirmed worker completion.
Foreground completion recorded before its deadline is flagged without modifying the ledger.
The panel remains about the current run when filtering archived task history.

The Requests tab shares the run selector and text search, including archived skill
responses. Its related-work buttons open matching tasks and bets from that same run.
Use **Reports** to browse and download saved final reports from any run, even if the
current run has no report yet. Reports render as plain text, up to 512 KiB each.

An ended run with **Final report pending** still needs its report-only retry. During a
live report retry the board says **Writing final report** while retaining the original
outcome in stored history. Viewer warnings and pending reports never approve unverified work.

`.improve/board.json` contains the URL, process identity, and private stop token;
`board.lock` enforces one server per repo and `board.log` records startup errors. Keep
`.improve/` out of version control. Daemon `--new-run` archives the ledger under `history/`
but preserves these service files and the existing URL. Daemon `--status --json` includes
board liveness and its URL without exposing the token.

Run controls always target the current run, even while browsing archived tasks. Task
priority/decision actions require unfinished current tasks; notes may target finished
current work, and follow-up requests may reference any run. A replacement run has a new
identity, so forms opened before it started are rejected with their drafts intact.
Requests have stable IDs for safe retry after a lost response. The board shows their
pending/applied/declined responses in a separate tab. `operator.lock` serializes submissions
and acknowledgments, worker request reads, and board/report snapshots against new-run
archival. The lock stays in place across runs. Archived pending requests do not become
current work; a new submission always goes to the current run.

The server binds only `127.0.0.1`, validates local Host headers, and serves allowlisted
board assets, ledger fields, user requests/responses, and the bounded saved final report.
It does not serve arbitrary repository files, intake answers, raw logs, or authorization
state. Task, request, and report content renders as text, never HTML. Browser mutations
require an explicit same-origin request, a per-service browser capability, and a matching
run identity. That capability cannot stop the board service; the CLI stop action uses a
separate private token. Requests have length limits and are never executed as shell commands. Treat task
content as locally visible and keep secrets out of task evidence. No remote fonts,
analytics, CDNs, or model calls are used.
