# `.improve/` state

## run.json

    {
      "repo": "/Users/x/Code/bonsai",
      "branch": "improve/2026-09-05",
      "baseline_commit": "8765b28",
      "started_at": "2026-09-05T14:03:00Z",
      "deadline":   "2026-09-05T18:03:00Z",
      "cycle": 7,
      "tier": 2,
      "benched_areas": ["performance"],
      "lanes": {
        "active": ["quality","coverage","security","performance","concurrency","resilience","gate-speed","docs","accessibility"],
        "off": { "contracts": "no client of the server in this tree" },
        "last_scanned": { "quality": 6, "coverage": 5, "security": 7, "docs": 3 },
        "last_yield":   { "quality": 3, "coverage": 1, "security": 0, "docs": 0 }
      },
      "journeys": {
        "cold-launch":  { "harness": "xcodebuild test -only-testing:PerfTests/LaunchTests", "unit": "s",
                          "baseline": [1.84,1.79,1.91,1.82,1.88], "current": [1.12,1.09,1.15,1.11,1.14] },
        "feed-open":    { "harness": "...", "unit": "s", "baseline": [0.62], "current": [0.62] },
        "api-p95":      { "harness": "cd backend && go test -bench=BenchmarkTopHandlers -count=5 ./cmd/loadmix", "unit": "ms",
                          "baseline": [212,208,215,210,214], "current": [140,138,144,141,139] }
      },
      "detectors": { "race": "cd backend && go test -race ./...", "a11y": "xcodebuild test -only-testing:UITests/AccessibilityAudit" },
      "gates": { "...": "see verification.md" },
      "gate_seconds": { "go.fast": 12, "swift.fast": 95, "swift.batch": 140 },
      "scanners": { "gitleaks": { "cmd": "gitleaks detect ...", "baseline": 0, "seconds": 3 } },
      "guardrails": ["no-migrations", "no-dep-bumps", "no-test-deletion", "no-invariant-rewrite"],
      "counts": { "done": 11, "rejected": 3, "proposed": 2 }
    }

## backlog.jsonl

One JSON object per line. Append new items; rewrite the file in place to change status.

    {"id":"q-0007","area":"quality","tier":1,"severity":3,"confidence":0.9,"blast":2,
     "file":"backend/internal/quest/bite.go","line":214,
     "claim":"lapse sweep swallows the repository error and reports success",
     "evidence":"err is assigned and never checked; a failed UPDATE returns nil to the caller",
     "failure":"a transient DB error silently marks a bite un-lapsed and the player keeps the streak",
     "status":"ready","commit":null,"note":null,
     "counter":null,"measurement":null}

Fields: `area` is one of `quality|coverage|security|performance|concurrency|resilience|
gate-speed|docs|accessibility|contracts`; only lanes in `lanes.active` may appear. `severity` 1-5,
`confidence` 0-1, `blast` 1-5. `status` is `ready|done|rejected|benched|proposed`.
`note` carries the rejection reason verbatim - the gate's actual output, not a paraphrase.

`counter` is filled when an item reaches the counter-scenario check in step 4: the scenario
verbatim as the subagent returned it, or `"none"`. An item `rejected` by its counter-scenario
has the scenario in `note` too, so a later attempt starts from the input that broke the
first one. Over a run, the ratio of `"none"` to real scenarios is what the report uses to
say whether the second look is earning its cost.

`measurement` is required on every `performance` and `gate-speed` item that reaches step 4,
naming the journey (or gate) and carrying the interleaved raw runs in the order taken, at
least five pairs:
`{"journey":"cold-launch","unit":"s",
  "runs":[["before",1.84],["after",1.12],["before",1.79],["after",1.09],["before",1.91],["after",1.15],["before",1.82],["after",1.11],["before",1.88],["after",1.14]]}`.
Raw and ordered, not medians, so whoever reads the ledger can see the separation for
themselves. When an item lands, copy its after-runs into `journeys.<name>.current` in
`run.json`; that is where the hourly journey line comes from. An item `rejected` for overlap, or `proposed` for a gain under five percent,
keeps its runs.

## Recipes

    # ranked ready queue
    jq -s 'map(select(.status=="ready"))
           | sort_by(-(.severity * .confidence * .blast))' .improve/backlog.jsonl

    # security always preempts
    jq -s 'map(select(.status=="ready" and .area=="security"))' .improve/backlog.jsonl

    # is this finding already known (including dismissed)?
    jq -s --arg f "$FILE" 'map(select(.file==$f)) | map({claim,status,note})' .improve/backlog.jsonl

    # counts for the hourly report
    jq -s 'group_by(.area)[] | {area:.[0].area,
            done:   map(select(.status=="done"))|length,
            reject: map(select(.status=="rejected"))|length}' .improve/backlog.jsonl

    # set an item's status
    jq -c --arg id "$ID" --arg s done --arg c "$SHA" \
       'if .id==$id then .status=$s | .commit=$c else . end' \
       .improve/backlog.jsonl > .improve/backlog.tmp && mv .improve/backlog.tmp .improve/backlog.jsonl
