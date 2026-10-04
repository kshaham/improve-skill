# The hourly report

Write to the terminal and `.improve/journal.md`. Send a notification only when an
available tool and existing authorization support it; this is optional.

## Terminal

    IMPROVE - hour 3 of 4 - improve/2026-09-05 - tier 2
    ------------------------------------------------------------
    Landed this hour (4)
      a1b2c3d  fix(quest): the lapse sweep no longer swallows a failed UPDATE
      e4f5a6b  test(bites): cover the deadline boundary at exactly midnight
      ...
    Rejected (4)
      q-0011  perf: batch the approval query
              gate red: TestApprovalOrdering - expected 3 rows, got 0
      s-0004  security: validate the redirect target
              contradicted Auth0Callback.swift:88, which documents why the check is upstream
      p-0007  perf: cache the tier lookup per request
              counter-scenario failed: two members of one household, second read returns the
              first member's tier (TestTierLookupHousehold, added)
      p-0008  perf: precompute the streak window
              overlap: before 1810/1795/1822 ns/op, after 1790/1815/1801
    Proposed, needs you (1)
      p-0002  Bonsai's CI has no DerivedData cache; every run recompiles 42 SPM packages
              including mlx-swift's 339 C++/Metal files. Est. most of the 45 min.
    Benched
      performance - 3 consecutive gate failures
    Journeys cold-launch 1.84s -> 1.12s (-39%)   feed-open 0.62s -> 0.62s   api-p95 212ms -> 140ms (-34%)
    Totals   quality 7/2  coverage 5/1  security 1/0  performance 2/1  concurrency 1/0
             resilience 0/0  gate-speed 1/0  docs 0/0  accessibility 2/0   backlog 6 ready
    Lanes    off: contracts (no client in tree)   idle this hour: resilience, docs
    Next     q-0019 unchecked error in the widget handoff drain

Rules for this block:

- The `hour 3 of 4` in the header uses the completed `report_hour` from `improve-clock.sh --json`, never
  its current `hour` interval. At 1h05m elapsed, report hour 1, although the current interval
  is hour 2. If several hours were missed, say which interval this report covers. A report is written only when the clock says `report ... is due`; cycles in
  between write nothing but a one-line journal entry at most.

- Rejections and benched areas are as prominent as successes. If the hour landed nothing,
  the report says "landed nothing this hour" as its first line and explains why.
- Rejection reasons quote the gate's actual output, never a paraphrase.
- A tier escalation is announced with the reason: "T1 dry after 2 refills, escalating to T2".
- Never report an item as landed unless you have its commit SHA.
- The `Journeys` line is baseline to now for every journey in `run.json`, every hour, even
  when nothing moved - an unchanged number is information. On a multi-day run this line is
  the result; the final report repeats it first, above the commit count.
- `Lanes` says which are off and why, and which active lanes were not scanned this hour, so
  a lane that has quietly rotated out is visible.
- A rejection says what killed it: the gate (quote its output), a counter-scenario (state
  the scenario and the test it became), or the measurement (show the runs). If more items
  died to counter-scenarios than to the gate this hour, say so in the first line - it means
  the gate has a hole the second look keeps finding, and the hole is the real finding.
- The first report of a run lists which scanners were found on the machine, their baseline
  counts, and which usual ones were absent. A scanner count that rose during the hour is the
  first line of that hour's report, with the reverted SHA.
- Time the daemon spent waiting on an account limit appears in the journal as its own
  entry, not folded into an hour's totals.

## Optional notification

One line, under ~120 characters:

    improve h3/4: 4 landed, 2 rejected, 1 needs you. perf benched. tier 2.

## journal.md

Append the terminal block verbatim under an ISO-8601 heading. This is what survives a session
restart or a context compaction, and it is what the human reads afterwards to decide whether
the branch is worth merging.

## The final report

Save the report to `.improve/final-report.md` as well as the journal. Report-only retries
keep the original stop reason and `ended_at`; they never imply a new working interval.

Written only when `improve-clock.sh` exits `10`, and it opens with the clock's output so the
reader can see the deadline had passed:

    IMPROVE - FINAL - improve/2026-09-05 - tier 2
    deadline 2026-09-05T18:03:00Z, now 2026-09-05T18:04:12Z, ran 4h01m of 4h

An environmental failure writes `HALTED`, a user stop writes `STOPPED`, and an explicitly
allowed improve-max target stop writes `TARGET REACHED`. Each leads with time left and why: `HALTED at 16:41Z, 1h22m before the deadline:
the iOS simulator stopped booting (gate unavailable)`.

At the deadline, add: total commits, the branch name, the one-line diffstat, every `proposed`
item in full, and the exact commands to review or discard:

    git log --oneline <baseline_commit>..improve/2026-09-05
    git diff <baseline_commit>..improve/2026-09-05

Use the recorded baseline rather than assuming the source branch was main. Discard commands
are suggestions for the human after review; never run branch deletion automatically.


## Focus and continuity

Include the local Kanban URL confirmed by `scripts/improve-board.sh --status` in opening,
hourly, and final reports. If unavailable or disabled, say so without claiming it is live.
The board stays available after the run; stopping it is independent of stopping the daemon.

Each report includes progress against the user's top priorities, the actual screens/features
or paths examined, the number of completed discovery passes, and the next concrete action.
Separate investigated, proposed, and committed work. For UI/assets include locations of
before/after captures and the states inspected. Never claim a visual result you did not see.

Hourly reports are progress updates. Continue immediately while the clock is running.
If the user stops, label the report `STOPPED`; a measured, explicitly permitted improve-max
target stop is `TARGET REACHED`. Neither is deadline completion. An unavailable finalization
agent leaves `summary_pending: true` and a factual daemon journal entry for later handover.
