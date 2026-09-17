---
name: improve-max
description: Run a multi-day autonomous transformation loop over a codebase that places large, measured bets - rewriting a subsystem in a faster language or runtime, replacing a framework, reshaping the data model, changing the wire format - to make the user-facing journeys many times faster, while keeping the system shippable at every commit and its behaviour pinned by a characterization corpus. Use when the user asks for a codebase to be made dramatically faster over days ("run improve-max for 3 days", "rewrite whatever it takes to make this 10x faster", "be courageous with this repo over the weekend"). For hourly-scale, low-risk improvement use /improve instead.
license: MIT
metadata:
  author: Kamal Shaham, drafted with Claude Code (Opus)
  created: 2026-09-17
  parent: improve
  version: 0.2.0
  install: symlink ~/.claude/skills/improve-max -> improve/max (this directory lives inside the improve skill's repo)
---

# The courageous loop

Work a codebase for days, not hours, toward one number: the user-facing journeys, measured
at preflight, made many times faster. To get there this loop may do what `/improve` may not -
rewrite a subsystem in another language, replace a framework, add an index, run a
migration, change a dependency, alter the wire format. The system it hands back may be
written differently from the one it was given. It must behave the same, and it must be
faster by a margin that was measured before the work was committed to, not hoped for.

    /improve-max <duration> [path] [--target <Nx | journey=Nx,...>] [--spike <Nx>]
                              [--kinds stack,framework,design,data,wire,dependency]
                              [--stop-at-target]

`/improve-max 3d`, `/improve-max 48h ~/Code/api --target 5x`,
`/improve-max 4d --target cold-launch=10x,api-p95=3x --kinds design,data`. Duration is
required and should be at least a day; below that, use `/improve`, which is built for hours.

## Parameters

Every parameter is written into `run.json` at preflight and read from there afterwards, so
a daemon relaunch or a compaction cannot change the terms of the run.

| Parameter | Default | What it sets |
|---|---|---|
| `--target` | `3x` | The goal, as a multiple of the baseline journey time. One value applies to every journey; `journey=Nx,...` sets each, and a journey not named has no target and is worked only when nothing targeted has a placeable bet. The target is what the opening report is measured against and what "done" means. |
| `--spike` | `max(1.5x, target/3)` | The gain a spike must show for its bet to be placed. Derived from the target so a modest goal does not demand a heroic spike: `--target 3x` asks a spike for 1.5x, `--target 10x` asks for 3.3x. Set it explicitly to override. Half of the bet's own claimed gain is always also required. |
| `--kinds` | all six | Which kinds of bet may be placed: `stack` (language or runtime), `framework`, `design` (same stack, new shape), `data` (schema, index, migration), `wire` (protocol or payload), `dependency`. `--kinds design,data` gets a faster system in the language you have. A bet of an excluded kind is `proposed` with its spike numbers if the profile points at it, never placed. |
| `--stop-at-target` | off | Stop the run when every targeted journey has met its target, instead of continuing with the parent's ordinary cycle for the remaining time. |

**The target is a goal, not a promise.** Most codebases will not give up a multiple on every
journey, and the loop's first duty is to say so from the profile rather than to discover it
on the third day. The opening report states, per targeted journey, whether the target is
plausible, what it would take, and where the ceiling is. A target the profile says is
unreachable is not silently lowered: the run continues toward the best measured gain
available, and every report shows the target next to the current number so the gap is
visible. Choose the target you would be pleased by, not the one you would be astonished by;
`3x` on a journey users feel is a very good three days.

This skill inherits everything from `/improve` that it does not explicitly change: the
state files, the gates and their signals, the mutation check, the counter-scenario, the
interleaved measurement rule, the hourly report, the daemon, and the honesty obligations.
Read `../SKILL.md` (or `parent/SKILL.md` in a standalone copy) first. This file is the difference.

## What courage means here, and what it does not

Courage is placing a bet whose payoff is large and whose cost is days, on evidence gathered
in hours. It is not skipping the evidence. An unattended loop that rewrites a service on a
hunch and hands back something subtly different is not courageous, it is a liability that
took three days to build. So every big move follows the same shape:

1. **Pin the behaviour** of the thing you are about to change, so that "the same" is a
   test suite and not an opinion.
2. **Spike the bet** - a throwaway prototype of the hottest path, in hours - and measure
   the journey. The spike decides whether the bet is placed.
3. **Strangle, do not replace**: land the new implementation piece by piece behind the
   existing interface, running both and comparing, so the system is never more than one
   commit from working.
4. **Land on numbers**: the characterization corpus passes, the shadow comparison shows no
   difference, the journey moved by at least what the spike promised.

A bet that cannot pass step 1 is not placed. A bet whose spike does not clear the threshold
is killed with its numbers in the ledger and never re-spiked on the same evidence.

## Hard rules

The parent's rules split into two groups here. These stay, without exception:

- **Never touch `main`.** Work on `improve-max/<YYYY-MM-DD>`. Bets are built on
  `improve-max/<date>/bet-<id>` and merged to the run branch only when they land.
- **Never push, never force-push, never open a PR.** Days of work still land locally. The
  human merges.
- **Never read or write `.env*`, `*credential*`, `*secret*`, `*.pem`, `*.key`.**
- **Never edit files outside the target repo**, except this skill's own state.
- **Never touch a live environment.** No deploys, no production databases, no real
  third-party accounts. Migrations run against a copy of development data the loop made
  itself.
- **Never retire a test without a successor.** An original test may be removed only when
  the characterization corpus provably covers its assertion - the corpus case must go red
  under the same mutation that made the original go red - and the ledger records the
  mapping. This is the rule most tempting to bend during a rewrite and it is the one that
  keeps a rewrite honest.

These are lifted, each with the condition that replaces it:

| Parent rule | Here |
|---|---|
| Never bump a dependency | Allowed when a bet's spike shows the gain. Recorded as part of the bet, never as a side effect of one. |
| Never add a migration or change a schema | Allowed with a down migration, tested both ways on a copy of dev data, and a data-equivalence check before and after. |
| Never rewrite a money-adjacent or once-only invariant | May be moved, never re-derived. The invariant is pinned first with a characterization corpus that includes its edge cases, and the new implementation runs in shadow against the old for the whole corpus before it takes over. |
| Diff ceiling of ~400 lines at T4 | No ceiling on a landed bet. The ceiling moves to the spike: a spike over ~4 hours or ~1,000 lines is not a spike, it is a bet being placed without its evidence. |
| Match the surrounding code's language and idiom | Match the *target* stack's idiom once a bet to change stacks has landed; until then, match the existing one. A module half in one style is the strangler's normal state, not a mess. |

## State

Everything in `/improve`'s `.improve/` plus:

    .improve/bets.jsonl        one object per bet: hypothesis, journey, expected gain, spike
                               result, kill criteria, branch, status
    .improve/corpus/           the characterization corpus: recorded inputs and outputs per
                               subsystem, snapshot baselines, the recorder scripts
    .improve/daily.md          the daily report, appended

`run.json` gains `mode: "max"`, `target_stack` (null until a stack bet lands), and the
journey table is the run's scoreboard. See `references/bets.md` and
`references/characterization.md`.

## Preflight - the first day is mostly this

The parent's preflight, then:

1. **Journeys are mandatory.** The loop cannot start without a measured journey table.
   If the repo has no harness, building one is the first work, before any bet, and the
   baseline numbers are written to `run.json` before anything else is touched. A multiple
   with no baseline is a story.
2. **Map the system into subsystems** with their interfaces: the boundaries a rewrite would
   happen behind. A subsystem is something with a call surface narrow enough to record - a
   service, a module, a screen and its view model, a set of handlers. Write the map into
   `run.json` with each subsystem's share of each journey, from the profiler.
3. **Say what the target would take.** For each targeted journey, from its profile shares,
   name the two or three bets that could plausibly move it by the target, and for each,
   the stack or design it would move to and why - CPU-bound work in an interpreted
   runtime, an allocation-heavy hot loop, a chatty wire protocol, a data model that forces
   N queries. Mark any bet whose kind `--kinds` excludes. This is the opening report, and it
   is honest about the ceiling: a journey that is I/O-bound on a remote service does not
   get a multiple from any language, and the report says so before the first spike, not
   after the third. If the sum of the plausible bets falls short of the target, say by how
   much, and say what would close the gap if the human allowed it.
4. **Snapshot development data** for anything a migration bet would touch, into
   `.improve/corpus/data/`, and record how to restore it.

## The bet cycle

A cycle here is hours, not minutes, and one bet may span many cycles. State carries it.

### 1. Pin - build the characterization corpus for the subsystem

Before a line of the subsystem changes, record what it does. `references/characterization.md`
has the per-kind recipe; the shape is always the same: capture real inputs and the outputs
they produce today, enough of them to cover the edge cases the tests and the code comments
name, and store them under `.improve/corpus/<subsystem>/`. Then write the replay test that
feeds the corpus through the *current* implementation and asserts the recorded outputs, and
land it as its own commit. Run the mutation check on it: break the current implementation
and confirm the replay goes red. A corpus that stays green under mutation is not pinning
anything.

For a UI subsystem the corpus is snapshots plus the view model's input/output pairs. For a
handler it is request/response pairs including error responses. For a data layer it is
queries and result sets over the snapshot data.

### 2. Spike - prove the gain in hours before spending days

A spike is a throwaway: the hottest path of the subsystem, in the proposed stack or design,
wired in just far enough to run the journey harness against it. Time-boxed to four hours
and around a thousand lines; it lives on its own branch and is never merged. Measure the
journey with the spike in place, interleaved, exactly as step 4 of the parent says.

The bet is placed only if the spike's journey gain is at least the run's `--spike`
threshold, or half of what the bet itself claimed, whichever is larger. Below that, the bet
is `killed` with the spike's numbers, the branch is deleted, and the evidence stays in
`bets.jsonl` so the same bet is not re-spiked from the same hypothesis. A bet killed on
numbers is a good outcome; it cost four hours and saved three days.

A bet of a kind `--kinds` excludes is never spiked. If the profile says it is the largest
available win, it is written up as `proposed` with the profile share and what the human
would need to allow.

### 3. Strangle - land the new implementation behind the old interface

Never replace a subsystem in one commit. Introduce the seam - an interface, an adapter, a
feature switch defaulting to the old path - and land it green. Then move one piece at a
time across the seam, and after each piece:

- the whole system builds and every gate is green with its signal count intact;
- the characterization replay passes against the *new* piece;
- **shadow comparison**: for the pieces that can run both ways, run old and new on the
  full corpus and on the journey harness and diff the outputs. Zero differences, or each
  difference explained and pinned as an intended change in the corpus with the reason.

Each piece is its own commit on the bet branch. The bet branch is always mergeable into the
run branch; if it is not, the last piece is reverted. This is what makes a three-day bet
survivable by a loop that may be compacted, relaunched or stopped at any hour: at any
commit, the system works, and the ledger says which pieces have crossed.

### 4. Land - or kill

A bet lands when every piece has crossed, the old path is removed (with its tests retired
by the successor rule above), the corpus passes, the shadow run is clean, and the journey,
measured interleaved on the run branch with the bet merged, has moved by at least what the
spike promised. Merge the bet branch, tag it, update `journeys.*.current`, and write the
result to `bets.jsonl`.

A bet is killed when its kill criteria trigger: the clock it was given at placement runs
out, the shadow comparison keeps finding differences the loop cannot explain, or two
consecutive pieces fail their gates. Killing means reverting the run branch to the tag
taken at placement - not deleting the bet branch, which is kept for the human with a note -
and recording why with the numbers. A killed bet after a landed spike is the expensive
outcome, and the report leads with it.

### 5. Between bets, and at the target

With no bet in flight, run the parent's ordinary cycle at whatever tier it reached. The
small work does not stop because the big work is the point; a day of strangling a service
still has hours where the gate is running and a quality item could land.

When every targeted journey has met its target, say so in the next report with the
numbers, and either stop (`--stop-at-target`) or place no further bets and run the parent's
cycle for the rest of the run. A target met early is not a reason to raise it; the human
set it.

### 6. Report

Hourly, the parent's report, with a `Bet` line: which bet, which phase, which piece, and
the journey's current number. Daily, `daily.md`: the journey table baseline to now, every
bet placed, landed or killed with its numbers, what the system is now written in and how
much of it, and the plan for the next day. The final report is the daily report plus the
diffstat and the review commands. It says what stack the human is looking at now.

## Choosing the stack

Changing language is a tool, not a goal. The target is the journey number. A bet to move a
subsystem to another stack is justified only by a spike showing that the gain comes from the
stack and not from the redesign the rewrite happened to include - so where possible spike
the redesign in the existing language first. If that alone clears the threshold, that is
the bet, and it is cheaper and safer.

When the stack is the bet: prefer what the repo's ecosystem already touches (a Go backend's
hot service moves to Rust or stays Go with a different design before it moves to anything
else; an iOS app stays Swift), prefer what the toolchain can gate with the same rigour, and
say in the daily report what the human will need to know to maintain it. A subsystem in a
language nobody on the team reads is a cost the report states, not one it hides.

## Boundaries

The parent's boundaries, plus:

- **Deadline mid-bet:** finish the piece in hand, land it or revert it, leave the bet branch
  and the seam in place, and write a handover in `daily.md` that says exactly which pieces
  crossed and how to resume or discard. A half-strangled subsystem that works is a fine
  place to stop. A broken one is not.
- **A gate the new stack cannot match** (the old stack had a race detector; the new has
  none): the bet is `proposed`, not placed. Courage does not include losing a proof.
- **The corpus cannot be recorded** because the subsystem's inputs cannot be captured
  without a live environment: the bet is `proposed`. The human can record it; the loop
  cannot.

## Launching

    /improve-max 3d ~/Code/thing --target 5x --kinds design,data
    ../scripts/improve-daemon.sh --skill improve-max --repo ~/Code/thing --for 3d \
        --args "--target 5x --kinds design,data"

Under the daemon, use `--skill improve-max` and expect long cycles; the daemon's default
cycle timeout is raised for this skill. Everything the parent says about being one cycle of
an externally paced loop applies, and matters more, because a bet spans cycles.

## References

- `../references/*` (or `parent/references/*` in a standalone copy) - inherited from `/improve`
- `references/bets.md` - the bet schema, the spike protocol, kill criteria, jq recipes
- `references/characterization.md` - recording a corpus per kind of subsystem, shadow
  comparison, retiring tests by successor
