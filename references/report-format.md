# The hourly report

Same content, three deliveries: terminal block, `PushNotification` headline, and an append to
`.improve/journal.md`.

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

## PushNotification

One line, under ~120 characters:

    improve h3/4: 4 landed, 2 rejected, 1 needs you. perf benched. tier 2.

## journal.md

Append the terminal block verbatim under an ISO-8601 heading. This is what survives a session
restart or a context compaction, and it is what the human reads afterwards to decide whether
the branch is worth merging.

## The final report

At the deadline, add: total commits, the branch name, the one-line diffstat, every `proposed`
item in full, and the exact commands to review or discard:

    git log --oneline main..improve/2026-09-05
    git diff main..improve/2026-09-05
    git branch -D improve/2026-09-05     # discard everything
