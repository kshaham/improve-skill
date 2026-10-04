---
name: improve
description: Ask ten intake questions, then continuously improve a codebase for a requested duration, prioritizing the user’s features, UI, graphics/assets, performance, bug fixes, security, and quality goals. Refill the backlog until the deadline, verify changes, and report progress. Use for timed ongoing improvement requests such as "improve this repo for 4 hours" or "keep finding and fixing issues until 6pm"; not for a one-off fix or editing this skill itself.
license: MIT
metadata:
  author: Kamal Shaham, drafted with Claude Code (Opus) in plan mode
  created: 2026-09-05
  origin: Designed to spec in the ~/Code/bonsai session (plan shimmying-roaming-goose.md); daemon added 2026-09-06 after the first 6h run hit ENOSPC
  version: 1.5.0
  changelog: |
    1.5.0 (2026-10-04) - evidence-based progress detection, bounded deadline cleanup, recoverable halts and final reports, verification-aware work selection, persistent retry counters and live status
    1.4.0 (2026-10-04) - ten-question intake, persistent user priorities, feature/UI/asset lanes, active discovery without dry sleeps, verified continuation, portable deadline supervisor and regression tests
    1.3.0 (2026-09-22) - the run lasts until the deadline: scripts/improve-clock.sh is the only authority on time, an empty backlog is a refill not a finish, an exhaustive list of stop conditions, dry-at-T4 keeps sweeping new ground instead of idling out
    1.2.0 (2026-09-17) - six new lanes (gate-speed, concurrency, resilience, docs always; accessibility, contracts when detected), lane rotation under the finder cap, benchmark-harness-first rule for performance
    1.1.0 (2026-09-17) - gate signal counts, counter-scenario check, interleaved performance measurement, scanner baseline in the security lane, usage-limit backoff in the daemon
---

# Continuous improvement loop

Work a codebase for a stated duration, guided by the user's priorities for features, UI,
graphics/assets, performance, bugs, security, and supporting quality. Verify every change
and report every hour. When the obvious work runs out, discover fresh evidence and keep
working until the deadline.

    /improve <duration> [path]

`/improve 4h`, `/improve 90m ~/Code/api`. Duration is required; path defaults to the
current working directory.

## Intake - first, before the timer

For each **new run**, ask the user the exact ten questions in
[references/intake.md](references/intake.md). Wait for answers before starting the timer,
launching finders, or editing code. Save all ten answers and derive an ordered focus plan.
A wakeup, daemon cycle, or compacted session resumes the same run and does not ask again.
A noninteractive launch requires previously collected answers; never fabricate them.

The focus plan controls which lanes, features, screens, assets, and journeys receive the
most effort. Technical cleanup supports that plan; it must not quietly replace it. Read
[references/continuation.md](references/continuation.md) before launching to select a real
continuation mechanism and checkpoint the next action. Foreground continuation is the
default when no scheduler is available. Use `references/work-selection.md` to turn the
answers into observable goals, prioritize discovery fairly, and budget verification.

## What makes this different from ordinary work

You are unattended. Nobody will catch a bad change before it lands, and nobody will notice
if you quietly stop finding real problems and start inventing them. Three obligations follow,
and they outrank throughput:

1. **Every change is proven or reverted.** There is no "looks right". A change whose gate
   did not run is a change that did not happen.
2. **Every report is honest.** A cycle that found nothing says so. Padding an hourly report
   with cosmetic churn to look productive is the defining failure mode of this skill, and it
   is worse than an idle hour because it costs review attention and buries the real work.
3. **The run lasts until the deadline.** The duration is the user's instruction, not an
   estimate. An empty backlog is a reason to look harder, not a reason to stop, and a run
   that writes its final report early has failed at the one thing it was asked to do - as
   surely as one that commits an unverified change. This has happened: a 2h run wrote
   "hour 2 of 2 (final)" seventeen minutes in, and a 3h run wrote FINAL fifty minutes early
   with "tier 1 throughout", both because the queue they started with had run out.

## Hard rules

These bound autonomous work. If a finding needs an exception, save it as a proposal and
continue eligible work; do not halt the entire run to request routine approvals:

- **Never touch `main`.** Work on `improve/<YYYY-MM-DD>`, created at preflight.
- **Never push, never force-push, never open a PR.** Landing is local commits only.
- **Never delete a test, weaken an assertion, or add a skip** to make something green. Test
  changes are additive. If a test genuinely encodes wrong behaviour, that is a backlog item
  for the human, not a fix.
- **Never bump a dependency** - not Go modules, not SPM, not npm, not the toolchain.
- **Never add a migration or change a schema.**
- **Never rewrite a money-adjacent or once-only invariant.** Anything that pays out, charges,
  or stamps a row exactly once is read-only. Findings there go to the backlog with evidence.
- **Never read or write `.env*`, `*credential*`, `*secret*`, `*.pem`, `*.key`.**
- **Never edit files outside the target repo**, except this skill's own state.

Repo-specific prohibitions may be added at preflight from `CLAUDE.md` / `AGENTS.md`. They are
additive; they never relax the list above.

## State

Durable, because wakeups and cron die with the session and context gets compacted. Everything
the loop needs to resume lives in `.improve/` in the target repo. Exclude this state from
clean-tree checks. If needed, add it to `.gitignore` only after creating the run branch,
and commit that housekeeping before the first item so it cannot leave a cycle dirty:

    .improve/intake.json    the ten user answers, collected before the timer
    .improve/run.json       deadline, focus, next action, cycle, tier, gates, baseline, guardrails
    .improve/discovery.jsonl completed scans, evidence, empty results, and next scopes
    .improve/supervisor.json daemon-owned start/deadline and terminal status (daemon only)
    .improve/backlog.jsonl  one JSON object per candidate, appended and rewritten in place
    .improve/journal.md     every hourly report, appended
    .improve/final-report.md final handover, with evidence and actual stop reason

Read `run.json`, `backlog.jsonl`, the focus plan, and the latest discovery checkpoint at
the start of **every** cycle. Never carry loop state only in your context - assume you
will be compacted mid-run. See `references/ledger.md` for
the schema and the jq recipes.

**Entry is resumption first.** Before asking the intake again, every invocation checks
for `.improve/run.json` with no `outcome`. If there is one, this is the same run: keep the
deadline and read the clock first. Resume unfinished preflight if `preflight_complete` is
false; otherwise go to the cycle. For legacy state, inspect recorded gates/baseline before
marking preflight complete. Expired runs finalize without repeating setup or asking intake.
Never recompute a deadline from the duration in the prompt when a run is in progress.
If `supervisor.json` exists, its start/deadline are authoritative. Terminal state does not
resume automatically. After resolving a halt, `--resume` explicitly continues with the
original deadline; `--finalize` retries a missing report without reopening work. A new run
gets a new intake and preserves the old ledger in history.
For a legacy live run with no intake, ask the ten questions once without moving its deadline.

## The clock

You cannot tell how much time has passed. You will count cycles, reports, or items and call
the result hours, and you will be wrong by a large factor - the run that wrote "hour 2 of 2"
after seventeen minutes had done exactly that. So nothing in this loop reasons about time.
It asks:

    <skill-directory>/scripts/improve-clock.sh <repo>

Resolve `<skill-directory>` from the SKILL.md you loaded; do not assume a particular install
path. `--json` returns machine-readable timing. It prints now, elapsed, remaining, the
current hour interval, the completed `report_hour` to report, whether a report is due,
and exits `0` while the deadline is in the future and `10` once it has passed. Capture
status explicitly: `0` means continue, `10` means expiry, `2` means
invalid state; do not let an `&&` chain or `set -e` skip finalization on exit `10`. Run it:

- at the start of **every** cycle,
- before writing any report, hourly or final - the report label uses its completed `report_hour`,
- before deciding an item is too large for the time left,
- before deciding whether to re-arm.

**Only exit `10` ends the run on time.** Writing "final", "at the deadline", "N minutes
left" or "hour N of M" without the script's output in front of you is the failure this
section exists to prevent. If the script is unavailable, compute the same numbers with
`date -u` against `run.json` and show the arithmetic; never estimate.

## When the run stops

This list is exhaustive. Anything not on it is a reason to keep working.

1. **The clock says the deadline has passed** (`improve-clock.sh` exits `10`).
2. **The user says stop.**
3. **A halt condition fires:** required verification is unavailable across all eligible
   work after bounded recovery, or an unresolved dirty tree prevents safe continuation.
   Three rejected ideas trigger diagnosis (see Stop-loss), not automatic shutdown.
   A halt is reported as a halt, with the time remaining -
   `HALTED at 22:41Z, 1h24m before the deadline, because ...` - never as a finished run.

Not on the list, and each one has ended a run early before: the backlog is empty; the items
seeded at launch are all done; every lane in the user's scope has been scanned once; the
remaining item looks too large; the hour's report has been written; the work so far feels
like a complete story.

## Preflight - once, at launch

1. Resolve the target repo and confirm it is a git repository. Complete the ten-question
   intake for a new run. Then record `started_at` and an absolute deadline using the system
   clock, **before** baseline checks, so setup consumes the requested work duration. Under
   the daemon copy the timestamps from `supervisor.json` exactly. Persist
   `phase: "preflight"` and `preflight_complete: false` until all setup checks succeed.
2. **Refuse to start on a dirty tree**, excluding `.improve/`. Report what is uncommitted
   and stop. The loop must be able to attribute and undo exactly its own edits.
3. Read `CLAUDE.md` / `AGENTS.md` / `docs/` for conventions, traps, and extra prohibitions.
   A repo that documents its own footguns is telling you what the backlog should avoid.
4. **Discover and record the gates** - the actual commands that prove a change is good in
   this repo. See `references/verification.md`. Write them into `run.json`.
   **Decide the lanes** while you are there. Eight technical lanes are available: `quality`, `coverage`,
   `security`, `performance`, `concurrency`, `resilience`, `gate-speed`, `docs`. Two are on
   only when preflight finds what they need: `accessibility` when there is a UI target
   (an app scheme, a web bundle, a React Native entry point), and `contracts` when the repo
   holds both a server and a client of it, or a client of a server whose schema is in the
   tree (an OpenAPI file, generated types, a shared models package). Use the intake to
   prioritize these lanes and to exclude ones the user ruled out.
   Enable `features`, `ui`, and `assets` when explicitly requested; see the finder briefs
   for their acceptance and verification rules. Record the active list
   and the reason for each conditional one in `run.json`; a lane switched off is stated
   once in the first report and not revisited, except by the dry sweep (see Escalation).
   Record whether each was switched off by the user or by your own judgement - the sweep
   treats them differently.
5. **Establish a green baseline by running them.** If the repo is already red, stop and
   report. You cannot attribute a failure to your change if it was failing before you
   started, and a loop that begins on red will thrash.
6. Create `improve/<YYYY-MM-DD>` (append a numeric suffix if it already exists); record the
   baseline commit SHA. Never reset an existing branch.
7. Complete `run.json` with the original timestamps, focus, gates, and continuation mode;
   set `preflight_complete: true` and `phase: "discover"`.
   Run `improve-clock.sh`; if setup consumed the whole duration, finalize without editing.
8. Report the plan: gates discovered, baseline state, deadline (as the clock prints it).
   Then begin cycle 1.

## The cycle

### 0. Read the clock

Read persisted focus, backlog, and discovery state; inspect any `active_item` left by an
interruption. Run `improve-clock.sh`. Exit `10`: go to the final report. Exit `2` is invalid
state: repair it from durable timestamps or halt, never interpret it as expiry. Otherwise
carry its `remaining` and `report` lines through the cycle.

### 1. Refill, when fewer than 5 items are `ready`

Check the count after every item. Start one refill generation when fewer than five items
are ready **and no refill is already active**. The launch queue is not a discovery pass.
Use the intake priorities and the persisted next scope to give up to four read-only finders
distinct assignments; respect actual agent availability, or run the briefs serially yourself.
See `references/continuation.md` for rotation, deduplication, and checkpoint rules, and
`references/finder-briefs.md` for lane-specific evidence.

Keep fixing queued items while read-only finders run. When the queue is empty, do a bounded
scan yourself or collect the active finders; do not end the run. Record every completed
scan, including zero accepted findings, in `discovery.jsonl`. Do not carry live finders
across daemon processes. Reduce barren supporting lanes' frequency, but do not starve an
explicit user priority. Never launch overlapping refill generations after every item.

When performance is active, read `references/performance.md`: choose the user's journeys,
build missing native measurement harnesses, and profile before proposing speed changes.
Other runs do not need to load that lane's profiling procedure.

Every returned finding must carry **evidence**: a file path, a line, and a concrete failure
scenario, measurement, or gap against a user-requested acceptance criterion. A visual or
feature request must name the observable before/after result, not an agent’s taste.

Dedupe against the whole ledger, including rejected items. Reopen only for changed code or
new evidence, linking the previous item in `reopens`. Never rename a rejected idea to retry it.

### 2. Confirm and rank

Reproduce or trace each claim before accepting it for implementation; inspect upstream
guards and documented behavior that could disprove it. For visual/feature work, compare
against the user’s acceptance checks. Use `references/work-selection.md` when estimating
work or deciding whether a priority is met, blocked, or still active.

Filter by user scope and guardrails first. A confirmed critical vulnerability in scope
preempts lower-risk work. Otherwise rank by the user’s ordered focus priorities, then by

    severity x confidence x blast-radius

within each priority. Ties break toward stronger evidence and a smaller verified diff.
Do not spend a UI-focused run on unrelated cleanup because it is easier to commit.

### 3. Fix - one item at a time, in the main session

Serially, never in parallel. Parallel edits collide, and a shared gate cannot tell you which
of two simultaneous changes broke it. Keep the diff minimal and scoped to the item; if the
fix turns out to need a second unrelated change, that second change is a new backlog item.

Match the surrounding code and design system. Before editing, record `active_item` with
its ID, starting SHA, and owned paths. Clear it only after commit or scoped rollback.

### 4. Verify

Run the relevant gates from `run.json`. For features, UI, and assets, also execute the
intake’s acceptance checks and the before/after checks in the finder briefs.

**A green gate is not proof. Mutate the change and watch the gate fail.** This is required, not
advisory, for executable behavior changes. For docs or asset-only changes, run the
documented command or inspect the rendered result instead of inventing a code mutation.
Record the evidence and why mutation does not apply. Deliberately break the thing you just fixed - reverse the comparison, drop the conjunct,
remove the guard - re-run the gate, and confirm it goes red *in the test that is supposed to
catch it*. Then restore and confirm green again.

If the gate stays green under mutation, **the gate is blind and your change is unverified.** Do
not commit it. Add a regression test that fails on the original or mutated code, restore
the fix, and commit the test and fix together only when green. A standalone test commit
is appropriate only if it passes against the baseline; never commit a known-red test. This is not hypothetical: a query optimisation once
passed an entire suite against a real database while selecting the wrong row, because every
test case had only one row to choose from.

Two cheap ways to fake this and get nothing: running the mutated code with a `-run` filter that
does not actually select the relevant test, and reading a pipeline's exit status where `$?` is
the last command in the pipe rather than the one under test. Both report a confident green over
nothing having run. Check that the test you expect actually executed.

**A test gate must prove that it actually ran the intended tests.** At preflight, record how many tests
each gate reports when it is green on the baseline - that number is the gate's `signal` in
`run.json`. Every later run is compared against it: a run that reports fewer tests than the
baseline, or none, or no output at all, is red, whatever the exit status says. The count
can only go up in this loop, because test changes are additive; a drop means the gate did not
exercise what it exercised an hour ago - a build that skipped a target, a runner that
filtered to nothing, a simulator that never booted. Never reason "it probably would have
passed". Compare counts for the same command, target, and filter; never compare a focused test
with a whole-suite baseline. Builds and linters use recorded artifact/diagnostic signals,
not an invented test count. A documented silent-success tool is not red merely for being quiet.

**The counter-scenario.** After the gate and the mutation check are green, hand the diff and
the backlog item to one read-only subagent with a **fresh context** when subagents are available, with a single question:
*what is the one input or state this change most plausibly still gets wrong?* It returns one
scenario with concrete values, or `none`. It has no write tools and it does not give a verdict -
the maker does not argue with it and the maker does not trust it either. **The scenario is
executed.** Write it as a test (additive, in the repo's idiom) or reproduce it directly, and run
the gate:

- Scenario fails - the counter-scenario found a real hole. Revert the fix, mark the item
  `rejected` with the scenario verbatim, and append the scenario to the item so the next
  attempt starts from it. Do not patch the fix in place; a fix that needed a second try
  under the same item is the two-strike rule's business.
- Scenario passes - keep the test if it is cheap and readable, land it in the same commit,
  and mark the item `done`. The counter-scenario has become coverage.
- `none` - land the item. Record `counter: "none"` on it so the report can show how often the
  second look found nothing, which over a run is a measure of whether it is worth its cost.

If no subagent is available, perform a separate adversarial pass yourself and record that
limitation; do not halt or claim an independent review.

The point is that the check that decides is still the gate. A second opinion delivered as a
verdict can be wrong in either direction; a second opinion delivered as a runnable scenario
can only be wrong by being irrelevant, and an irrelevant one costs a test that passes. For a
`security` item, or any diff that touches auth, sessions, crypto, or the handling of
untrusted input, the question is sharpened to *how would you get past this?*

**A performance item lands only on a clean separation of its journey, measured interleaved.**
Build the before and after versions once each, then run the journey's harness alternately -
before, after, before, after - at least five pairs, so thermal state, caches, and whatever
else the machine is doing land on both sides equally. Record every raw number. Commit only if
the **slowest after-run beats the fastest before-run** - no overlap at all - *and* the gain at
the medians is at least five percent. Overlap is noise, whatever the diff looks like; under five percent is real but
not worth a commit the human has to read, so it becomes `proposed` with its numbers. An item
with no measurement the loop can actually run is `proposed` on its complexity argument and
never applied; the mutation check cannot stand in for a benchmark, because a slower correct
program passes every test. A rejected experiment keeps its numbers in the ledger, which is
what stops the finder proposing it again.

- **Green** - one atomic conventional commit, lowercase after the colon, message explaining
  **why**. Mark the item `done` with the commit SHA.
- **Red** - restore only the item’s owned edits to its starting state and remove only
  untracked files that item created. Never use blanket reset/clean or drop someone’s stash.
  Mark the item `rejected` with
  the verbatim failure text, and move on. Do not attempt a third repair of the same item;
  two failed attempts means the item was misunderstood, and the honest outcome is a rejected
  item with evidence for the human.

**Stop-loss:** three consecutive rejections in one lane trigger a bounded diagnosis:
re-run its unchanged baseline gate, distinguish weak findings from an environment failure,
and rotate to another eligible focus area. Bench a failing lane with a recorded reason and
retry condition; continue independent work whose gates remain usable. Three rejected
hypotheses alone are not evidence that the whole run is broken. Halt only when no eligible
work can be verified after recovery, or a dirty state cannot be safely attributed.

### 5. Report on the hour

"The hour" is a wall-clock hour since `started_at`, as the clock prints it. A cycle whose
clock says `report not due` writes no hourly report, however much it landed - it appends a
one-line entry to `journal.md` at most. When a report is written, set `last_report_hour` in
`run.json` to the hour it covers, so the next cycle's clock knows it is done.

**Lead with landed versus proposed.** An item moved to `proposed` is honest work, but a run can
satisfy every rule in this skill while landing nothing at all - hours of scanning that produce
only recommendations. That is a legitimate outcome for a hardened codebase and a failure mode
for a lazy loop, and the two are indistinguishable unless the ratio is stated. If an hour
produced more proposals than commits, say so in the first line and say why: the items genuinely
needed a human decision, or the loop is avoiding hard work.

See `references/report-format.md`. Write a terminal block,
an append to `.improve/journal.md`, and a notification only if a notification tool is
available and already authorized. Missing notifications never block work.

### 6. Continue, with a persisted next action

Read the clock, increment `cycle` after useful work or discovery, and checkpoint the next
concrete action. While the clock exits `0`, continue according to the mode in
`references/continuation.md`: execute the next cycle immediately in the foreground, verify
an actual scheduler has re-armed, or return to the daemon. **A progress report is not a
final response.** Do not return from foreground work because the current batch is done.

No dry-sweep sleep, no assumed scheduling tool, and no final report before expiry. At exit
`10`, finish or revert the item in hand, record late finder results as unimplemented
candidates, write the final summary, and stop. Report the branch and review commands.

## Escalation - when the backlog runs dry

Do not stop, and do not re-scan the same ground at the same depth. Escalate one tier. Never
skip a tier, and announce every escalation in the hourly report.

- **T1 Defects.** Real bugs, unhandled errors, swallowed exceptions, obvious vulnerabilities,
  incorrect edge-case handling.
- **T2 Coverage.** Uncovered branches and error paths, missing boundary cases, untested
  public surface. Guided by an actual coverage report where the language provides one.
- **T3 Structure.** Coupling hotspots, oversized files, dead code, duplicated logic,
  inconsistencies with the repo's own documented conventions.
- **T4 Refactors.** Deeper structural change. At T4 the diff-size ceiling applies: anything
  above roughly 400 changed lines is written up as a proposal in the report and left for the
  human rather than applied.

**Dry is measured, not felt.** A tier is dry when completed scans have covered the eligible
focus areas at that tier and nothing survives ranking. One empty finder, a failed scan,
or one batch that missed the highest-priority area is not enough. Having worked
through the items you started with is not dry; you have not looked yet.

**Dry at T4 is not the end of the run - change the ground, not the bar.** The quality bar
does not drop: a finding still needs a file, evidence, and a failure or user acceptance gap. What changes is where
the finders look, in this order, one step per dry refill:

1. **The run's own commits.** `git diff <baseline>..HEAD --stat` - every file this run
   touched has new edges, new callers, and tests that now make neighbouring gaps visible.
   Point every finder at those files and their direct callers.
2. **Unscanned ground.** Record each finder's scanned directories in `run.json` under
   `scanned_paths`. Aim the next refill at the largest directories not yet in that list -
   within the highest-priority user scope first, using size only to break ties. A large
   unrelated directory does not outrank a requested screen or journey.
3. **Lanes switched off by preflight's guess rather than by the user.** A lane turned off
   because it "looked irrelevant" (not because the user excluded it, and not because the
   repo lacks what it needs) gets one scan. Say so in the report.
4. **The proposed pile.** Re-read every `proposed` item. One that was deferred only for
   lack of time, or for a gate that has since got faster, may become `ready` again only
   if its measured effort fits the clock. Items needing human decisions stay proposed.

Only when a full pass finds nothing is the run genuinely dry. Report the evidence and
continue active, bounded discovery using a new hypothesis or evidence source. Keep the
quality bar intact. No commit quota and no 20-minute naps; useful investigation counts as
work even when it produces no change. Follow the continuation reference until expiry.

## Cost, and where it goes

Refill is the expensive phase, not fixing. Four parallel finders reading a large codebase cost
far more than the single serial edit that follows. So:

- Refill only when fewer than 5 items are `ready`. A cycle that already has a queue skips it
  entirely and goes straight to fixing.
- Never more than 4 finders at once, whatever the number of active lanes; the rotation above
  decides which four.
- Cap each finder at 8 findings. A finder that returns 30 is padding, and the cost of reading
  30 low-confidence claims exceeds the value of the two real ones inside them.
- Prefer the repo's own index (CodeGraph, ast-grep, an existing coverage report) over a
  general file sweep. `codegraph_impact` answers "what breaks if I change this" in one call;
  deriving the same answer by reading files costs orders of magnitude more.

If actual usage/cost data is available, include it in the first hourly report. Otherwise
say it is unavailable; never invent a burn rate.

**Refill latency dominates short runs.** Measured on a 2,000-file repo: four parallel tier-1
finders took 3.5 to 6.5 minutes to return. In a 30-minute window that is a fifth to a quarter
of the run spent before any fix can start, and the slowest finder (coverage, which has to
build a real coverage profile) can miss the deadline entirely. So:

- For short runs, narrow finders to the user’s top priorities and keep their scopes small.
  Use measured gate times to choose items; do not assume a fixed number of refills or fixes.
- After intake and scope discovery, read-only finders may overlap the baseline gate.
  Baseline results still decide whether edits can begin.
- A finder that returns after the deadline has still done the work. Record its findings for
  the next run rather than discarding them; save completed evidence without implementing it after expiry.

## Boundaries

- **Deadline or stop mid-item:** settle the item in hand, then stop. Under the daemon,
  cleanup is bounded by `IMPROVE_FINISH_GRACE` (120 seconds by default); check the clock
  and `.improve/stop.request` between items and tool calls, and begin rollback promptly
  when verification cannot fit. A forced timeout leaves an explicit recovery halt.
  Never abandon a change half-applied - an unverified working tree is the one state the
  human cannot cheaply reason about.
- **Deadline mid-refill:** collect already completed findings as unimplemented candidates;
  cancel unfinished scans, checkpoint their scope, and finalize. Do not start their fixes.
- **Backlog empty before the deadline:** refill, then escalate, then sweep (see
  Escalation). Never a final report.
- **An item too large for the time left:** read the clock first - the "twenty minutes left"
  that deferred an item once was really an hour and forty-three. If the clock agrees it is
  too large after counting editing, measured verification, and cleanup, take a
  smaller item from the queue, or propose it and go to the next. Either way keep working;
  a large item is never a reason to stop.
- **User sends a message mid-run:** they outrank the loop. Incorporate steering in the saved focus
  and continue with the same deadline unless they stop or replace the run. On stop, cancel
  an actual scheduler if one exists and record `stopped by user`, not deadline completion.
- **Gate becomes unavailable:** diagnose and try bounded recovery, then bench affected
  work and continue independent eligible work. Halt if nothing can be verified. Never
  silently substitute a weaker gate.
- **Context compaction:** expected. Re-read `run.json` and `backlog.jsonl` and continue. This
  is why nothing lives only in your head.

## References

- `references/intake.md` - the ten questions and persistent focus plan
- `references/work-selection.md` - acceptance goals, evidence, scope, and verification budgets
- `references/continuation.md` - execution modes, discovery checkpoints, recovery
- `references/performance.md` - journey harnesses and profiling when performance is active

- `scripts/improve-clock.sh` - elapsed, remaining, hour label, report due, deadline passed
- `references/finder-briefs.md` - the per-lane subagent briefs, by tier
- `references/verification.md` - discovering and recording this repo's gates
- `references/ledger.md` - `.improve/` schema and jq recipes
- `references/report-format.md` - the hourly report

## Running continuously

In an interactive session, use foreground continuation by default. Use a scheduling tool
or `/loop` only when it is actually available and has confirmed re-entry. Save its identity
in the ledger. A session ending without such a mechanism ends the work; do not imply it is
still running.

For an external supervisor, finish the ten-question intake first, then run:

    scripts/improve-daemon.sh --repo ~/Code/thing --for 6h --intake /path/to/intake.json

The runner requires Python 3.9+ and the Claude CLI, uses the skill files beside itself, and
works on macOS/Linux without jq or GNU timeout. Each launch is one cycle with durable
state, a fixed deadline, bounded retries, and a finalization pass. It invokes Claude with
`--permission-mode bypassPermissions`; launch it only within the user-authorized unattended
scope. See `references/continuation.md` and README for restart, stop, and new-run behavior.
