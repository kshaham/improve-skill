# Local Kanban board

The board shows the target repository's `.improve/` ledger and lets the user control a
running daemon or submit requests for the skill. It needs
Python 3.9+ and a browser, with no npm install, hosted account, or external assets.
It does not launch agents or rewrite task status directly. Requests and responses live
separately from the worker-owned task ledger; see "Board controls and user requests" below.

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
reuse the same healthy service for that repository. The preferred port is 8765 (or
`IMPROVE_BOARD_PORT` when set); an occupied preferred port falls back to an available one. Use `--port 0` to request a free
port or `--port 9000` for a specific port. An occupied explicit port is an error. To change
an existing service's port, stop it first. Omitting `--start` runs the server in the
foreground until Ctrl-C. All modes require the Git repository root.

**One board per codebase.** A board shows only its own repository: its tasks, its knowledge
graph (`.improve/graph.db`), and its wiki (`.improve/wiki/`); nothing is shared between
repositories. Running boards record themselves in `boards.json` beside the global learning
store (`~/.config/improve`, or `IMPROVE_LEARNING_HOME`), so they can lead to each other:

- **The usual address follows the work.** The board on the preferred port (8765) sends its
  page to the board of the codebase being improved now (a live run first, otherwise the
  latest start), on the same tab, when its own run is finished or older. That board says
  where the reader came from and links back.
- **Any other address stays put.** A board on another port was opened for its own codebase;
  it shows a notice with a link to the active one and never redirects.
- The sidebar lists the other codebases' boards. Those links add `?stay=1`, which keeps that
  tab on the chosen codebase for the rest of the browser session.
- A board whose own run is live never sends its page away, and a page opened through a phone
  host is never redirected to `127.0.0.1`.

Always report the URL that `--start` or `--status` printed for this repository.

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
For phone access, pass `--allow-host HOSTNAME` (daemon: `--board-allow-host`) for an HTTPS
tunnel such as Tailscale Serve that forwards to the loopback port; the board accepts that
Host only with an `https://` origin. Voice commands arrive as ordinary board requests and
receive the same checkpoint handling.

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
scrolling. Tab arrows switch Board/History/Overview/Requests/Knowledge; Enter opens the focused card or row, and
Escape closes its details.

Open a card or row for evidence, acceptance checks, verification, files, and commit
details. Blocked tasks lead with `blocked_reason`, `blocked_by`, `blocked_at`, and
`block_count`, and show the supervisor's `supervisor_note` as its own section. Export downloads JSON: History exports every matching finished task across all
pages with its outcome filter; Board exports all matching tasks, including finished work.
The page refreshes every three seconds while visible and every ten seconds while hidden. Connection
failures retain the last view and show a warning with the data's age; controls stay enabled
while the last snapshot is under 30 seconds old and then close with the reason until a
refresh succeeds. Failed polls back off (3, 6, 12, 24, then 30 seconds), pause while the
browser reports itself offline, and resume on reconnection. The countdown and the health
ages advance from the server's clock plus the time since the snapshot, so a phone with a
skewed clock still shows the right deadline. The snapshot's ETag covers everything the board
renders except the moving clock and liveness (`heartbeat_at` and the heartbeat and cycle
ages): a supervisor that only refreshed its heartbeat, as the pause loop does every five
seconds, yields 304. The browser reads liveness from `/api/health` (`control_id`,
`heartbeat_at`, `heartbeat_age_seconds`, `cycle_started_at`, `cycle_age_seconds`,
`daemon_running`, `snapshot_age_seconds`) every five seconds, fifteen while hidden, and uses
whichever report is newest. Parsed ledgers are cached by inode, mtime, and size; the cache
holds six files per run directory (at least 64, at most 4,096) with least-recently-used
eviction, so an unchanged **All runs** poll re-parses nothing however many runs are archived. Partial/malformed ledger rows generate
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
The panel also reads the supervisor's `cycle_started_at`, `child_pid`,
`cycle_timeout_seconds`, `finish_grace_seconds`, `quarantines`, `recoveries_total`,
`crashes`, `limit_waited_seconds`, `next_limit_wait`, `park_until`, `ended_at`, and
`cycle_history` (missing fields read as null or zero) and renders heartbeat and cycle ages
with amber/red thresholds (75% and 100% of the cycle limit; the heartbeat is red past twice
the limit), supervisor counters, and a timeline of the last 24 cycles. **Journal**,
**Daemon log**, and **Quarantine** open text views of `journal.md` (per run, last 1 MiB),
the current run's `daemon.log` tail (at most 2,000 lines, 256 KiB, refreshed every five
seconds while open), and `refs/improve/quarantine/*` with a restore command per ref. Only
refs matching the supervisor's naming (`refs/improve/quarantine/<YYYY-MM-DDTHH-MM-SSZ>[-label]`
with a label of letters, digits, `.`, `_`, `-`) are listed, with the ref shell-quoted in the
command; others are counted as `hidden` and never echoed.
**Notify me** enables browser notifications, each fired once per event: run outcome set,
phase becomes `account-limit` or `parked`, three consecutive failures, deadline reached, a
task entering Blocked, or a request receiving a response. The same events fill the panel's
**Events** log without permission, and the tab title is marked while halted or parked.
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
current work, and follow-up requests may reference any run. **Retry task** (`retry`, with
the blocked task's `task_key`) and **Withdraw** (`withdraw`, with a pending request's
`target_id`) are offered only when the paired runtime lists those types in
`controls.request_types`; otherwise the server refuses them with `unsupported_request_type`.
A replacement run has a new
identity, so forms opened before it started are rejected with their drafts intact.
Requests have stable IDs for safe retry after a lost response. The board shows their
pending/applied/declined responses in a separate tab. `operator.lock` serializes submissions
and acknowledgments, worker request reads, and report reads against new-run archival (a
report read waits at most two seconds for the lock, then answers 503 with code `busy`); the
polled board snapshot reads without it, because ledgers are replaced atomically and a
half-archived state only produces warnings. The lock stays in place across runs. Archived
pending requests do not become current work; a new submission always goes to the current run.

The server binds only `127.0.0.1`, validates local Host headers, and serves allowlisted
board assets, ledger fields, user requests/responses, and the bounded saved final report.
Three read-only operational endpoints follow the same Host and origin rules: `/api/journal`
(a run's `journal.md`, last 1 MiB), `/api/log` (the current run's `daemon.log` only, at most
2,000 lines and 256 KiB), and `/api/quarantine` (ref names, commits, dates, and subjects of
`refs/improve/quarantine/*`). None follows symlinks. Every `/api/*` read refuses a request
whose `Sec-Fetch-Site` is `cross-site` or `same-site` (403, code `cross_site`), so another
site cannot read board state or hold the operator lock through `/api/report`; links that
open the board page itself still work. Graph reads take bounded whole-number parameters
(more than nine digits, or anything that is not a whole number, is a JSON 400), and an
unexpected fault in any graph read or write, request, or operational view answers JSON 500
with code `internal` instead of dropping the connection. Graph reads for the board:
`/api/graph` (the bounded view, including dream `gaps`), `/api/graph/node` (details with
`code_changed` and each link's `tier`), `/api/graph/context` (the task dialog's knowledge,
with the open task excluded inside the ranking, insight `code_changed`/`contradicted_by`,
blocker `resolved`, `gates`, `code` dependents/imports/notes, and module `groups`),
`/api/graph/history`, and `/api/graph/questions` (suggested questions with their `kind` and
whether their `id` opens a node). The server does not serve arbitrary
repository files, intake answers, other logs, or authorization state. Task, request,
report, journal, and log content renders as text, never HTML. Browser mutations
require an explicit same-origin request, a per-service browser capability, and a matching
run identity. That capability cannot stop the board service; the CLI stop action uses a
separate private token. Requests have length limits and are never executed as shell commands. Treat task
content as locally visible and keep secrets out of task evidence. No remote fonts,
analytics, CDNs, or model calls are used.

# Board controls and user requests

Read this when a run has a local board. The board can pause/resume a running supervisor,
request a stop, and save user requests for either Claude Code or Codex. Foreground runs
consume the same requests; process controls require the daemon. The board never launches
a model, changes its provider, extends time, or rewrites worker-owned task ledgers.

## Read requests at checkpoints

Before selecting an item, after settling an item, and after a context reset, run the
installed skill's helper (use its actual absolute path):

```sh
python3 <skill-directory>/scripts/improve_control.py --repo /absolute/repo pending
```

It prints the run identity, the pause/stop flags, and pending requests oldest first as a
TOON table (`requests[N]{id,type,...}:` then one row per request; `--format json` for JSON).
A request saved during an item waits for its next checkpoint; never claim immediate interruption.
Read and apply requests yourself. The supervisor makes their location discoverable but
does not pretend that saving or displaying one has fulfilled it.

Requests are user intent. Preserve existing scope/exclusions unless the user explicitly
changes them. Proposal approval covers the named proposal; it is not blanket permission
to deploy, publish, spend, or make unrelated changes. If a request needs clarification,
explain the missing detail in the current conversation and leave it pending; under the
daemon there is no conversation, so acknowledge it `declined` with the question in the note
instead (the user sees it on the board and can send a clearer request). Continue
independent authorized work. A request outside the remaining duration may be queued as
unimplemented work; it must not extend the deadline.

| Type | Apply at a safe checkpoint |
|---|---|
| `task` | Add a task with the requested title, area, details, and observable acceptance checks. Use `id: "user-<request UUID>"` and `board_request_id: "<UUID>"`; preserve the UUID across recovery. Set `ready` when within scope or `proposed` when a concrete decision is still needed. |
| `guidance` | Update the saved focus/constraints and next action, recording the request UUID and the user's intent in the journal. Carry that guidance into subsequent cycles. |
| `priority` | Find the current task by its exact `task_key`; for a bet use `bets.jsonl`. Record `user_priority` as `high`, `normal`, or `low`, and rank accordingly within the user's scope and focus plan. Do not overwrite severity or fabricate supporting evidence. |
| `note` | Append `{request_id: "<UUID>", text: "<user text>", at: "<UTC timestamp>"}` to the target's `user_notes`, only if that UUID is absent. Notes can annotate finished current tasks/bets; preserve status, evidence, verification, measurements, and rejection reasons. Acknowledge after saving. |
| `focus` | `areas` maps task areas to `prefer` or `skip` (omitted areas are normal; an empty map resets to balanced). Optional `themes` lists improvement categories chosen from board tiles (see "Improvement categories" below). Optional `agents` (a whole number from 1 to 10) is the board's **Work in parallel** switch: see "Parallel agents" below. Replace `run.json.focus.board_areas` with `{prefer: [...], skip: [...], themes: [...], agents: N, request_id: "<UUID>", at: "<UTC timestamp>"}` (leave `agents` out when the request has none) and journal the change. Rank `prefer` areas ahead of unmarked areas after `user_priority`; do not start new tasks or finder lanes in `skip` areas, and leave existing ready work there queued rather than rejected. A later focus request fully replaces an earlier one. Focus cannot add areas excluded at intake, widen authorization, or suppress a confirmed critical in-scope vulnerability; say so in the acknowledgment when it applies. |
| `retry` | The user says a blocked task's blocker is gone (`task_key`, `expected_status: blocked`; `text` may say what changed). Re-check the blocker; if it is cleared, set the task back to `ready` with a `reopens`-style note recording the request UUID and keep its original evidence; if it is still blocked, decline with what is still missing. Never retry without confirming the blocker changed. |
| `withdraw` | The user cancelled a pending request (`target_id`). The helper already hides the target from `pending`; acknowledge the withdrawal as `applied` after confirming nothing from the target was applied, or `declined` with what had already been done. A request the skill already answered cannot be withdrawn. |
| `decision` | Verify that the named item still awaits that decision. An approved ordinary proposal becomes eligible `ready` work; a declined proposal becomes `rejected` with the user's reason. For improve-max, preserve its required characterization/spike gates; approval does not mean a bet has landed or passed measurement. |

Never apply an old request to a similarly named task in another run. Finished targets
remain finished; notes add context, while follow-up work needs a new task. When a target changed
since submission, explain whether the decision still applies. Conflicting requests are
handled in order, with the later explicit direction taking precedence where applicable.
The helper includes `expected_status` for targeted requests: compare it with the current
ledger before acting. Its run identity, flags, and requests are read under the archival
lock so they always belong to the same run. Reread at each checkpoint.

Bulk priority changes validate and save 1–50 selected targets atomically. Invalid or stale
targets reject the whole submission without partial requests. A successful batch creates
ordinary `priority` requests with independent IDs and responses, not an all-or-nothing
worker operation. Recheck each target at its checkpoint; decline one that has since
finished without blocking the others. Browser retry IDs persist with the batch draft,
including after a lost response. Never apply notes or priorities as verification evidence.
If a note asks for more implementation, create separate follow-up work under the existing
scope and gates, retaining the note's request UUID for recovery; do not reopen its source.

## Acknowledge an actual result

After atomically saving the plan/task change, acknowledge the request with a short,
specific result. Include the created task ID or affected proposal when useful:

```sh
python3 <skill-directory>/scripts/improve_control.py --repo /absolute/repo ack \
  --id REQUEST-UUID --status applied --note 'Queued user-REQUEST-UUID with mobile acceptance checks.'
```

Use `--status declined` with a reason when the request cannot apply. Never acknowledge
before the change is saved. `applied` means the request affected the plan or ledger; it
does not mean implementation is finished. The task's normal verification determines Done.
After interruption, look for its `board_request_id` or journal UUID before applying again,
then acknowledge the existing result. Repeating the same acknowledgment is idempotent;
changing an existing response is rejected so history stays truthful.
For notes, also check `user_notes[].request_id` before appending again after interruption.

`operator.json` holds immutable submitted requests. `board-receipts.json` holds responses.
Only the helper updates these files, using a shared lock and atomic writes. The board
does not compete with the worker to write `backlog.jsonl`, `bets.jsonl`, `run.json`, or
`supervisor.json`. Run archival moves requests, receipts, and pause state together;
`operator.lock` remains in place so its inode continues to coordinate concurrent access.
The Requests tab uses the shared run selector to show current, archived, or all runs.
Text search includes request details and skill responses; status filtering and JSON export
cover every matching page. Twenty rows render at a time, and new arrivals preserve your
place on older pages. Archived pending requests remain historical; do not carry them into
a replacement run. Creating a request while viewing an archive always targets the current
run and returns the view there after saving.

Related work links match `board_request_id` or the exact target key within the same run.
They show task/bet status and open its evidence, including completed work. Keep those IDs
when updating ledgers; an applied receipt alone does not imply the task is done. Missing
links mean no matching work was recorded. Damaged archived request files show a warning
without disabling the current run's controls.

**Create follow-up** submits a new task request with the source task key/status and commit
in its details. Preserve that context and create separate work with acceptance checks;
do not reopen or overwrite the source task merely because the form came from its details.

Unsubmitted drafts live only in this browser, separated by repository, run, request type,
target, and proposal decision. They are not user requests until explicitly submitted.
Closing keeps a draft; Resume draft opens the most recently edited one. Every request type,
including `retry` and `withdraw`, has drafts; a form closed with nothing typed (a priority
form with its level unchanged) keeps none. An unreadable saved draft is dropped by itself and
the rest stay readable and writable; drafts written by a newer board version are never
overwritten. Submission clears that draft; Discard draft removes it. A stale draft can be copied into a new request but
must never be silently retargeted to a new run. Retry IDs persist across reloads so a
response lost after a successful save can be retried without creating duplicate work.

The **Reports** button lists saved final reports for all runs. It starts with the selected
run's report when available, otherwise the current or latest archived report. Reports are
bounded to 512 KiB, displayed as plain text, and downloadable with the run in the filename.

## Pause, resume, and stop

Pause is cooperative. If the helper reports `pause_requested`, settle the item in hand,
save its actual evidence and next action, collect/cancel any finders, and return a clean
checkpoint to the daemon. Do not wait inside a child cycle. The supervisor holds between
cycles without launching either model until resumed, stopped, or the original deadline
expires. Pause preserves failure counters, engine/model, rate-limit retry time, intake,
and the original wall-clock deadline. Deadline expiry still permits the bounded final
report; it permits no new improvement work. Do not apply new requests during finalization.

The UI distinguishes Pause requested from Paused. Resume can cancel a pending pause or
release an already paused supervisor. Stop uses the existing stop request and cleanup
grace, then ends the run; it does not stop the board. Resuming an ended/halted run requires
the existing CLI recovery checks and original settings. Existing daemons started before
board controls were installed must be restarted with the updated skill to enable them.

Every browser mutation requires a same-origin session capability and the current run's
identity. Old tabs cannot control a replacement run. The browser capability is separate
from the CLI-only service stop token. Reports are rendered as text and can be downloaded;
the board never executes report or request content as code.

## Parallel agents

A focus request with `agents: N` means the user ticked **Work in parallel** on the board and
set the slider to N. It is their explicit request to use helpers: in Claude Code, subagents or
one workflow of at most N agents (the board switch is the opt-in that workflows need); in
Codex, its subagents when the host has them. It lasts until a later focus request, which
replaces it; a request with no `agents` field turns it off, and then you decide for yourself
when a helper is worth it, as before.

While it is on:

- **Use helpers where they are safe:** refill finders (N replaces the default cap of four),
  profiling and measurement, and independent counter-scenario review. Give each helper a
  distinct, read-only assignment and the evidence rules in `finder-briefs.md`.
- **Never run more than N at once,** counting every subagent and every agent inside a
  workflow. Finish or collect helpers before a daemon cycle ends; none carry over.
- **Helpers never edit.** Fixes stay as SKILL.md describes them: one item at a time, in the
  main session, each behind its gate. A helper that finds a fix describes it with evidence;
  you make the change.
- **Nothing else widens.** Helpers follow the run's scope, exclusions, and authorization;
  a helper's finding is a candidate until you confirm it against the code.
- **No helpers available?** Run the same assignments yourself, one after another, and say
  so in the acknowledgment note.

Acknowledge the request as `applied` with the number you will use, for example "Using up to
5 parallel agents for discovery and review." Report helper use in the hourly report's
discovery line; do not count helper activity as progress.

## Improvement categories

Read this when `run.json.focus.board_areas.themes` (or a pending `focus` request) names
categories. Each category prefers its listed areas and tells you what kind of work to look
for inside them. Categories narrow where you look; they never widen intake scope,
authorization, or exclusions, and every change still needs normal verification.

Rank work matching a chosen category ahead of other work in the same priority tier.
When several categories are chosen, rotate discovery between them instead of draining one.

| Id | Category | Areas | Look for |
|---|---|---|---|
| `ux` | UI/UX polish | ui, accessibility | Visual hierarchy, inconsistent spacing/type, missing empty/loading/error states, confusing flows, unclear copy, dead-end screens |
| `animation` | Animation & transitions | ui, performance | Abrupt state changes, missing feedback on press/submit, layout jumps, janky (non-compositor) animations, missing `prefers-reduced-motion` handling, durations over ~300 ms for small UI |
| `security` | Security hardening | security | Unvalidated input, missing authorization checks, injectable queries/commands, secrets in code or logs, unsafe deserialization, known-vulnerable dependencies |
| `scale` | Scaling & production | performance, resilience, concurrency | N+1 or unbounded queries, missing pagination/limits, no timeouts or retries with backoff, missing caching, unbounded memory/queues, single points of failure, missing graceful shutdown, config not externalized |
| `performance` | Speed & performance | performance, gate-speed | Slow startup/load, heavy bundles, unnecessary re-renders, hot-path allocations, blocking I/O on main thread; measure before and after |
| `accessibility` | Accessibility | accessibility | Keyboard traps, missing labels/roles, low contrast, focus order, missing alt text, small touch targets |
| `reliability` | Reliability & errors | resilience, quality | Swallowed errors, crashes on bad input, missing retries for transient failures, inconsistent state after partial failure |
| `observability` | Logging & monitoring | resilience, quality | Unstructured or missing logs at failure points, no health/readiness checks, errors without context, missing metrics for key operations; never log secrets or personal data |
| `tests` | Test coverage | coverage, quality | Untested critical paths, assertion-free tests, flaky tests, missing edge cases for recent bugs |
| `features` | Feature completeness | features | Half-built features, TODO-gated paths, missing edge cases the user's outcomes imply |
| `mobile` | Mobile & responsive | ui, accessibility | Overflow at 320–430 px, tiny touch targets, hover-only interactions, viewport/safe-area issues, slow mobile loads |
| `dx` | Developer experience | gate-speed, docs, quality | Slow or flaky local gates, unclear scripts, missing setup steps, confusing errors for contributors |
| `docs` | Documentation | docs | Outdated instructions, undocumented commands/config, wrong examples |
| `api` | APIs & data contracts | contracts | Unvalidated payloads, undocumented breaking changes, missing versioning, loose types at boundaries |
| `code-health` | Code health | quality | Dead code, duplication, oversized functions, misleading names; refactor only with tests that pin behavior |
| `assets` | Assets & media | assets, performance | Oversized images, missing responsive variants, unoptimized fonts/icons, layout shift from media |
| `privacy` | Privacy & compliance | security, contracts | Personal data in logs/analytics, missing retention limits, data sent to third parties without need |
| `i18n` | Internationalization | ui, features | Hard-coded user-facing strings, locale-unaware dates/numbers/currency, layouts that break with longer text or right-to-left |
| `seo` | SEO & discoverability | ui, performance | Missing titles/meta descriptions, non-semantic markup, missing link previews, broken sitemaps/robots rules (web projects only) |
| `concurrency` | Concurrency & races | concurrency | Shared mutable state, check-then-act races, missing locks, async ordering bugs, unsafe cancellation |

If a category does not apply to this project (for example `seo` in a CLI), record that
once in the journal and continue with the other chosen categories.
