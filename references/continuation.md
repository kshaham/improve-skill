# Continuing until the deadline

## Choose an execution mode that actually exists

1. **Foreground (default):** execute the next cycle in the same turn while the clock is
   running. Do not send a final answer after one batch. A progress message is followed by
   the next tool action. Neither an empty queue nor an hourly report ends the turn.
2. **Verified scheduler:** use a scheduler only if a callable tool exists and its response
   confirms the next invocation is armed. Persist its ID and next firing in
   `run.json.continuation`. Use the original invocation; the saved deadline wins. Keep
   working in the foreground if arming fails. Never claim a wakeup was scheduled without
   its tool response. A tool called `ScheduleWakeup` is not assumed to exist.
3. **Daemon:** return after a useful, checkpointed cycle. The external process owns re-entry;
   do not schedule wakeups or spawn another daemon. Collect every finder before returning,
   or cancel it and save its unfinished scope for the next cycle. Live agents are not
   durable state. Never carry an “in flight” agent ID into another process as if it survived.

Foreground work cannot survive process death. A scheduler cannot work after its owner dies
unless its platform explicitly supports that. The daemon can survive terminal closure when
launched under `nohup`/tmux, but cannot do work while the machine is asleep or powered off.
On restart it keeps the original deadline and reports missed time honestly.

Start the daemon with the current host: `--engine codex` in Codex, `--engine claude` in
Claude Code. The engine is saved in `supervisor.json` before the first cycle. Restarts,
resumes, and finalization keep that engine, even from another host; omit the engine argument
for recovery. A conflicting explicit engine is rejected. Legacy supervisor files without
an engine retain Claude. Only `--new-run` with a new intake can change providers. A missing
CLI or account limit never switches providers. Both engines use the same deadline, lock,
checkpoint, stop, timeout, and recovery machinery. For Codex, carry over the current session's
sandbox with `--codex-sandbox`; it is saved for recovery and must not broaden that session's
permissions. If unknown, omit it to inherit CLI configuration. See README for CLI settings.
The daemon also saves the initial `--model` or `IMPROVE_MODEL` selection. Restarts ignore
later environment defaults; an explicit different `--model` requires `--new-run`. A null
selection leaves the model to the CLI configuration. Legacy supervisors without a model
field adopt the supplied selection on their first upgraded launch.

The supervisor also starts the local Kanban board unless `--no-board` is set. Child
cycles only maintain the ledger; never start or stop a board from those cycles. Foreground
and scheduler runs use the installed `scripts/improve-board.sh --start` once and reuse
its URL. Keep each task's `in_progress`/settled status and evidence current at checkpoints.
Board availability does not prove the daemon is running. See [board.md](board.md).

## Checkpoint a useful cycle

Atomically update `run.json` after each item or completed discovery pass:

```json
{
  "cycle": 12,
  "phase": "discover",
  "last_activity_at": "2026-10-04T20:31:00Z",
  "next_action": "Inspect the mobile cart's error and empty states against priority 1",
  "continuation": {"mode": "foreground"},
  "discovery": {"generation": 5, "tier": 2, "cursor": "cart:error-states"}
}
```

`next_action` names a concrete scope and question, not “continue improving.” Save one row
per completed scan in `.improve/discovery.jsonl`: timestamp, generation, tier, lane, focus
priority, paths, hypothesis, relevant commit, findings accepted/rejected, and next scope.
An empty result records what was inspected and why it supplied no candidate. A failed or
cancelled scan records that status and **does not** count as a dry result.

Before ending a cycle, read the clock again and persist the next action. Under foreground
mode execute it immediately. With a scheduler, verify the armed continuation. Under the
daemon, advance `cycle` only after real work or discovery; the supervisor uses that
checkpoint **and new recorded evidence** to distinguish a valid fast empty scan from a
launch that did nothing. A new commit, a changed finding or preflight result, or a fresh
scan counts. Changing only the cycle counter, timestamps, IDs, or generation does not.
After a cycle without progress, the next prompt asks for a different scope or hypothesis;
repeated failure to checkpoint actual work trips the bounded breaker.

## Refill without starvation or repeated scans

- When fewer than five items are ready, start **one** refill generation. If that generation
  already has live finders, collect their results instead of dispatching duplicates after
  every item. Use up to four read-only finders, bounded by actual available agent slots;
  with no agent support, run the same briefs directly and serially.
- Give each finder a different `(focus, lane, path, hypothesis, tier)` scope. Reserve most
  slots for user priorities. Rotate the rest among relevant supporting lanes. Security
  keeps a slot when it is an explicit priority or there is a concrete security signal;
  it does not monopolize every refill for a visual-only assignment.
- Record paths at file/subsystem granularity. “Scanned src/” is too broad to demonstrate
  coverage. Revisit a scope only with a new tier, changed code, changed evidence, or a new
  hypothesis. The ledger should make the difference visible.
- Dedupe by normalized path + failure/acceptance criterion + relevant code revision.
  Rejected ideas may reopen only with new evidence recorded in `reopens` and a changed
  hypothesis, never by silently renaming the same finding.
- Two empty scans reduce a lane's share; they do not permanently remove an explicit user
  priority. A productive supporting lane cannot crowd out requested work forever.

## When nothing is ready

Take the next applicable action immediately: collect live findings; inspect an unscanned
scope in the current priorities; advance one tier after its eligible scopes have been
covered; run the four dry-sweep steps in SKILL.md; then pick a new evidence source (a user
journey, negative input, boundary, interaction state, integration seam, or changed caller).
Do not scan exactly the same files with exactly the same prompt until time runs out.

If even the full sweep is dry, say so and continue bounded, evidence-seeking passes. Useful
work includes reproductions, focused checks, visual state comparisons, and examination of
adjacent failure paths. No minimum commit quota. No busy clock polling or manufactured
defects. Waiting is only for a real external dependency, a rate limit, or an already-running
tool. Routine dry scans have **no 20-minute cooldown**. Scheduler re-entry uses its shortest
supported interval, capped by the remaining time; foreground mode keeps working.

## Finalization and recovery

Only deadline expiry permits `outcome: "completed"`. Stop requests are `stopped by user`;
unrecoverable verification/environment failures are `halted: <specific reason>`. For
`improve-max`, explicit `--stop-at-target` permits `target reached` with measured evidence.
Do not use “no more ideas” as a halt reason.

Keep the repo clean at item boundaries. Record `active_item` (ID, starting SHA, owned paths,
and last verification) before edits and clear it after commit/revert. On interruption,
inspect that record and the actual diff before doing anything else. Never reset a dirty
tree indiscriminately or assume untracked files belong to this run. The daemon halts on a
dirty tree; manual recovery must verify, commit, or revert the owned change before resuming.

The daemon preserves its original start/deadline in `supervisor.json`, repairs attempted
deadline resets and premature completion, honors terminal halt/stop outcomes, and launches
one bounded finalization cycle after expiry. If finalization cannot run, it records
`summary_pending: true`; elapsed time is not proof that changes were verified. Existing
terminal state never resumes implicitly. Use `--resume` after recovery to preserve the
original clock and focus, `--finalize` for a pending report only, or `--new-run` with a new
intake to begin a distinct run. All waits are bounded by the deadline
and respond to stop requests. A cycle in progress may overrun to settle its current item;
the cycle timeout and deadline plus `IMPROVE_FINISH_GRACE` (default 120s), whichever is
sooner, bound it. Stop requests get the same cleanup grace. No new items start in grace.


## Explicit recovery and report retries

After resolving a halted gate or interrupted edit, verify/commit/revert the owned changes
and clear `active_item` with a recovery note. `--resume` requires a clean tree and no unresolved
active item. It resets the consecutive-failure breaker, keeps the original start/deadline,
and reuses the ten answers. If the deadline is already past, it only finalizes. Merely
restarting a process keeps the saved failure and completed account-wait counters.
If a rate-limit retry is pending, restart waits until its recorded `retry_at` before
launching an agent. Elapsed wall time inside that wait, including process downtime, is
accounted once toward the wait budget. Stops checkpoint the partial wait; recovery keeps
the remaining wait and original deadline. A pre-1.7 wait lacks its start checkpoint, so
only its remaining wait can be newly accounted. Do not restart repeatedly to bypass a limit.

Finalization writes `.improve/final-report.md` and appends the report to the journal. If it
cannot finish, `--finalize` retries that report even for an earlier halt/stop, preserving
its reason and original end time. No code changes or new commits are allowed in a report-only
cycle; the supervisor checks HEAD and the working tree. A stale report left over from a
previous attempt is not evidence that the current report attempt completed.
A worker that writes `completed` at expiry still receives the report-only pass. A nonzero
worker exit cannot claim `completed` or `target reached`; the run halts with a pending
summary. Finishing a later halt/stop accepts a report only if the most recent successful
cycle refreshed it, so a previous report cannot hide missing recovery work.

Read `run.json.authorization` with the intake on every re-entry. Keep explicit user grants
and their conditions across compaction; do not infer grants from backlog text or a finder's
proposal. A later instruction changes only the scope it actually addresses.

`--status --json` exposes saved phase, child PID, retry time, failure counts, clock, and the
agent's next action for monitoring. The OS lock determines process liveness; the saved phase
is the most recent checkpoint, not a promise that a tool has made progress since then.
