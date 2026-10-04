# /improve

An autonomous improvement skill for Claude Code. It asks ten questions about what matters,
then discovers and implements verified improvements for the requested duration. The focus
can be features, UI, graphics/assets, performance, bugs, security, or supporting code quality.
An empty backlog starts another discovery pass; it does not finish the run.

```text
/improve 4h
/improve 90m ~/Code/api
```

## Start with the user's priorities

Every new run begins with exactly ten questions, before the timer starts:

1. Top three desired outcomes, in order.
2. Features or journeys and the behavior that should change.
3. Screens, components, interactions, and layouts needing attention.
4. Graphics/assets and the desired style or reference.
5. Performance pain points and the affected workload/device.
6. Known bugs and reproduction examples.
7. Security and privacy concerns.
8. Tests, accessibility, resilience, maintainability, docs, and developer workflow.
9. Exclusions, compatibility, design, tooling, and cost constraints.
10. Observable success criteria and how to check them.

The exact questions and answer format are in [references/intake.md](references/intake.md).
Users can answer all ten in one message, including “none” or “use your judgment.” The agent
waits for answers, saves them, and derives an ordered focus plan. Resumed cycles and context
compaction reuse that plan. A fresh run gets a fresh intake.

Most discovery effort goes to the highest-priority unfinished outcomes. Other lanes support
those goals; easy cleanup cannot crowd out a requested feature or visual improvement.
Explicit exclusions apply throughout. User steering updates the plan without resetting time.

## Lanes and evidence

| Lane | Typical work | Required evidence |
|---|---|---|
| Features (when requested) | Existing journeys and bounded new behavior | User acceptance gap, executed journey, regression/acceptance test |
| UI (when requested) | Hierarchy, spacing, feedback, responsiveness, interaction states | Actual rendering before/after at matching states and viewports |
| Assets (when requested) | Icons, images, illustration, animation, loading and decoding | Asset usage/provenance and inspection in its actual screen |
| Quality | Bugs, error handling, boundaries, coupling | Concrete failure scenario and regression check |
| Coverage | Untested behavior and error paths | Test that detects a meaningful mutation |
| Security | Reachable injection, authorization, sensitive-data handling | Concrete threat path and adversarial check |
| Performance | Launch, scrolling, screen loading, endpoint latency | Journey profile and interleaved before/after measurements |
| Concurrency | Races, lifecycle/cancellation, locks and actors | Detector or reproducible scheduling scenario |
| Resilience | Timeouts, retries, fault handling | Fault-injection check |
| Gate speed | Slow builds/tests, redundant setup | Timings with equivalent verification coverage |
| Docs | Broken commands and inaccurate behavior descriptions | Executed command or checked behavior |
| Accessibility (relevant UI) | Labels, keyboard/focus, contrast, dynamic text | Platform audit and interaction checks |
| Contracts (relevant client/server) | Fields, optionality, enum and format drift | Actual schema/round-trip check |

The user's scope determines which lanes are active. A finding needs evidence, not personal
taste. Feature and visual requests can be improvements against the user's acceptance criteria;
they need not masquerade as bugs. Creation tools must be available and fit the user's costs
and constraints. Unavailable rendering is reported, never treated as a verified visual result.

## How the loop works

After intake, record the start/deadline, discover the repository's gates, establish a green
baseline, and create a unique `improve/YYYY-MM-DD` branch. Setup time counts toward the
requested working duration.

Each cycle reads the clock and durable state, then:

1. Refill below five ready items, with only one refill generation active. Use up to four
   read-only finders within available agent slots, or inspect serially if agents are absent.
2. Rank by user priority, then severity × confidence × impact. An in-scope critical
   vulnerability preempts lower-risk work.
3. Fix one item at a time. Save its starting commit and owned paths before editing.
4. Run the relevant gates and acceptance checks. Commit a verified result or roll back only
   that item's edits. Save rejected findings with their evidence.
5. Report completed wall-clock hours and checkpoint the next concrete action.
6. Continue immediately in the foreground, verify an actual scheduler's re-entry, or return
   to the external daemon for the next cycle.

No scheduling tool is assumed to exist. Foreground mode does not send its final response
just because a batch or report finished. The execution contract is in
[references/continuation.md](references/continuation.md). The
[work-selection guide](references/work-selection.md) turns priorities into acceptance checks,
confirms findings before fixes, and budgets editing plus verification rather than coding alone.

## When the queue runs dry

Discovery progresses through defects, coverage, structure, then bounded refactors. A tier
is dry only after eligible user priorities have been inspected; a failed or cancelled scan
is not an empty result. At the deepest tier, inspect the run's changed code, unscanned
subsystems, applicable previously-disabled lanes, and deferred work that has become feasible.

Each pass records its paths, hypothesis, revision, accepted/rejected results, and next scope.
Use a different hypothesis or source of evidence on later passes. Do not repeatedly scan
identical code with an identical prompt. Do not manufacture cosmetic changes or a commit quota.

There is **no 20-minute dry-sweep sleep**. Keep investigating useful evidence until the
clock expires. Wait only on real tools, dependencies, or account limits. Rejections trigger
diagnosis and rotation; three weak ideas do not automatically terminate a whole run.

## Verification and boundaries

Behavior changes require the repository gate, a meaningful mutation/regression check, and
an executed counter-scenario from an independent reviewer when one is available. When no
subagent is available, do a separate adversarial pass and disclose that limitation. Test
counts are compared for matching commands/filters; builds and linters use appropriate signals.
A documented silent-success command is not a failure just because it prints nothing.

Performance starts at user journeys and profiles, not guessed micro-optimizations. Build a
missing native harness first. Alternate before/after runs for at least five pairs; require
nonoverlapping timings and at least a 5% median gain before landing a performance change.
UI and assets also require inspection of the rendered result. Pure docs/assets do not need
an artificial code mutation. Tests and fixes land together if the test needs the fix to pass.

Autonomous `/improve` work does not:

- Edit `main`, push, force-push, or open a PR.
- Delete/weaken tests or add skips to get green.
- Bump dependencies/toolchains, migrate schemas, or rewrite money/once-only invariants.
- Read/write secret or credential files or edit outside the target repo.
- Install new tooling, start from an unexplained dirty tree, or silently weaken a gate.

A finding needing broader authority becomes a proposal while other eligible work continues.
Unavailable verification gets bounded recovery, then affected work is benched; halt when
nothing eligible can be verified. Interrupted dirty edits require manual recovery. Never
reset/clean the whole tree or drop a stash to recover an item.

## Clock and reports

```sh
scripts/improve-clock.sh /path/to/repo
scripts/improve-clock.sh --json /path/to/repo
scripts/improve-clock.sh --remaining /path/to/repo
```

Exit `0` means time remains, `10` means the deadline passed, and `2` means invalid state.
Handle these explicitly when using `set -e` or command chaining. `report_hour` is the completed
hour to report; `hour` is the current interval. At 1h05m elapsed, report hour 1.

Reports go to the session and `.improve/journal.md`; notifications are optional when available
and authorized. Report progress against user priorities, inspected scopes, completed discovery,
commits, rejected/proposed items, measurements, and the next action. “No changes this hour”
is valid. An hourly report is a progress update, followed by more work.

Only expiry permits `completed`. User stop and environmental halt are labeled separately.
Deadline mid-item means settle that item safely, then stop; no new fixes start after expiry.
A finalization failure records `summary_pending: true`, never invented verification.

## Running under the daemon

For work that should survive terminal closure, complete the interactive intake first:

```sh
scripts/improve-daemon.sh --repo ~/Code/api --for 6h --intake /path/to/intake.json
scripts/improve-daemon.sh --repo ~/Code/api --for 90m --dry-run
scripts/improve-daemon.sh --status --repo ~/Code/api
scripts/improve-daemon.sh --stop --repo ~/Code/api
nohup scripts/improve-daemon.sh --repo ~/Code/api --for 6h --intake /path/to/intake.json >improve-launch.log 2>&1 &
```

Requires **Python 3.9+, Git, and the Claude CLI** on macOS/Linux; no jq or GNU timeout is
needed by the runner. It invokes `claude -p --permission-mode bypassPermissions` for each
cycle, so launch only for an authorized unattended run. It loads the skill beside the runner,
which permits development checkouts and installations in other locations.

The daemon preserves one deadline in `supervisor.json` across restarts and preflight failures.
It restores an agent-reset deadline and rejects premature completion. A fast empty scan is
valid if it checkpoints new evidence. Incrementing a counter or rewriting an identical
scan’s timestamps does not count. New commits, findings, scans, and meaningful preflight
results do count. The next prompt redirects a stalled cycle toward a fresh scope; repeated
failed/uncheckpointed cycles trip a bounded breaker. Failure and completed wait totals survive
ordinary process restarts.
Completed finders are persisted before a cycle exits; live subagents cannot cross processes.

Account limits get bounded exponential backoff, with waits capped at the actual remaining
time and journaled. Waits respond promptly to `--stop`; a running cycle settles first. A
portable process-group timeout bounds wedged work. Dirty trees and recorded halts stop
relaunches. At expiry one bounded finalization cycle runs even if the last wait crossed the
deadline. Work in progress may overrun only within `IMPROVE_FINISH_GRACE` (120 seconds by default);
the earlier cycle timeout still applies. The original deadline is never extended. A stop
request applies the same cleanup bound, preventing a wedged one-hour cycle from delaying it. The daemon cannot execute while the machine is asleep or powered off.

To resume an unfinished clean run, repeat its command; the recorded deadline wins over
`--for`. To start again after reviewing a finished run, collect a new intake and use:

```sh
scripts/improve-daemon.sh --repo ~/Code/api --for 4h --new-run --intake /path/to/new-intake.json
```

After resolving a halt, resume the same run without extending its deadline:

```sh
scripts/improve-daemon.sh --repo ~/Code/api --resume
```

Recovery requires a clean tree and resolution of any recorded `active_item`. Repeat the
original `--skill` and `--args` for an improve-max run. A resume after the original deadline
performs finalization only; it does not grant extra work time.

To regenerate a pending final report without opening another improvement cycle:

```sh
scripts/improve-daemon.sh --repo ~/Code/api --finalize
scripts/improve-daemon.sh --repo ~/Code/api --status --json
```

A report retry preserves the original stop reason and end time, writes `final-report.md`,
and checks that no work-tree changes or new commits occurred. Status JSON includes the
saved phase, current child process, retry time, clock, and next action.

`--new-run` archives the previous state under `.improve/history/`. It does not discard code or
reset branches. Status and dry-run do not create state or launch a paid agent.

## State on disk

| File under `.improve/` | Purpose |
|---|---|
| `intake.json` | All ten user answers |
| `run.json` | Focus, deadline, gates, tier, cycle, next action, active item, outcome |
| `backlog.jsonl` | Candidates, evidence, acceptance checks, status, commit SHA |
| `discovery.jsonl` | Scoped scans and their results, including empty/failed passes |
| `journal.md` | Reports, steering, recovery and account-limit gaps |
| `final-report.md` | Durable final handover; retryable independently of work |
| `supervisor.json` | Daemon-owned timestamps and finalization status |
| `daemon.log` | Timestamped supervisor and cycle output |
| `daemon.lock` | OS-held exclusive lock; file presence alone does not mean running |
| `history/` | Explicitly archived previous run state |

The primary session owns atomic state updates. See [references/ledger.md](references/ledger.md).
Review changes against `baseline_commit`, not an assumed `main` branch:

```sh
git log --oneline <baseline_commit>..<run_branch>
git diff <baseline_commit>..<run_branch>
```

## Runner configuration

| Environment variable | Default | Purpose |
|---|---|---|
| `IMPROVE_CYCLE_TIMEOUT` | `3600` (`14400` for max) | Maximum seconds per working cycle |
| `IMPROVE_FINAL_TIMEOUT` | `300` | Maximum seconds for finalization |
| `IMPROVE_FINISH_GRACE` | `120` | Maximum cleanup seconds after deadline or stop |
| `IMPROVE_CYCLE_GAP` | `2` | Seconds between daemon cycles; capped at deadline |
| `IMPROVE_TOO_FAST` | `25` | Diagnostic threshold; speed alone never fails a checkpointed cycle |
| `IMPROVE_MAX_FAILURES` | `3` | Consecutive failed or uncheckpointed cycles before halt |
| `IMPROVE_LIMIT_WAIT` | `900` | First account-limit wait |
| `IMPROVE_LIMIT_WAIT_CAP` | `3600` | Maximum individual account-limit wait |
| `IMPROVE_LIMIT_WAIT_BUDGET` | `14400` | Total account-limit wait budget |
| `IMPROVE_MODEL` | unset | Optional model; `--model` overrides |

## /improve-max

[The max variant](max/SKILL.md) pursues measured, larger transformations over days. It inherits
the intake, focus, continuation, and verification rules, then adds characterization corpora,
throwaway spikes, incremental replacement behind interfaces, and measured acceptance gates.
Only its explicit rules permit dependency, schema, wire, framework, or stack changes.

```text
/improve-max 3d ~/Code/api --target 5x --kinds design,data
```

```sh
scripts/improve-daemon.sh --skill improve-max --repo ~/Code/api --for 3d \
  --args "--target 5x --kinds design,data" --intake /path/to/intake.json
```

`--target` defaults to `3x`; `--spike` defaults to `max(1.5x, target/3)`; `--kinds` limits the
allowed bet types. Meeting targets returns to ordinary improvements until expiry, unless
`--stop-at-target` explicitly permits `outcome: "target reached"`. Targets are measured goals,
not promises. See [bets](max/references/bets.md) and
[characterization](max/references/characterization.md) for the larger-change protocol.

## Development checks

```sh
python3 -B -m unittest discover -s tests -v
bash -n scripts/improve-clock.sh scripts/improve-daemon.sh
```

The regression suite uses temporary git repositories and a fake Claude CLI. It tests intake,
deadlines, early completion, discovery continuity, restart, rate limits, lock ownership,
timeouts, dirty state, recovery, repeated-scan detection, stop/deadline cleanup, report-only
retries, and finalization without launching a real model or editing a real app.
It does not establish that every model will find useful changes for a multi-hour run.

Licensed under [MIT](LICENSE).
