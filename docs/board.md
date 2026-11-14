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
- **Task details:** evidence, acceptance checks, verification, files, notes, and commits. A
  blocked task opens with a banner saying why it is blocked, whether the worker or the
  supervisor blocked it, when, and how many times; its card shows the same reason.
- **Task notes and request history:** add context to current tasks, including completed
  work, and read linked requests and responses in their details. Notes preserve existing
  verification; follow-up implementation gets a separate task.
- **What the skill has learned:** the Overview tab shows verified-success rates by area,
  the lessons the skill follows, and recurring blockers, built from every finished run.
- **Knowledge graph:** the Knowledge tab draws what the skill has learned across runs:
  files, areas, tasks, gates, blockers, and knowledge, and how they connect. Search it,
  filter by node type, drag nodes, zoom, and select a node to see its links, success rate,
  and evidence. Focus a node to show its neighborhood, or open a task node in the task
  dialog. Curate knowledge with **Pin** (shown every cycle), **Useful**, **Wrong**,
  **Archive**/**Restore**, and **+ Add knowledge** (a pitfall, technique, convention,
  decision, or fact, optionally about files or areas). The skill reads your changes at its
  next cycle. **Refine now** merges duplicates and rescores; the refinement log lists each
  pass. **Dream now** infers links, archives and forgets unused knowledge, and proposes
  consolidations; the dream log lists each dream. Inferred links are dashed and show their
  confidence; hover for the evidence, and **Not related** removes one so it is never inferred
  again. Each link in a node's details names its tier: **recorded** (written by an agent or
  by you), **observed** (from ledgers, git history, or code), or **inferred** (by dreaming).
  Knowledge whose files changed since it was recorded is marked **Code changed since
  recorded** with the files to re-verify; contradicting knowledge is linked in red.
  **Recently forgotten** lists removed nodes with the reason; **Restore** brings one back
  with its links. **Knowledge gaps** lists files whose tasks keep failing with nothing
  recorded about why, and **Questions worth asking** lists what the graph suggests a person
  check: contested or contradicted knowledge, knowledge whose code changed, gaps, files that
  bridge separate groups, and loosely connected groups. Task details include **What the
  knowledge graph knows here**: earlier attempts on the same files (the open task itself is
  left out), knowledge to apply with **Code changed**, **Contradicted**, **Contested**, or
  **Proven** badges, blockers (a blocker that later verified work got past is marked
  **Resolved**), the gates that verified work there with their cost, files usually changed
  together, the code that depends on these files (tests marked), their imports and code
  notes, and the module group they belong to. The graph is kept across runs in
  `.improve/graph.db`. Knowledge export downloads the nodes and links currently shown.
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
  and pending final report. A connected board is separate from a running daemon. The panel
  also shows live ages: how long since the supervisor's last heartbeat and how long the
  current cycle has run against its limit ("Cycle running for 47 min of 1 h 00 min limit").
  Both turn amber at 75% of the cycle limit and red past it (the heartbeat turns red past
  twice the limit), so a hung child is told apart from a long cycle. Counters list
  quarantines, recoveries, crashes, and time waited on account limits; a parked run shows
  when it will retry. A compact timeline draws the last 24 cycles: green advanced, grey made
  no progress, red exited with an error; hover a bar for its start, duration, and outcome.
- **Journal, daemon log, quarantine:** three buttons in the run health panel open the run's
  hourly `journal.md` (also for archived runs), the last 400 lines of the current run's
  `daemon.log` (refreshing every five seconds while open), and the list of quarantined
  snapshots the supervisor saved as `refs/improve/quarantine/*`, each with the
  `git checkout <ref> -- .` command that restores it. Only refs named the way the supervisor
  names them (`<UTC time>-cycle-N...`) are listed, and the ref in the command is shell-quoted,
  so a hostile ref name can never become a command you paste; the view counts any other refs
  under that namespace. All render as plain text.
- **Notifications:** **Notify me** asks the browser for notification permission and then
  alerts you once per event when the run halts, stops, or completes, parks or waits for an
  account limit, reaches its deadline, fails three cycles in a row, blocks a task, or
  answers one of your requests. The choice is remembered in this browser. Without
  permission, the same events still appear in the panel's **Events** log (last 20), and the
  tab title gains a warning mark while the run is halted or parked.
- **Run controls:** pause after a clean checkpoint, resume, or stop a running daemon.
  Pause keeps the original wall-clock deadline; it works with either saved engine.
- **Improvement categories:** tap one or more of twenty tiles such as UI/UX polish,
  Animation & transitions, Security hardening, Scaling & production, Logging & monitoring,
  Mobile & responsive, Privacy, Internationalization, or SEO. Each tile prefers its task
  areas and tells the skill exactly what to hunt for (see `references/categories.md`).
  Voice works too: “focus on animations and scaling”.
- **Focus areas:** under **Fine-tune by area**, choose Prefer, Normal, or Skip for each task
  area (features, UI, security, performance, tests, docs, and more). Each tile shows open and finished work in that area and
  links to a filtered board. The skill applies a saved focus at its next checkpoint; cards in
  preferred areas are marked *Focus* and skipped areas are dimmed.
- **Mobile layout:** phones and small touch screens switch to it automatically: a bottom
  navigation bar with Board, History, Overview, voice, Requests, Knowledge, and Focus
  (it scrolls sideways on narrow screens), one board column at a time (tap a status chip or swipe left/right),
  larger touch targets, a sticky run status strip, and full-width sheets for forms. Use
  **Switch to mobile/desktop view** in the footer, or `?layout=mobile|desktop|auto`, to
  override detection in that browser.
- **Voice steering:** tap the microphone (bottom bar on phones, **Steer by voice** or
  Shift+V on desktop) and speak a command: “focus on security and skip docs”, “new task:
  add offline mode, high priority”, “the checkout should keep the cart after reload”,
  “pause the run”, or “resume”. The board shows what it heard and how it will send it;
  you can edit either. In hands-free mode it reads the command back and listens for
  “send”, “cancel”, or “again”. Stopping a run requires saying “confirm stop”. Every
  text field in request forms also has a dictation microphone.
- **New tasks and guidance:** send desired outcomes, acceptance checks, or updated focus
  directly from the board. Change a current task's priority or approve/decline a proposal
  from its details.
- **Retry and withdraw:** a blocked current task offers **Retry task**, which asks the skill
  to re-queue it at its next checkpoint (say what changed, if anything). A pending request
  in the Requests tab offers **Withdraw**, which asks the skill to drop it before acting.
  Both are ordinary requests with checkpoint responses; a withdrawn request shows as
  **Withdrawn** in the Requests tab. The buttons appear only when the
  running skill version accepts those request types; an older runtime refuses them with a
  clear message instead of saving them.
- **Dark mode:** the board follows the system colour scheme. **Theme: auto** in the footer
  cycles to light or dark for this browser.
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
`bets.jsonl`, and refreshes every three seconds while visible. The footer shows how old
the displayed data is. When a refresh fails, the board keeps the last snapshot and the
controls stay usable while it is under 30 seconds old; after that they close with the
reason ("No fresh data for N s") until a refresh succeeds. Retries back off from three
seconds to thirty, pause while the browser is offline, and resume when it reconnects.
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

## Using the board from a phone

The board listens only on `127.0.0.1`, and browsers allow microphone access only on
HTTPS or localhost pages. To use it from a phone, put it behind an HTTPS tunnel that only
your devices can reach, then tell the board to accept that hostname. For example, with
Tailscale on the computer running the board:

```sh
tailscale serve --bg --https=443 http://127.0.0.1:8765
"$improve_board" --repo "$improve_repo" --stop
"$improve_board" --repo "$improve_repo" --start --port 8765 --allow-host mymac.example-tailnet.ts.net
# Prints the local URL and "Phone: https://mymac.example-tailnet.ts.net"
```

Daemon runs take the same option as `--board-allow-host`. Requests through that hostname
must come from an `https://` page on it; plain-HTTP origins and other hostnames are
refused. Anyone who can open the tunnel URL can steer the run, so never expose it
publicly (for example with Tailscale Funnel or an open port forward).

Speech recognition is provided by the browser. Chrome and Edge send audio to their
vendor's speech service; Safari may process it on the device or with Apple. Firefox has
no speech recognition, so the microphone explains that and keyboard dictation still works.
Nothing is sent to the skill until you confirm it.

## Drafts and links

Drafts are stored in this browser for the board's local origin and repository, with a
maximum of fifty request contexts. Every request type has drafts, including Retry and
Withdraw. Closing or cancelling a form with nothing typed (for a priority change: with the
level unchanged) leaves no draft. A saved draft the board cannot read is dropped on its own;
the others stay readable and new drafts are still saved. A different browser profile or port has separate
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
show the latest saved observations, not a guarantee that an agent is still making progress;
the heartbeat and cycle ages next to them are the staleness measure, and they keep counting
in the browser between refreshes. Liveness is read from the small `/api/health` endpoint every
five seconds (fifteen while the tab is hidden), not from the task snapshot: the supervisor
rewrites its heartbeat every five seconds even while paused, and the snapshot's change tag
ignores it, so an unchanged paused run answers "not modified" instead of resending every
task. The cycle limit comes from the supervisor; when an older
daemon has not recorded one, the panel assumes one hour and says so.

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
no npm setup or hosted service. Besides ledgers and reports it serves the run journal, a
bounded tail of the current run's `daemon.log` (at most 2,000 lines and 256 KiB), and the
names of quarantine refs; nothing else from the repository. Keep `.improve/` untracked. See
[board lifecycle and task schema](../references/board.md) for details and remote-host access.
