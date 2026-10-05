# Priorities, discovery, and verification

[← Back to README](../README.md)

The skill uses the user’s outcomes to choose work and requires evidence before accepting a result.

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

The exact questions and answer format are in [references/intake.md](../references/intake.md).
Users can answer all ten in one message, including “none” or “use your judgment.” The agent
waits for answers, saves them, and derives an ordered focus plan. Resumed cycles and context
compaction reuse that plan. A fresh run gets a fresh intake.

An intake explicitly marked `awaiting_answers` cannot launch unattended work. Mark it
`complete` after the user has answered; legacy files without a status still accept ten
nonempty answers. Resupplying the original `--intake` on recovery preserves later saved
authorization and steering notes. New answers require a new run or explicit in-session steering.

Most discovery effort goes to the highest-priority unfinished outcomes. Other lanes support
those goals; easy cleanup cannot crowd out a requested feature or visual improvement.
Explicit exclusions apply throughout. User steering updates the plan without resetting time.

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
[references/continuation.md](../references/continuation.md). The
[work-selection guide](../references/work-selection.md) turns priorities into acceptance checks,
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

## Lanes and evidence

| Lane                               | Typical work                                                     | Required evidence                                                 |
| ---------------------------------- | ---------------------------------------------------------------- | ----------------------------------------------------------------- |
| Features (when requested)          | Existing journeys and bounded new behavior                       | User acceptance gap, executed journey, regression/acceptance test |
| UI (when requested)                | Hierarchy, spacing, feedback, responsiveness, interaction states | Actual rendering before/after at matching states and viewports    |
| Assets (when requested)            | Icons, images, illustration, animation, loading and decoding     | Asset usage/provenance and inspection in its actual screen        |
| Quality                            | Bugs, error handling, boundaries, coupling                       | Concrete failure scenario and regression check                    |
| Coverage                           | Untested behavior and error paths                                | Test that detects a meaningful mutation                           |
| Security                           | Reachable injection, authorization, sensitive-data handling      | Concrete threat path and adversarial check                        |
| Performance                        | Launch, scrolling, screen loading, endpoint latency              | Journey profile and interleaved before/after measurements         |
| Concurrency                        | Races, lifecycle/cancellation, locks and actors                  | Detector or reproducible scheduling scenario                      |
| Resilience                         | Timeouts, retries, fault handling                                | Fault-injection check                                             |
| Gate speed                         | Slow builds/tests, redundant setup                               | Timings with equivalent verification coverage                     |
| Docs                               | Broken commands and inaccurate behavior descriptions             | Executed command or checked behavior                              |
| Accessibility (relevant UI)        | Labels, keyboard/focus, contrast, dynamic text                   | Platform audit and interaction checks                             |
| Contracts (relevant client/server) | Fields, optionality, enum and format drift                       | Actual schema/round-trip check                                    |

The user's scope determines which lanes are active. A finding needs evidence, not personal
taste. Feature and visual requests can be improvements against the user's acceptance criteria;
they need not masquerade as bugs. Creation tools must be available and fit the user's costs
and constraints. Unavailable rendering is reported, never treated as a verified visual result.

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

By default, autonomous `/improve` work does not:

- Edit `main`, push, force-push, or open a PR.
- Delete/weaken tests or add skips to get green.
- Bump dependencies/toolchains, migrate schemas, or rewrite money/once-only invariants.
- Read/write secret or credential files or edit outside the target repo.
- Install new tooling, start from an unexplained dirty tree, or silently weaken a gate.

A finding needing broader authority becomes a proposal while other eligible work continues.
Explicit user authorization takes precedence within its stated scope. For example, a request
to push and merge is carried into `run.json.authorization` and honored after the checks;
the skill does not ask for that permission again. A general improvement request alone does
not grant publishing, spending, or rewrite permission. Existing exclusions still apply.
Unavailable verification gets bounded recovery, then affected work is benched; halt when
nothing eligible can be verified. Interrupted dirty edits require manual recovery. Never
reset/clean the whole tree or drop a stash to recover an item.
