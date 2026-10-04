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
