# `.improve/` state

## run.json

    {
      "repo": "/Users/x/Code/bonsai",
      "branch": "improve/2026-09-05",
      "baseline_commit": "8765b28",
      "started_at": "2026-09-05T14:03:00Z",
      "deadline":   "2026-09-05T18:03:00Z",
      "focus": { "source": "intake.json", "priorities": [], "excluded": [] },
      "authorization": { "grants": [], "exclusions": [] },
      "phase": "discover",
      "preflight_complete": true,
      "last_activity_at": "2026-09-05T17:03:00Z",
      "next_action": "Inspect the mobile cart error states against priority 1",
      "continuation": { "mode": "foreground" },
      "discovery": { "generation": 3, "cursor": "cart:error-states" },
      "active_item": null,
      "cycle": 7,
      "tier": 2,
      "last_report_hour": 3,
      "dry_sweep": 0,
      "scanned_paths": ["backend/internal/quest", "ios/Bonsai/Feed"],
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
      "counts": { "done": 11, "rejected": 3, "proposed": 2 },
      "outcome": null
    }

`started_at` and `deadline` are ISO-8601 UTC with a `Z`, written from the system clock (or copied exactly from `supervisor.json`), because
`scripts/improve-clock.sh` reads them and nothing else decides what time it is.
`last_report_hour` is the wall-clock hour the latest hourly report covered (0 before the
first); the clock compares it with elapsed time to say whether a report is due.
`scanned_paths` is every directory a finder has been aimed at this run, which is what the
dry sweep uses to find ground nobody has looked at. `dry_sweep` is which step of the sweep
(0 = not dry, 1-4 = the step in SKILL.md's Escalation) the next refill runs.

`outcome` stays `null` while the run is live - that is what marks a run as resumable. At the
end it becomes `completed` when the clock has exited `10`, `halted: <reason>` for a halt
condition, `stopped by user`, or `target reached` for improve-max with explicit
`--stop-at-target`. Never `completed` while the clock still says `RUNNING`. The supervisor
repairs an agent’s premature completion and records it in the journal. `summary_pending`
means the narrative final report still needs writing; it does not claim verification.
Explicit `--resume` after recovery clears a halt/stop marker while preserving the deadline
and journaling the previous outcome. A plain restart never clears terminal state.

## backlog.jsonl

One JSON object per line. Append new items; change status by writing a temporary file and atomically renaming it.
Apply the same temporary-file/rename rule to run.json. Only the main session owns these
writes; finders return evidence to it and never race to update shared state.

    {"id":"q-0007","area":"quality","tier":1,"severity":3,"confidence":0.9,"blast":2,
     "file":"backend/internal/quest/bite.go","line":214,
     "claim":"lapse sweep swallows the repository error and reports success",
     "evidence":"err is assigned and never checked; a failed UPDATE returns nil to the caller",
     "failure":"a transient DB error silently marks a bite un-lapsed and the player keeps the streak",
     "status":"ready","commit":null,"note":null,
     "counter":null,"measurement":null}

Fields: `area` is one of `quality|coverage|security|performance|concurrency|resilience|
gate-speed|docs|accessibility|contracts|features|ui|assets`; only lanes in `lanes.active` may appear. `severity` 1-5,
`confidence` 0-1, `blast` 1-5. `status` is `ready|in_progress|done|rejected|blocked|proposed`.
Legacy `benched` tasks display in the board's Blocked column. A benched lane remains
separately recorded in `run.json.benched_areas`.
`focus_priority` links to the ordered focus plan; `acceptance` records the observable
requested result for feature/visual work; `reopens` links to a prior candidate only when
new evidence permits reconsideration.
`note` carries the rejection reason verbatim - the gate's actual output, not a paraphrase.

For the live board, add a concise `title`, `kind` (`change` or `investigation`), and UTC
`created_at`/`updated_at`; record `started_at` and `completed_at` when applicable. These
fields are optional for old records. Set `in_progress` before editing, then save the
settled status, verification evidence, and actual commit SHA. Investigations may finish
without a commit when their findings/checks are recorded. Do not delete finished or
rejected tasks, renumber IDs, or mark an unverified change done. The viewer supports old
append-only updates by merging repeated IDs in file order; skill writers should continue
using one canonical row per ID with atomic rewrites so queue queries remain correct.
See [board.md](board.md) for column meanings and service lifecycle.

Task lifecycle timestamps, IDs, titles, status labels, `user_priority`, `focus_priority`,
`board_request_id`, and `user_notes` are bookkeeping for the viewer,
not proof of new work. Pair a transition with evidence or verification. The daemon also
checks `bets.jsonl`, including measurements and actual implementation-piece commits;
changing a piece's status without evidence does not reset its failure breaker.

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

    # ranked ready queue after scope and critical-security checks
    jq -s 'map(select(.status=="ready"))
           | sort_by([(if .user_priority=="high" then 0 elif .user_priority=="low" then 2 else 1 end),
                      (.focus_priority // 999), -((.severity // 0) * (.confidence // 0) * (.blast // 0))])' .improve/backlog.jsonl

    # inspect in-scope critical security findings for preemption
    jq -s 'map(select(.status=="ready" and .area=="security" and .severity==5))' .improve/backlog.jsonl

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


## Intake, discovery, and supervision

`intake.json` preserves all ten answers before any new run starts; see `intake.md`. Keep
`run.json.focus` alongside it as the interpreted, ordered plan. User steering changes that
plan with a dated journal note, never silently restarts the duration.
`run.json.authorization` records explicit user grants, their source and conditions, and
remaining exclusions. Carry it across cycles; neither a finder proposal nor a new priority
silently grants permission to publish, spend, or expand the requested scope.

Board submissions live in `operator.json`; acknowledged outcomes live in
`board-receipts.json`. Consume them at item boundaries using `scripts/improve_control.py`,
and link created tasks with `board_request_id`. `user_priority` records high/normal/low
without changing evidence-based severity. The board never writes worker-owned ledgers.
`user_notes` is an optional array of `{request_id, text, at}` objects for operator context.
Append each request UUID once, preserving the task's original `note`, status, verification,
and evidence. User context and priority edits do not satisfy the daemon's progress check.
`pause.json` belongs to the user and is scoped to the supervisor's immutable `run_id`;
the supervisor alone writes its `paused` phase. See [board-control.md](board-control.md).

`discovery.jsonl` records one row per scan:

```json
{"generation":3,"tier":2,"lane":"ui","focus_priority":1,"paths":["src/cart"],"hypothesis":"error state obscures retry control","revision":"a1b2c3d","status":"complete","accepted":0,"rejected":1,"evidence":"rendered network-error state; retry remained visible at 390px","next_scope":"cart keyboard focus","at":"2026-09-05T17:03:00Z"}
```

`supervisor.json` belongs to the daemon. Agents copy its start/deadline into their run
ledger and do not edit it. It preserves the deadline even if preflight never finished.
Its `engine` is `codex` or `claude`, selected before the first cycle and kept on recovery.
Older supervisor files lacking `engine` are migrated to `claude`, the historical provider.
`codex_sandbox` saves an explicit Codex mode, or null to use the CLI's configuration.
`model` saves the initial explicit model selection, or null for the CLI default. Recoveries
reuse it even if `IMPROVE_MODEL` changes. The clock uses these supervisor timestamps and
the agent's `last_report_hour`; it works before the first agent checkpoint.
`daemon.lock` is an OS-held exclusive lock; its file may remain when no process is running.
Use `--status` to inspect liveness. `stop.request` asks the supervisor to stop after the
current cycle. `history/` contains state archived explicitly with `--new-run`.

An interrupted item records `active_item: {"id":"q-0007","start_commit":"...",
"owned_paths":["src/cart.ts"],"last_verification":"..."}`. Inspect both the actual git diff
and this record before recovery. Never use the record as permission to erase unrelated work.


`final-report.md` is the durable final handover. A report-only retry preserves the original
outcome and `ended_at`; a new report's timestamp is not a new run end time. The supervisor
stores `consecutive_failures`, `limit_waited_seconds`, `next_limit_wait`, `phase`, `child_pid`,
`heartbeat_at` (last supervisor state update), `retry_at`, and `limit_wait_started_at` (the
unaccounted start of a pending wait). A restarted daemon honors `retry_at` and accounts the
elapsed portion once, including downtime. Agents must not edit those
fields. `--status --json` returns them with the current clock and the agent's run state.
