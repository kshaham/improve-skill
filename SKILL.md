---
name: improve
description: Run a time-boxed autonomous improvement loop over a codebase - code quality, test coverage, security, performance, concurrency, resilience, gate speed, docs, and (where the repo has them) accessibility and client/server contracts - refilling its own backlog when it runs dry and reporting every hour. Use when the user asks to continuously improve, harden, or keep working on a repo for a set duration ("improve this repo for 4 hours", "spend the afternoon raising coverage", "keep finding and fixing issues until 6pm").
license: MIT
metadata:
  author: Kamal Shaham, drafted with Claude Code (Opus) in plan mode
  created: 2026-09-05
  origin: Designed to spec in the ~/Code/bonsai session (plan shimmying-roaming-goose.md); daemon added 2026-09-06 after the first 6h run hit ENOSPC
  version: 1.2.0
  changelog: |
    1.2.0 (2026-09-17) - six new lanes (gate-speed, concurrency, resilience, docs always; accessibility, contracts when detected), lane rotation under the finder cap, benchmark-harness-first rule for performance
    1.1.0 (2026-09-17) - gate signal counts, counter-scenario check, interleaved performance measurement, scanner baseline in the security lane, usage-limit backoff in the daemon
---

# Continuous improvement loop

Work a codebase for a stated duration across its lanes - code quality, test coverage,
security, performance, concurrency, resilience, the speed of its own gates, its docs, and
where the repo has them, accessibility and client/server contracts - verifying every change
and reporting every hour. When the obvious work runs out, escalate to harder work rather than
stopping.

    /improve <duration> [path]

`/improve 4h`, `/improve 90m ~/Code/api`. Duration is required; path defaults to the
current working directory.

## What makes this different from ordinary work

You are unattended. Nobody will catch a bad change before it lands, and nobody will notice
if you quietly stop finding real problems and start inventing them. Two obligations follow,
and they outrank throughput:

1. **Every change is proven or reverted.** There is no "looks right". A change whose gate
   did not run is a change that did not happen.
2. **Every report is honest.** A cycle that found nothing says so. Padding an hourly report
   with cosmetic churn to look productive is the defining failure mode of this skill, and it
   is worse than an idle hour because it costs review attention and buries the real work.

## Hard rules

Never, at any tier, for any reason, without stopping to ask:

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
the loop needs to resume lives in `.improve/` in the target repo (add it to `.gitignore` at
preflight if absent):

    .improve/run.json       deadline, cycle count, tier, gates, baseline commit, guardrails
    .improve/backlog.jsonl  one JSON object per candidate, appended and rewritten in place
    .improve/journal.md     every hourly report, appended

Read `run.json` and `backlog.jsonl` at the start of **every** cycle. Never carry loop state
only in your context - assume you will be compacted mid-run. See `references/ledger.md` for
the schema and the jq recipes.

## Preflight - once, at launch

1. Resolve the target repo and confirm it is a git repository.
2. **Refuse to start on a dirty tree.** Report what is uncommitted and stop. The loop's
   safety rests on `git checkout --` being able to undo exactly one item.
3. Read `CLAUDE.md` / `AGENTS.md` / `docs/` for conventions, traps, and extra prohibitions.
   A repo that documents its own footguns is telling you what the backlog should avoid.
4. **Discover and record the gates** - the actual commands that prove a change is good in
   this repo. See `references/verification.md`. Write them into `run.json`.
   **Decide the lanes** while you are there. Eight are always on: `quality`, `coverage`,
   `security`, `performance`, `concurrency`, `resilience`, `gate-speed`, `docs`. Two are on
   only when preflight finds what they need: `accessibility` when there is a UI target
   (an app scheme, a web bundle, a React Native entry point), and `contracts` when the repo
   holds both a server and a client of it, or a client of a server whose schema is in the
   tree (an OpenAPI file, generated types, a shared models package). Record the active list
   and the reason for each conditional one in `run.json`; a lane switched off is stated
   once in the first report and not revisited.
5. **Establish a green baseline by running them.** If the repo is already red, stop and
   report. You cannot attribute a failure to your change if it was failing before you
   started, and a loop that begins on red will thrash.
6. Create `improve/<YYYY-MM-DD>`, record the baseline commit SHA.
7. Compute the deadline from the duration. Write `run.json`.
8. Report the plan: gates discovered, baseline state, deadline. Then begin cycle 1.

## The cycle

### 1. Refill, when fewer than 5 items are `ready`

**Dispatch the finders in the background and keep fixing while they run.** They are read-only,
so they cannot collide with the serial edit in the main session, and blocking on them is the
single largest source of idle time in a run - measured at 3.5 to 6.5 minutes per refill, during
which nothing lands. Launch them, go straight to the top of the current queue, and fold their
findings in when they return. Only wait on a finder if the queue is genuinely empty.

Dispatch **read-only** subagents in parallel, at the current tier, using the briefs in
`references/finder-briefs.md`. They search and report; they never edit.

**Four finders per refill, rotated across the active lanes.** There are more lanes than
finder slots, and that is deliberate: the cap is what bounds the cost of a refill, and the
lanes take turns. `security` has a standing slot every refill. The other three slots go to
the lanes with the longest wait since their last scan, except that a lane that landed
something in the previous refill keeps its slot rather than rotating out. Record `last_scanned`
and `last_yield` per lane in `run.json` so the rotation survives a compaction.

**Stop scanning barren lanes.** A lane whose last two scans produced nothing that survived
ranking drops to every third turn in the rotation; one that keeps landing work keeps its slot.
Scanning every lane at equal depth forever spends the same tokens on the lane that has been
dry since hour one as on the lane doing the work. `docs` starts at every third turn - its
findings are real but rarely urgent - and earns a regular slot only by landing.

**The performance lane is top-down, and it starts by building its own instruments.**
Performance here means what the user feels - how long the app takes to be usable, how long
a screen takes to show its data, whether the main list scrolls without dropping frames, how
long the server takes to answer at the 95th percentile - not how fast a function is. A
function can get ten times faster without anyone noticing; a journey cannot. So the lane
works from the journey down, never from the code up:

1. **Journeys first.** At preflight, name the three to five journeys that matter for this
   repo, from its docs, its main screens, its busiest handlers: *cold launch to first
   interactive frame*, *open the main screen with a warm cache*, *scroll the main list for
   five seconds*, *the top three endpoints under a fixed request mix*. If no harness can
   measure them, the lane's first items are to build one, additive and in the toolchain's
   native form - `XCTApplicationLaunchMetric`, `XCTOSSignpostMetric`, `XCTClockMetric` and a
   scrolling UI test on iOS; `testing.B` over `httptest` handlers plus a query counter per
   request on Go; a load script only if the repo already ships one. Land the harness as its
   own commit, run it for baseline numbers, write them under `journeys` in `run.json`.
2. **Profile, do not guess.** Each refill, the performance finder runs the slowest journey
   under the platform's profiler - `xctrace` Time Profiler and Hangs on iOS, `pprof` CPU and
   allocation profiles on Go, the browser performance trace on web - and reports the
   **largest contributors by share of that journey's time**, with the frame. That is its
   evidence. A finding without a profile share is a smell, not a finding, and goes to
   `quality` if it is anything.
3. **Fix the biggest share, re-measure the journey.** The item's measurement is the
   journey the profile came from, not a micro-benchmark of the function that changed.
   Interleaved, no overlap, five percent floor, as step 4 says. A change that made the
   function faster and the journey no faster is `rejected` with both numbers.
4. **Report the journeys every hour.** `launch 1.84s -> 1.12s (-39%)`, one line per journey,
   baseline to now, in every report. Over a multi-day run this line is the run's result.
   Everything else in the lane is in service of moving it.

What moves a journey is rarely exotic: work on the main thread that belongs off it, a screen
that waits on three requests it could make in parallel or one it could cache, images decoded
at full size for a thumbnail, a view whose body recomputes on every keystroke, a handler that
runs one query per row, a response ten times the size the screen needs. The loop is allowed
all of these. What it is not allowed to do - add an index, change a schema, bump a
dependency, alter the wire format - it proposes with the journey numbers that justify it,
and those proposals are usually the largest remaining wins, so they go at the top of the
report, not the bottom.

Say plainly, in the first report, how far this lane can take the repo. An app that does the
things above will move a lot; an app that already does none of them will not, and the lane
will run dry quickly and say so. Numbers measured on a simulator or a development machine
are relative, not absolute, and the report says which.

Every returned finding must carry **evidence**: a file path, a line, and a concrete failure
scenario or measurement. A finding that cannot say what breaks and when is not a finding, it
is a preference. Drop it.

Dedupe against the whole ledger including `rejected` items, so the loop never re-litigates
something it already dismissed.

### 2. Rank

A confirmed vulnerability preempts everything else. Otherwise one pool, ordered by

    severity x confidence x blast-radius

regardless of which area produced it. A real bug in the quality lane outranks a speculative
optimisation in the performance lane. Ties break toward the smaller diff.

### 3. Fix - one item at a time, in the main session

Serially, never in parallel. Parallel edits collide, and a shared gate cannot tell you which
of two simultaneous changes broke it. Keep the diff minimal and scoped to the item; if the
fix turns out to need a second unrelated change, that second change is a new backlog item.

Match the surrounding code - its naming, its idiom, and its comment density.

### 4. Verify

Run the gates from `run.json` for the half of the codebase you touched.

**A green gate is not proof. Mutate the change and watch the gate fail.** This is required, not
advisory, and it is the step that separates this loop from one that ships plausible-looking
work. Deliberately break the thing you just fixed - reverse the comparison, drop the conjunct,
remove the guard - re-run the gate, and confirm it goes red *in the test that is supposed to
catch it*. Then restore and confirm green again.

If the gate stays green under mutation, **the gate is blind and your change is unverified.** Do
not commit it. Revert, write the test that fails on the mutation, land that test as its own
commit, and only then re-apply the change. This is not hypothetical: a query optimisation once
passed an entire suite against a real database while selecting the wrong row, because every
test case had only one row to choose from.

Two cheap ways to fake this and get nothing: running the mutated code with a `-run` filter that
does not actually select the relevant test, and reading a pipeline's exit status where `$?` is
the last command in the pipe rather than the one under test. Both report a confident green over
nothing having run. Check that the test you expect actually executed.

**A gate proves itself by its count, not its exit code.** At preflight, record how many tests
each gate reports when it is green on the baseline - that number is the gate's `signal` in
`run.json`. Every later run is compared against it: a run that reports fewer tests than the
baseline, or none, or no output at all, is red, whatever the exit status says. The count
can only go up in this loop, because test changes are additive; a drop means the gate did not
exercise what it exercised an hour ago - a build that skipped a target, a runner that
filtered to nothing, a simulator that never booted. Never reason "it probably would have
passed". If the count is unavailable for a gate, say so at preflight, and treat empty output
as red for that gate.

**The counter-scenario.** After the gate and the mutation check are green, hand the diff and
the backlog item to one read-only subagent with a **fresh context** and a single question:
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
- **Red** - `git checkout -- <files>` (or `git stash drop`), mark the item `rejected` with
  the verbatim failure text, and move on. Do not attempt a third repair of the same item;
  two failed attempts means the item was misunderstood, and the honest outcome is a rejected
  item with evidence for the human.

**Stop-loss:** three consecutive rejections in one area bench that area - for one hour, or for
the rest of the run if less than an hour remains. A run of six hours should not lose a whole
lane to three bad findings in its first twenty minutes; an area comes back at the next tier or
after the next refill, whichever is sooner. Say so in the report, both when it is benched and
when it returns. Three consecutive rejections across *all* areas halts the loop -
that pattern means the baseline moved or your gate is lying, and continuing burns tokens
producing nothing.

### 5. Report on the hour

**Lead with landed versus proposed.** An item moved to `proposed` is honest work, but a run can
satisfy every rule in this skill while landing nothing at all - hours of scanning that produce
only recommendations. That is a legitimate outcome for a hardened codebase and a failure mode
for a lazy loop, and the two are indistinguishable unless the ratio is stated. If an hour
produced more proposals than commits, say so in the first line and say why: the items genuinely
needed a human decision, or the loop is avoiding hard work.

See `references/report-format.md`. Three deliveries of the same content: terminal block,
`PushNotification` headline, and an append to `.improve/journal.md`.

### 6. Re-arm

If now is before the deadline, call `ScheduleWakeup` with `prompt` set to the original
`/improve <duration> [path]` invocation verbatim, so the next firing re-enters here, and
`noop: false` if anything landed (`true` if the cycle was genuinely quiet).

`delaySeconds` is the minimum the runtime allows (60) whenever items are `ready` - the queue is
full and every second of delay is a second not spent fixing. Back off to 300 or more only when
the backlog is dry and a refill is in flight, since there is nothing to do until it lands.

At the deadline: final summary, stop, and **do not** re-arm. Tell the user the branch name,
the commit count, and how to review or discard it.

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

Genuinely dry at T4 means the run has done its job. Say that plainly and idle - do not
manufacture work.

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

Tell the user the burn rate in the first hourly report so they can decide whether to let a
long run continue.

**Refill latency dominates short runs.** Measured on a 2,000-file repo: four parallel tier-1
finders took 3.5 to 6.5 minutes to return. In a 30-minute window that is a fifth to a quarter
of the run spent before any fix can start, and the slowest finder (coverage, which has to
build a real coverage profile) can miss the deadline entirely. So:

- Below about an hour, expect one refill and one or two fixes. Say so at launch rather than
  implying more.
- Start the finders in the same message as preflight - the baseline gate and the scan are
  independent, and serialising them wastes the scan's latency.
- A finder that returns after the deadline has still done the work. Record its findings for
  the next run rather than discarding them; the discard rule exists to stop you writing
  half-formed state, not to throw away completed analysis.

## Boundaries

- **Deadline mid-item:** finish and verify the item in hand, commit or revert it, then stop.
  Never abandon a change half-applied - an unverified working tree is the one state the
  human cannot cheaply reason about.
- **Deadline mid-refill:** discard the findings, write nothing, stop.
- **User sends a message mid-run:** they outrank the loop. Answer them, do what they ask, and
  only then decide whether to resume. If they say stop, call `ScheduleWakeup` with
  `stop: true` and give the final report.
- **Gate becomes unavailable** (simulator gone, Docker down, toolchain missing): halt, report
  what broke, do not fall back to a weaker gate. A silently-downgraded gate turns every
  subsequent green into a lie.
- **Context compaction:** expected. Re-read `run.json` and `backlog.jsonl` and continue. This
  is why nothing lives only in your head.

## References

- `references/finder-briefs.md` - the per-lane subagent briefs, by tier
- `references/verification.md` - discovering and recording this repo's gates
- `references/ledger.md` - `.improve/` schema and jq recipes
- `references/report-format.md` - the hourly report

## Launching under /loop

`/improve` self-paces via `ScheduleWakeup`. If that is unavailable in the current session,
the equivalent is `/loop /improve <duration> [path]`, which puts the loop skill in charge of
re-entry. Either way the state in `.improve/` is what actually carries the run.

## Launching under the daemon

`scripts/improve-daemon.sh` runs the loop from OUTSIDE any session:

    scripts/improve-daemon.sh --repo ~/Code/thing --for 6h

It exists because a wakeup dies with the session that armed it. Close the terminal, sleep the
laptop, lose the process, and the run stops silently mid-item with a ledger that still says
"running" - which on a six-hour run is most of the run.

**Under the daemon you are one cycle, not the loop.** The prompt says so, and the difference
is small but total:

- **Do not call `ScheduleWakeup`.** It would die with this session anyway, and the daemon is
  what re-enters. Finish the cycle and stop.
- **A run already in progress keeps the deadline in `run.json`.** The duration in the prompt
  is what REMAINS, and it is there for the case where no run exists yet. Do not recompute a
  deadline from `now` when `run.json` already has one - that is how an externally paced run
  silently becomes an unbounded one.
- **Nothing carries over except `.improve/`.** The next cycle starts with no memory of this
  one and does not see what you printed. This is the arrangement the state files were written
  for; it is the normal case here rather than recovery from compaction.
- **Past the deadline**, write the final summary, set `outcome` in `run.json`, and stop
  without starting new work. The daemon stops on its own clock too, but the ledger is what a
  human reads.

The daemon owns when a cycle starts, when to stop, and what happens when a cycle dies - a
per-cycle timeout, and a breaker that gives up after three consecutive failures so a repo
whose gate cannot run costs three launches rather than a night of them. An account usage
limit is neither a failure nor a success: it is the one thing a relaunch provably cannot
fix, so it is kept out of the breaker entirely, backed off on doubling (15m, 30m, 1h, 1h...),
and written into `journal.md` as a gap the human can see, so a six-hour run that spent two
of them waiting on the account says so in the same place as its results. It leaves the working
tree alone: `--stop` lets the cycle in flight finish, because killing an agent mid-commit
produces the one state a human cannot cheaply reason about.
