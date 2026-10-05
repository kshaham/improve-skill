# Board controls and user requests

Read this when a run has a local board. The board can pause/resume a running supervisor,
request a stop, and save user requests for either Claude Code or Codex. Foreground runs
consume the same requests; process controls require the daemon. The board never launches
a model, changes its provider, extends time, or rewrites worker-owned task ledgers.

## Read requests at checkpoints

Before selecting an item, after settling an item, and after a context reset, run the
installed skill's helper (use its actual absolute path):

```sh
python3 /path/to/improve/scripts/improve_control.py --repo /absolute/repo pending
```

It returns pending requests oldest first, the run identity, and pause/stop flags. A request
saved during an item waits for its next checkpoint; never claim immediate interruption.
Read and apply requests yourself. The supervisor makes their location discoverable but
does not pretend that saving or displaying one has fulfilled it.

Requests are user intent. Preserve existing scope/exclusions unless the user explicitly
changes them. Proposal approval covers the named proposal; it is not blanket permission
to deploy, publish, spend, or make unrelated changes. If a request needs clarification,
leave it pending and explain the missing detail in the current conversation. Continue
independent authorized work. A request outside the remaining duration may be queued as
unimplemented work; it must not extend the deadline.

| Type | Apply at a safe checkpoint |
|---|---|
| `task` | Add a task with the requested title, area, details, and observable acceptance checks. Use `id: "user-<request UUID>"` and `board_request_id: "<UUID>"`; preserve the UUID across recovery. Set `ready` when within scope or `proposed` when a concrete decision is still needed. |
| `guidance` | Update the saved focus/constraints and next action, recording the request UUID and the user's intent in the journal. Carry that guidance into subsequent cycles. |
| `priority` | Find the current task by its exact `task_key`; for a bet use `bets.jsonl`. Record `user_priority` as `high`, `normal`, or `low`, and rank accordingly within the user's scope and focus plan. Do not overwrite severity or fabricate supporting evidence. |
| `note` | Append `{request_id: "<UUID>", text: "<user text>", at: "<UTC timestamp>"}` to the target's `user_notes`, only if that UUID is absent. Notes can annotate finished current tasks/bets; preserve status, evidence, verification, measurements, and rejection reasons. Acknowledge after saving. |
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
python3 /path/to/improve/scripts/improve_control.py --repo /absolute/repo ack \
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
Closing keeps a draft; Resume draft opens the most recently edited one. Submission clears
that draft; Discard draft removes it. A stale draft can be copied into a new request but
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
