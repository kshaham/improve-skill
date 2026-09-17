# /improve

A time-boxed, unattended improvement loop for a codebase. Give it a repo and a duration; it
finds work across ten lanes - from correctness and coverage to what the user actually feels
when the app launches - fixes one item at a time, proves every change before committing it,
refills its own backlog when the queue runs dry, reports every hour, and stops at the
deadline with a branch for you to review.

    /improve 4h
    /improve 90m ~/Code/api
    scripts/improve-daemon.sh --repo ~/Code/api --for 6h     # survives a closed terminal

It is a Claude Code skill. `SKILL.md` is the contract the agent follows; this file explains
what that contract gives you and why it is shaped the way it is.

## Contents

- [What it does](#what-it-does)
- [What it will never do](#what-it-will-never-do)
- [How a run unfolds](#how-a-run-unfolds)
- [How a change is proven](#how-a-change-is-proven)
- [How the backlog is found and ranked](#how-the-backlog-is-found-and-ranked)
- [Escalation when work runs out](#escalation-when-work-runs-out)
- [Reports](#reports)
- [State on disk](#state-on-disk)
- [Three ways to run it](#three-ways-to-run-it)
- [The daemon](#the-daemon)
- [Cost](#cost)
- [Reviewing a finished run](#reviewing-a-finished-run)
- [Configuration](#configuration)
- [/improve-max - the courageous variant](#improve-max---the-courageous-variant)
- [Files](#files)
- [Design notes](#design-notes)

## What it does

The loop works ten lanes. Eight are always on; two switch on when preflight finds what they
need.

| Lane | Looks for | Proven by |
|---|---|---|
| **quality** | swallowed errors, unchecked results, missing guards the surrounding code uses, off-by-ones, leaks, duplicated logic that has drifted, dead code | the gate, mutation, counter-scenario |
| **coverage** | uncovered branches on paths that can fail in production, untested error paths and boundaries, public surface with no test | the new test failing on the mutation |
| **security** | injection reachable from untrusted input, missing authorization, secrets in source or logs, unsafe deserialization, weak hashing, trust extended to a caller | the gate, an adversarial counter-scenario, and an hourly scanner re-run |
| **performance** | what the user feels: cold launch, time to a usable screen, scroll smoothness, p95 of the busiest endpoints - worked top-down from a profile of the slowest journey | the journey's own harness, interleaved, no overlap |
| **concurrency** | shared state without a lock or actor, check-then-act, UI mutation off the main actor, continuations resumed twice, locks held across an await | the race detector or strict-concurrency diagnostics |
| **resilience** | calls with no timeout, retries with no backoff, non-idempotent retried writes, ignored cancellation, errors logged without the id needed to find them | a fault-injection test |
| **gate-speed** | the repo's own tests and builds: missing caches, serial suites, real sleeps, fixtures rebuilt per test | the gate itself, re-timed, with its test count unchanged |
| **docs** | README commands that no longer run, setup steps that no longer match the tree, comments that contradict the code beneath them | running the documented command |
| **accessibility** *(UI targets only)* | controls with no label, images with no description, custom controls with no role, text that does not scale, contrast and touch-target violations | the platform accessibility audit in the UI test |
| **contracts** *(server + client only)* | a field named differently on the two sides, optional-vs-required drift, an enum value the client cannot handle, an error status mapped to "unknown" | a round-trip test of the real response through the real decoder |

Everything it lands is a small, atomic, conventional commit on a dated branch
(`improve/YYYY-MM-DD`), each one explaining why. Nothing touches `main`, nothing is pushed.

## What it will never do

These hold at every tier, for the whole run, without exception. The agent stops and asks
rather than crossing any of them.

- Touch `main`, push, force-push, or open a pull request. Landing is local commits only.
- Delete a test, weaken an assertion, or add a skip to get green. Test changes are additive.
  A test that encodes wrong behaviour becomes a backlog item for you, not a fix.
- Bump a dependency: no modules, packages, or toolchains.
- Add a migration or change a schema.
- Rewrite anything that pays out, charges, or stamps a row exactly once. Money-adjacent and
  once-only invariants are read-only; findings there go to the backlog with evidence.
- Read or write `.env*`, `*credential*`, `*secret*`, `*.pem`, `*.key`.
- Edit files outside the target repo, other than its own state under `.improve/`.
- Start on a dirty working tree, or on a red baseline.
- Install tooling. It uses whatever scanners and toolchains the machine already has.

A repo's `CLAUDE.md` or `AGENTS.md` can add prohibitions. Nothing can remove one.

## How a run unfolds

**Preflight, once.** Confirm the target is a clean git repo. Read `CLAUDE.md`, `AGENTS.md`
and `docs/` for conventions and traps. Discover the gates (the commands that prove a change
is good in this repo - its own scripts first, then Makefile targets, then CI, then the
toolchain default), time them, and record them with their baseline test counts. Decide the
lanes: `accessibility` turns on if there is a UI target, `contracts` if the tree holds a
server and a client of it. Find the detectors each lane needs - the race detector, the
accessibility audit, the journey harnesses - and record what exists and what is absent. Run
whatever security scanners are installed and seed the backlog from their output. Run every
gate and detector once to establish a green baseline; if the repo is already red, stop and
say so. Create the branch, compute the deadline, write `run.json`, report the plan.

**Then, every cycle:**

1. **Refill** when fewer than five items are ready. Four read-only finder subagents are
   dispatched in the background at the current tier, and the loop keeps fixing while they
   run. Security holds a standing slot; the other three rotate across the active lanes by
   longest wait, with a lane that just landed something keeping its place. Lanes that keep
   coming back empty drop to every third turn.
2. **Rank** the whole pool by severity x confidence x blast radius. A confirmed
   vulnerability preempts everything. Ties go to the smaller diff.
3. **Fix** the top item, alone, in the main session. Serial by design: one gate run maps to
   exactly one change, so a red gate is never ambiguous.
4. **Verify** - see below. This is most of the skill.
5. **Report** on the hour.
6. **Re-arm** for the next cycle, or, at the deadline, write the final summary and stop.

## How a change is proven

Every landed commit has passed all of these. A change that fails any one is reverted with
`git checkout --`, marked `rejected` in the ledger with the verbatim failure, and not
retried more than once.

**The gate.** The repo's own test and check commands, discovered at preflight and never
re-derived mid-run. A fast gate runs on every change; a batch gate runs once an hour.

**Gate signal.** At preflight the loop records how many tests each gate reports when green.
Every later run must report at least that many. Fewer, none, or empty output is red
regardless of exit status - in this loop test counts can only go up, so a drop means the
gate did not exercise what it exercised an hour ago.

**Mutation.** A green gate is not proof. The loop deliberately breaks the thing it just
fixed - reverses the comparison, drops the guard - re-runs the gate, and confirms it goes
red in the test that is supposed to catch it, then restores and confirms green. If the gate
stays green under mutation, the gate is blind: the change is reverted, the test that would
have caught the mutation is written and landed on its own, and only then is the fix
re-applied.

**Counter-scenario.** A fresh-context, read-only subagent is given the diff and the item
and asked one question: what is the one input or state this change most plausibly still
gets wrong? It answers with concrete values or `none`. It gives no verdict, and the loop
neither argues with it nor trusts it - the scenario is written as an additive test and
run. If it fails, the fix is reverted and the scenario stays on the item for the next
attempt. If it passes, the test lands with the fix as coverage. For security-adjacent diffs
the question becomes "how would you get past this?"

**Measurement, for performance and gate-speed items.** The measurement is the *journey*
the finding came from - cold launch, the feed opening, the p95 of the request mix - never a
micro-benchmark of the function that changed. Before and after are built once each and the
journey's harness is run alternately, at least five pairs, so thermal state and caches fall
on both sides. The change lands only if the slowest after-run beats the fastest before-run
and the median gain is at least five percent. Overlap is noise; under five percent becomes a
`proposed` item with its numbers; a function that got faster while its journey did not is
rejected with both numbers. All raw runs go in the ledger, and a landed item's after-runs
become the journey's new current value.

**Scanner self-check, hourly.** The scanners that seeded the backlog are re-run before each
report. A count that rose means one of this hour's commits introduced something a scanner
can see; the loop bisects the hour's SHAs, reverts it, and leads the report with it.

Two-strike rule: a second failed attempt on the same item ends it. Three consecutive
rejections in one lane bench that lane for an hour; three consecutive rejections across all
lanes halt the run, because that pattern means the baseline moved or a gate is lying.

## How the backlog is found and ranked

Finders are read-only subagents with per-lane briefs (`references/finder-briefs.md`). Each
returns at most eight findings, and every finding must carry a file, a line, a claim, the
evidence actually read, and a concrete failure scenario: specific inputs leading to a
specific wrong outcome. Anything that cannot say what breaks and when is dropped as a
preference, not a finding.

Finders are told what is already in the ledger, including rejected items, so nothing is
re-litigated. They are told not to report what the linter would catch, and not to
re-propose anything a comment or `CLAUDE.md` documents as a deliberate decision.

The security lane starts from the scanner baseline taken at preflight: secrets are ready
at top severity with the scanner's line as evidence, SAST hits are ready only once the
finder has traced them to untrusted input, and dependency advisories are always `proposed`.
The finder's own brief is the space scanners cannot reach - authorization decided in the
wrong place, business rules that can be walked around, trust in an "internal" caller.

Ranking is one pool across lanes: severity x confidence x blast radius. A real bug in the
quality lane outranks a speculative optimisation in the performance lane.

## The performance lane, specifically

This is the lane most people mean when they ask whether a long run will make the app
faster, so it is worth being precise about what it does and what it can promise.

It works from the user's experience down, never from the code up. At preflight it names the
three to five journeys that matter - cold launch to first interactive frame, the main screen
with a warm cache, five seconds of scrolling the main list, the top endpoints under a fixed
request mix - and if nothing in the repo can measure them, its first commits build the
harness: `XCTApplicationLaunchMetric` and signpost metrics and a scrolling UI test on iOS,
`testing.B` over `httptest` handlers with a per-request query counter on Go. Each refill,
the finder runs the slowest journey under the platform profiler (`xctrace`, `pprof`, the
browser trace) and reports the largest contributors by share of that journey's time. The
loop fixes the biggest share, re-measures the journey, and reports every journey's baseline
and current number in every hourly report. On a multi-day run that line is the result.

What moves a journey is rarely exotic, and the loop may do all of it: main-thread work that
belongs off it, a screen waiting on three requests it could make in parallel or serve from
a cache it already has, thumbnails decoded at full size, a view body recomputing on every
keystroke, a handler running one query per row, a response ten times larger than the screen
consumes. What it may not do - add an index, change a schema, bump a dependency, alter the
wire format - it proposes with the journey numbers that justify it, at the top of the report,
because those are usually the largest remaining wins.

What it cannot promise: a multiple. An app that does several of the things above will move
a long way; an app that already does none of them will not, and the lane will run dry and
say so early. Numbers from a simulator or a development machine are relative, not what a
user on a device will see, and the report says which it measured.

## Escalation when work runs out

The loop does not stop when a tier is dry and it does not re-scan the same ground at the
same depth. It escalates one tier at a time, announcing each escalation in the report:

| Tier | Scope |
|---|---|
| **T1 Defects** | real bugs, unhandled errors, obvious vulnerabilities, wrong edge cases |
| **T2 Coverage** | uncovered branches and error paths, guided by a real coverage report |
| **T3 Structure** | coupling hotspots, oversized files, dead code, drift from documented conventions |
| **T4 Refactors** | deeper structural change, with a ~400-line diff ceiling above which the work is written up as a proposal instead of applied |

Genuinely dry at T4 means the run has done its job. It says so and idles; it does not
manufacture work.

## Reports

Every hour, the same content three ways: a terminal block, a one-line push notification,
and an append to `.improve/journal.md`.

    IMPROVE - hour 3 of 4 - improve/2026-09-05 - tier 2
    ------------------------------------------------------------
    Landed this hour (4)
      a1b2c3d  fix(quest): the lapse sweep no longer swallows a failed UPDATE
      ...
    Rejected (2)
      q-0011  perf: batch the approval query
              gate red: TestApprovalOrdering - expected 3 rows, got 0
      p-0007  perf: cache the tier lookup per request
              counter-scenario failed: two members of one household ...
    Proposed, needs you (1)
      ...
    Benched
      performance - 3 consecutive gate failures
    Totals   quality 7/2  coverage 5/1  security 1/0  performance 0/3   backlog 6 ready

The rules that keep it honest: the first line states landed versus proposed, and if an hour
produced more proposals than commits it says why. A cycle that found nothing says so.
Rejections quote the gate's actual output, name what killed the item (gate, counter-scenario
or measurement), and are as prominent as successes. Nothing is reported as landed without a
commit SHA. Time the daemon spent waiting on an account limit is its own journal entry, not
folded into an hour's totals.

Every report carries a `Journeys` line - baseline to now for every measured journey, even
when nothing moved - and a `Lanes` line saying which lanes are off and why, and which
active ones were not scanned this hour.

The first report of a run also states the burn rate, which scanners and detectors were
found and which were absent, how far the performance lane can plausibly take this repo,
and - for runs under about an hour - that one refill and one or two fixes is what to
expect, because refill latency dominates short runs.

## State on disk

Everything the loop needs to resume lives in `.improve/` inside the target repo
(gitignored at preflight):

    .improve/run.json       deadline, cycle, tier, gates + signals, scanners + baselines,
                            active lanes + rotation state, journeys + baseline/current runs,
                            detectors, baseline commit, benched lanes, guardrails, counts
    .improve/backlog.jsonl  one JSON object per candidate: lane, tier, severity, confidence,
                            blast, file, line, claim, evidence, failure scenario, status,
                            commit, note, counter-scenario, measurement
    .improve/journal.md     every hourly report and every daemon wait, appended

State is re-read at the start of every cycle and never carried only in the agent's context.
This is what makes context compaction, session death and daemon relaunches survivable: a
fresh cycle with no memory picks up exactly where the last one stopped. See
`references/ledger.md` for the schema and jq recipes.

## Three ways to run it

**In a session, self-paced.** `/improve 4h`. The skill re-arms itself between cycles with a
wakeup. Simplest, and the wakeup dies with the session: close the terminal or sleep the
laptop and the run stops.

**Under `/loop`.** `/loop /improve 4h`. Same thing with Claude Code's loop skill in charge of
re-entry, for sessions where self-pacing is unavailable.

**Under the daemon.** `scripts/improve-daemon.sh --repo ~/Code/thing --for 6h`. Runs from
outside any session, one fresh `claude -p` per cycle, and survives everything the other two
do not. This is the one to use for anything longer than an hour or two.

## The daemon

The daemon owns when a cycle starts, when the run stops, and what happens when a cycle
dies. The skill owns everything inside a cycle. Each cycle starts cold and is told it is one
cycle of an externally paced loop: do not re-arm, honour the deadline already in `run.json`,
leave everything for the next cycle in `.improve/`.

    improve-daemon.sh --repo ~/Code/bonsai --for 6h
    improve-daemon.sh --repo ~/Code/bonsai --for 90m --dry-run
    improve-daemon.sh --status --repo ~/Code/bonsai
    improve-daemon.sh --stop   --repo ~/Code/bonsai
    nohup improve-daemon.sh --repo ~/Code/bonsai --for 6h >/dev/null 2>&1 &

What it guards against:

- **A wedged cycle.** Per-cycle timeout (default 1h) so a process waiting on a simulator that
  will never boot cannot pin the run.
- **A crash loop.** A breaker that gives up after three consecutive failures or too-fast
  exits, so a repo whose gate cannot run costs three launches rather than a night of them.
- **The account usage limit.** Kept out of the breaker entirely, because a relaunch provably
  cannot fix it. The daemon backs off on doubling (15m, 30m, 1h, 1h...) up to a four-hour
  budget, writes each wait into `journal.md` with the real state of the working tree, and
  stops early if the next wait would pass the deadline. The match is narrow and applies only
  to non-zero exits, so a finding that mentions "rate limiting" cannot put the daemon to
  sleep.
- **A dirty tree between cycles.** Refused at launch by the daemon itself, before a session
  is paid for; logged after any cycle that leaves one.
- **Killing an agent mid-commit.** `--stop` lets the cycle in flight finish. An unverified
  working tree is the one state a human cannot cheaply reason about.

It needs `--permission-mode bypassPermissions` to commit without a human at the keyboard.
The safety comes from the skill's hard rules and the bounds above, not from prompts. Read
the "Hard rules" section of `SKILL.md` before running it.

## Cost

Refill is the expensive phase, not fixing: four parallel finders reading a large codebase
cost far more than the single serial edit that follows. So the loop refills only when fewer
than five items are ready, runs at most four finders at once, caps each at eight findings,
throttles lanes that keep coming back empty, and prefers the repo's own index (CodeGraph,
ast-grep, an existing coverage report) over a general file sweep. The counter-scenario adds
one short subagent per landed item; the report tracks how often it returns `none` so you
can judge whether it is earning its cost on this repo.

Measured on a 2,000-file repo, four tier-1 finders took 3.5 to 6.5 minutes to return. The
loop dispatches them in the background and keeps fixing, but in a 30-minute run that is
still a fifth of the window before the first fix can land.

## Reviewing a finished run

The final report gives you the branch, the commit count, every `proposed` item in full, and
the exact commands:

    git log --oneline main..improve/2026-09-05
    git diff main..improve/2026-09-05
    git branch -D improve/2026-09-05     # discard everything

Read `journal.md` for the hour-by-hour story, including anything benched, escalated or
waited on. The `proposed` items are the ones that needed a human: schema changes,
dependency bumps, refactors over the diff ceiling, a test that encodes wrong behaviour, a
performance gain too small to be worth a commit.

## Configuration

Daemon environment variables, all optional:

| Variable | Default | Meaning |
|---|---|---|
| `IMPROVE_CYCLE_TIMEOUT` | `3600` | seconds before a cycle is killed |
| `IMPROVE_CYCLE_GAP` | `20` | seconds between cycles |
| `IMPROVE_TOO_FAST` | `25` | a cycle exiting faster than this counts as a failure |
| `IMPROVE_MAX_FAILURES` | `3` | consecutive failures before the daemon gives up |
| `IMPROVE_LIMIT_WAIT` | `900` | first wait after an account usage limit |
| `IMPROVE_LIMIT_WAIT_CAP` | `3600` | ceiling for the doubling wait |
| `IMPROVE_LIMIT_WAIT_BUDGET` | `14400` | total seconds the daemon will wait on the account |
| `IMPROVE_MODEL` | unset | model passed to each cycle (`--model` overrides) |

Everything inside a cycle is configured by the repo itself: its gates, its scanners, its
`CLAUDE.md` prohibitions, its coverage tooling.

## /improve-max - the courageous variant

`/improve` is built for hours and never changes what the system *is*. `/improve-max` is
built for days and may: rewrite a subsystem in a faster language or runtime, replace a
framework, add an index, run a migration, change a dependency, alter the wire format. Its
target is the same journey table, moved by a multiple rather than a percentage, and the
system it hands back may be written differently from the one it was given.

    /improve-max 3d ~/Code/api
    /improve-max 4d ~/Code/api --target cold-launch=10x,api-p95=3x --kinds design,data
    scripts/improve-daemon.sh --skill improve-max --repo ~/Code/api --for 3d --args "--target 5x"

The goal is a parameter, because most codebases will not give up a multiple on every
journey and the loop should be measured against what you asked for, not a slogan:

| Parameter | Default | Sets |
|---|---|---|
| `--target` | `3x` | the goal per journey, one value for all or `journey=Nx,...` |
| `--spike` | `max(1.5x, target/3)` | the gain a spike must show before its bet is placed |
| `--kinds` | all | which kinds of bet may be placed: `stack`, `framework`, `design`, `data`, `wire`, `dependency`. `--kinds design,data` keeps your language. |
| `--stop-at-target` | off | stop when every targeted journey is there, instead of running `/improve`'s cycle for the remaining time |

Every report shows the target beside the current number, the opening report says per
journey whether the target is plausible and what it would take, and a target the profile
says is unreachable is stated rather than silently lowered.

It lives at `max/` in this repo and is installed as a symlink
(`~/.claude/skills/improve-max -> improve/max`). It inherits everything from `/improve`
it does not explicitly change, and changes this:

**What it may do that `/improve` may not.** Dependency changes, migrations with a tested
down path, schema changes, moving money-adjacent invariants, retiring tests, and landed
diffs of any size. Each is allowed under a condition rather than unconditionally - the
condition is always a proof the loop gathered first.

**What still holds.** Never `main`, never push, never a PR, never secrets, never outside
the repo, never a live environment. And one rule that is new and absolute: **never retire a
test without a successor** - an original test goes only when a corpus case or new test
provably catches the same mutation, and the mapping is written down.

**How a big bet is placed.** Every bet follows the same four moves, and the moves are what
make courage survivable unattended:

1. **Pin.** Record a characterization corpus for the subsystem - real inputs and the outputs
   the current code produces, including the error cases and every edge its comments and
   git history mention - and prove the replay goes red under mutation. Nothing is touched
   until "the same" is a test.
2. **Spike.** A throwaway prototype of the hottest path in the proposed stack or design,
   four hours or a thousand lines at most, never merged. The journey is measured with it in
   place. The bet is placed only if the spike clears the run's spike threshold (a third of
   the target by default), or half of what the bet claimed, whichever is larger. Otherwise it is killed with its numbers and never
   re-spiked from the same hypothesis. A killed spike cost four hours and saved three days.
3. **Strangle.** The new implementation lands piece by piece behind the old interface. After
   every piece the whole system builds, every gate is green with its test count intact, the
   corpus passes against the new piece, and old and new have been run in **shadow** over
   the full corpus with zero unexplained differences. The system is never more than one
   commit from working, which is what lets a three-day bet survive compaction, relaunch or
   a stop at any hour.
4. **Land or kill.** A bet lands when every piece has crossed, the old path is gone by the
   successor rule, and the journey on the run branch has moved by at least what the spike
   promised. It is killed - run branch reverted to the tag taken at placement, bet branch
   kept for you - when its clock runs out, two consecutive pieces fail, or a second shadow
   difference cannot be explained.

**Choosing a stack.** Changing language is a tool, not the goal. Where plausible the loop
spikes the redesign in the existing language first; if that alone clears the threshold,
that is the bet. When the stack is the bet, it prefers what the repo's ecosystem already
touches and what the toolchain can gate as strictly, and the daily report says what you
will need to know to maintain the result.

**Reporting.** The hourly report gains a `Bet` line; a daily report adds the journey table
baseline-to-now, every bet placed, landed or killed with numbers, and what the system is
now written in and how much of it. The opening report says, from the profile, what the
target would take and where the ceiling is - a journey that is I/O-bound on a remote
service does not get a multiple from any language, and the loop says so before the first
spike.

See `max/SKILL.md`, `max/references/bets.md` and `max/references/characterization.md`.

## Files

    SKILL.md                        the contract the agent follows
    README.md                       this file
    references/verification.md      discovering gates, signals and scanners
    references/finder-briefs.md     per-lane, per-tier briefs for the read-only finders
    references/ledger.md            .improve/ schema and jq recipes
    references/report-format.md     the hourly and final report
    scripts/improve-daemon.sh       the out-of-session runner (--skill improve | improve-max, --args for skill parameters)
    max/SKILL.md                   the /improve-max contract - what it may do beyond /improve, and the proof each needs
    max/references/bets.md         the bet schema, spike protocol, kill criteria
    max/references/characterization.md   recording a corpus, shadow comparison, retiring tests by successor
    LICENSE                         MIT

## License

MIT. See `LICENSE`.

## Design notes

**Why performance is measured at the journey, not the function.** A function can get ten
times faster without anyone noticing; a journey cannot. Measuring the journey is what
stops the lane collecting micro-wins the user never feels, and it is what makes the hourly
`Journeys` line an honest statement of what a long run achieved.

**Why the loop is serial.** Parallel edits collide, and a shared gate cannot say which of two
simultaneous changes broke it. One change per gate run is what makes every red attributable
and every revert exactly one item.

**Why verification is most of the skill.** An unattended loop's defining failure is not
doing nothing; it is quietly producing plausible-looking work. Every rule in the verify step
exists because a specific shortcut once produced a confident green over nothing having run:
a `-run` filter that selected no test, a pipeline whose `$?` was `tee`'s, a query
optimisation that passed a whole suite against a real database while selecting the wrong
row because every test had one row to choose from.

**Why the second opinion is a scenario, not a verdict.** A reviewer that says "reject" can
be wrong; a reviewer that says "try two members of one household" can only be irrelevant,
and an irrelevant scenario costs a passing test. The gate stays the only thing that decides.

**Why performance is measured interleaved with no overlap allowed.** Three-before-then-
three-after lets a thermal ramp or a warmed cache masquerade as a gain. Alternating puts the
noise on both sides; requiring the slowest after to beat the fastest before means the
change has to clear the noise entirely rather than a statistic of it.

**Why scanners are a baseline and an hourly gate rather than a per-refill step.** A
scanner's answer does not change until the code does, so re-running it every refill only
costs time. Running it hourly against the loop's own commits turns it into the cheapest
regression check the loop has.

**Why a usage limit is not a failure.** The breaker exists to stop a relaunch loop that is
discovering the same broken gate at token cost. A rate limit is the one condition a relaunch
cannot fix and costs nothing to wait out, so counting it would end good runs at 2am with a
log that blamed the repo.

**Why state lives on disk.** Wakeups, cron and context all die with the session. A loop
that keeps its state in its head cannot be resumed, compacted or handed to a daemon. Under
the daemon, "no memory of the previous cycle" is the normal case, not recovery.
