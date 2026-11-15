# Ten questions before a new run

Ask these **exactly once per new run**, before launching finders, editing the target, or
starting the duration clock. Introduce them as ten short questions to focus the work. The
user can answer in one message; “none” or “no preference” is a valid answer. If the question
tool limits a call to fewer than ten questions, use consecutive batches without omitting
any question. With no question tool, present this numbered list in chat and wait.

1. **Outcomes (`outcomes`).** What are the top three improvements you want, in priority
   order: specific features, UI, graphics/assets, performance, bug fixes, security, or
   something else?
2. **Features (`features`).** Which features or user journeys should I improve, and what
   should work differently? Say whether you want existing behavior polished or new behavior
   added.
3. **UI (`ui`).** Which screens, components, interactions, or responsive layouts need the
   most attention? Describe what feels wrong or what you want them to feel like.
4. **Graphics and assets (`assets`).** Which icons, illustrations, images, animations, or
   other assets should change, and what visual style or existing reference should guide them?
5. **Performance (`performance`).** Where do you notice slowness, excessive resource use,
   or lag, and on which device, browser, workload, or dataset?
6. **Bugs (`bugs`).** Which broken or unreliable behaviors matter most? Share reproduction
   steps or examples if you have them.
7. **Security (`security`).** Which security or privacy concerns should I focus on, such as
   permissions, authentication, exposed data, or untrusted input?
8. **Other quality goals (`quality`).** How should I prioritize tests, accessibility,
   resilience, code maintainability, documentation, and the developer workflow?
9. **Boundaries (`boundaries`).** What must stay unchanged or out of scope, and what
   compatibility, design, dependency, tooling, or cost constraints should I respect?
10. **Success (`success`).** What observable results would make this run successful, and
    which checks, examples, or before/after comparisons should demonstrate them?

Do not invent answers to unanswered questions. A user who explicitly says “use your
judgment for the rest” has answered the remainder with that preference; record it as such.
Save partial answers in `intake.json` with `status: "awaiting_answers"`, so a compacted
session can recover them before the timer starts. If a reply answers only some questions,
follow up only on the unanswered numbers. Set `status: "complete"` once all are answered. Existing
prompt details may be shown as suggested answers but do not skip presenting any of the ten.
Do not add an eleventh routine confirmation question. Resolve ordinary implementation
choices yourself. Ask follow-ups only if an actual ambiguity blocks the requested work.

## Persist the answers and derive the focus

Save the user's answers verbatim in `.improve/intake.json`:

```json
{
  "questions_version": 1,
  "answers": {
    "outcomes": "First checkout bugs, then product-page UI, then faster image loading",
    "features": "Improve the existing cart quantity and checkout flow",
    "ui": "Product detail on mobile: crowded controls and unclear selected variant",
    "assets": "Use the existing product images; preserve the current brand",
    "performance": "Product detail scrolls poorly on older phones",
    "bugs": "Changing quantity twice can show the wrong total",
    "security": "Review cart ownership checks; do not change the payment integration",
    "quality": "Regression tests and accessibility first; docs only if needed",
    "boundaries": "No checkout redesign, paid services, or new dependencies",
    "success": "Reproduction passes, mobile controls are clear, scrolling improves"
  }
}
```

Before handing off to the daemon, also save `intake.json.authorization`: any explicit
user grants from the original request or later messages, their source wording, and their
conditions. Copy them to `run.json.authorization` during preflight. This captures instructions
outside the ten answers without asking the user to repeat them. An empty grants list is valid.

Turn these into `run.json.focus` with `priorities` (ordered outcomes, lanes, paths/journeys,
and acceptance checks), `excluded` (explicit user boundaries), and `source: "intake.json"`.
Use the goal/check/status model in "Selecting work that advances the user's goals" below. Show that compact focus plan with
the opening report. Keep the original answers alongside
the interpretation so later cycles cannot quietly substitute their own goals.

- Allocate the majority of discovery effort to the highest-ranked unfinished outcomes.
  Rotate modules and hypotheses **inside** those areas before broadening the search.
- `features`, `ui`, and `assets` are opt-in lanes. Enable only the ones requested and
  supported by the target. Map performance/bugs/security to `performance`/`quality`/`security`.
  Enable the other technical lanes as relevant supporting work, honoring explicit exclusions.
- Rank eligible findings exactly as SKILL.md "Confirm and rank" and `improve_ledger.py queue`
  do: a confirmed critical in-scope vulnerability first, then task-level `user_priority`, then
  board `prefer` areas (never new work in `skip` areas), then the ordered focus priorities,
  then severity × confidence × `blast`.
  A confirmed critical vulnerability in scope preempts lower-risk work. A reportable issue
  outside the user's scope stays a proposal; it does not authorize edits.
- Feature and visual findings may be an evidenced gap against the user's stated acceptance
  criteria, rather than a crashing defect. Include the exact screen/interaction/asset and
  how the outcome will be checked. Do not manufacture cosmetic changes to fill time.
- Save explicit grants and their conditions in `run.json.authorization` alongside the
  exclusions. A preference is not an authorization to spend, publish, or broaden scope.
  Honor permissions the user has already explicitly granted; propose work that still needs
  broader authority and continue on the highest-priority eligible work.

## Resumption and unattended execution

When an intake has a `status` field, unattended work requires `"complete"`. A file marked
`"awaiting_answers"` is rejected even if all ten fields contain suggested text. Legacy
files without a status remain valid when all ten answers are present and nonempty.

Passing the original `--intake` file again checks that its answers match, but leaves the
saved intake unchanged, including later authorization, exclusions, and steering metadata.
Record steering in the active session; use `--new-run` with a fresh intake to replace it.
Rejected recovery attempts do not install a newly supplied intake file.

Wakeups, compaction, and daemon cycles with a saved intake resume without asking ten more
questions. A user steering the active run changes the saved focus with a dated note; it
does not restart the clock. A **new** run gets a new intake even in the same repo.

A noninteractive `codex exec` or `claude -p` process cannot conduct this conversation.
Finish it in the interactive session first, then pass the saved file and current host
(`--engine codex` from Codex, `--engine claude` from Claude Code):

```sh
scripts/improve-daemon.sh --engine codex --repo /path/to/repo --for 4h --intake /path/to/intake.json
```

The daemon checks that all ten keys have nonempty string answers before starting its
clock. It refuses a missing/incomplete intake instead of guessing preferences or starting
a billed retry loop. On resume it reuses `.improve/intake.json`; after a finished run,
`--new-run --intake FILE` archives the old state before starting with the new answers.

# Selecting work that advances the user's goals

Read this after intake and when a run is producing many proposals or repeated empty scans.

## Turn priorities into observable outcomes

Give each ordered priority an ID, explicit scope, acceptance checks, and status in
`run.json.focus.priorities`. Example:

```json
{"id":"p1","rank":1,"goal":"Mobile cart recovers clearly from a failed update",
 "lanes":["quality","ui"],"scope":["src/cart","mobile cart journey"],
 "checks":["reproduce a failed update and retry without losing quantity",
           "inspect loading, error, retry and success at 390px"],
 "status":"active","blocker":null}
```

Use `active`, `met`, `blocked`, or `excluded`. `met` requires evidence linked to the checks;
running out of ideas does not meet a goal. A blocked goal names its missing capability or
decision and the condition that would make it workable again. Preserve the user's ordering.
Every candidate links to `focus_priority` and at least one acceptance check or failure.

Board area focus (`run.json.focus.board_areas`) ranks `prefer` areas ahead of unmarked
areas within the same `user_priority`, and removes `skip` areas from new finder lanes and
task selection. It never widens the intake scope or authorization.

At a checkpoint, apply board priority requests and acknowledge the saved changes before
choosing the next eligible item: `high`, then `normal` or absent, then `low`; use the ordered focus
plan and evidence-based importance. Scope, critical-security preemption, dependencies,
and verification gates still apply. A high priority cannot make blocked work executable
or extend the deadline. When bypassing it, record the concrete blocker and next useful work.

Before each refill, look at what the last two generations actually investigated. If a top
active priority received no scan, give it the next scope before another supporting cleanup.
If a priority is met, continue on the next active priority; do not raise the completed target
or redesign adjacent features without scope. If blocked, work the next eligible priority and
recheck only when its stated unblock condition changes.

## Confirm a candidate before paying for its fix

For a defect, trace the claimed bad input to the observed failure. Check whether the caller,
validator, framework, or documented invariant already prevents it. For a feature or visual
gap, reproduce the current behavior and compare it with the intake's acceptance check.
Discard false positives with the exact contrary evidence. A finder reporting a suspicion
does not by itself authorize a patch.

For a proposed implementation, identify which gate proves it and what could regress. If the
fix depends on another unrelated change, queue that dependency separately rather than
turning one item into a large opportunistic rewrite. Read-only investigation may continue
while a baseline check runs; edits wait for an applicable green baseline.

## Admit work using real verification costs

Before starting an item, read the clock and estimate **edit + build + required test runs +
mutation/counter-scenario + relevant rendering or benchmarking + rollback/reporting** from
measured gate durations and similar completed items. Record the estimate and actual time on
the candidate when available. Do not count only coding time or automatically reject every
item that needs more than half the remaining window.

If it does not fit, choose a smaller independently useful change or spend the remaining time
on a bounded reproduction/scan that can be saved. Split a large feature only at a seam that
can be verified and left working. A partial visual redesign without a usable result is not
an independently useful change. Never skip verification to squeeze in another commit.

Finder scopes need budgets too: inspect one named journey or subsystem with a specific
hypothesis, then return evidence and the next scope. If a broad scan repeatedly times out,
reduce its scope. A timeout does not establish that the subsystem is dry.

## Learn from rejection without thrashing

Record a rejection as one of: disproved claim, implementation regression, gate/environment
failure, inconclusive measurement, out-of-scope change, or missing capability. Those have
different next actions: change the discovery hypothesis, repair within the item’s attempt
budget, recover/bench its gate, improve the measurement, or move to eligible work.

After two failed implementation attempts, preserve the evidence and move on. Reopening needs
new evidence or changed code and links to the prior item. Cosmetic renaming, new timestamps,
or increasing the cycle number does not make an old attempt new work. An empty but genuinely
new investigation is useful; repeating the same investigation without new evidence is not.

## Leave evidence a reviewer can use

For each landed item, keep the user priority, acceptance/failure, commit SHA, verification
commands and observed results, and before/after artifact paths when relevant. Reports should
distinguish **met goals**, **verified partial progress**, **investigated with no change**, and
**blocked/proposed**. Commit count alone is not a measure of whether the user's desired
feature, UI, or performance actually improved.
