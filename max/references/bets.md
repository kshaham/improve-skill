# Bets

A bet is the unit of work in `/improve-max`, the way an item is the unit in `/improve`. It
is larger, it takes longer, and it is placed on evidence the loop gathered itself.

## bets.jsonl

One JSON object per line. Append on creation; rewrite in place to change status.

    {"id":"b-003",
     "subsystem":"feed-service",
     "journey":"feed-open",
     "hypothesis":"feed-open spends 71% of 0.62s in Python JSON assembly over 40 rows; a Go handler over the same query returns in under 80ms",
     "kind":"stack",
     "target":"go",
     "claimed_gain":6.0,
     "threshold":3.3,
     "spike":{"branch":"improve-max/2026-09-18/spike-b-003","hours":2.5,"lines":640,
              "runs":[["before",0.62],["after",0.09],["before",0.61],["after",0.10],["before",0.64],["after",0.09],["before",0.62],["after",0.11],["before",0.63],["after",0.09]],
              "gain":6.6},
     "placed_at":"2026-09-18T14:10:00Z",
     "tag_at_placement":"max-b-003-start",
     "branch":"improve-max/2026-09-18/bet-b-003",
     "clock_hours":20,
     "pieces":[{"name":"seam: FeedProvider interface + switch","status":"landed","commit":"a1b2c3d"},
               {"name":"list endpoint","status":"landed","commit":"d4e5f6a","shadow":"0 diffs / 1,240 cases"},
               {"name":"detail endpoint","status":"in-progress"},
               {"name":"remove python path + retire tests","status":"pending"}],
     "kill":{"clock":"2026-09-19T10:10:00Z","consecutive_piece_failures":0,"unexplained_shadow_diffs":0},
     "status":"placed",
     "landed":null,
     "note":null}

`kind` is one of `stack` (language or runtime), `framework`, `design` (same stack, new
shape), `data` (schema, index, migration), `wire` (protocol or payload format), `dependency`.

`status` is `spiking | killed | placed | landed | proposed`. `proposed` is a bet the loop
may not place unattended - no corpus can be recorded, the new stack loses a gate - written
up for the human with the spike numbers if a spike was run.

`landed` carries the interleaved journey runs on the run branch after merge, and the gain.
`note` carries the kill reason verbatim with its numbers.

## The spike protocol

1. Branch from the run branch: `improve-max/<date>/spike-<id>`.
2. Build only the hottest path of the subsystem in the proposed stack or design, wired in
   just far enough that the journey harness exercises it. Stubs and hard-coding are fine.
   Tests are not required. Nothing from a spike is ever merged.
3. Time-box: four hours or about a thousand lines, whichever comes first. Over either, stop
   and record the spike as `killed: over budget`. A spike that needs more than that is a
   bet being placed without its evidence.
4. Measure the journey interleaved - spike build, baseline build, alternating, at least five
   pairs - and record every run.
5. Threshold: the bet is placed if the spike's gain is at least `threshold`, which is the
   larger of the run's `spike_threshold` in `run.json` (from `--spike`, default
   `max(1.5, target/3)`) and half of `claimed_gain`. Otherwise `killed`, with the runs,
   and the spike branch deleted. `kind` must be in the run's `kinds`; if not, the bet is
   `proposed` without a spike.
6. Where the bet changes stack, spike the redesign in the existing stack first if that is
   plausible. If it clears the threshold alone, place that bet instead - it is cheaper, and
   the human keeps their language.

## Kill criteria

Set at placement and written into the bet. Checked at the end of every piece.

- **Clock.** `clock_hours` from placement, defaulting to four times the spike's hours,
  capped at what remains of the run minus six hours for landing. Past it, kill.
- **Two consecutive piece failures.** A piece whose gates or shadow comparison fail twice
  in a row is reverted, and the bet is killed. Two in a row means the seam is wrong, not
  the piece.
- **Unexplained shadow differences.** A difference the loop cannot attribute to an intended
  change and pin in the corpus with a reason. One is investigated; a second unexplained
  one kills the bet.
- **A gate lost.** The new implementation cannot be gated as strictly as the old (no race
  detector, no coverage, a signal count that fell). Kill, or `proposed` if it was known at
  placement.

Killing a placed bet: revert the run branch to `tag_at_placement`, keep the bet branch for
the human with the reason in `note`, lead the next report with it.

## Landing

All pieces `landed`; old path removed; retired tests mapped to corpus successors in
`characterization.md`'s ledger; corpus replay green against the new implementation and red
under mutation; shadow run clean over the full corpus; journey measured interleaved on the
run branch with the bet merged, gain at least the spike's. Then merge, tag
`max-<id>-landed`, update `journeys.<journey>.current` in `run.json`, set `landed`.

## run.json additions

    "mode": "max",
    "target": { "cold-launch": 10.0, "api-p95": 3.0 },     // or { "*": 3.0 }
    "spike_threshold": 3.3,
    "kinds": ["stack","framework","design","data","wire","dependency"],
    "stop_at_target": false,
    "target_stack": null

## Recipes

    # bets in flight
    jq -s 'map(select(.status=="placed")) | .[] | {id,subsystem,journey,pieces:(.pieces|map(.status))}' .improve/bets.jsonl

    # what the spikes said, for the daily report
    jq -s 'map(select(.spike!=null)) | .[] | {id,kind,target,claimed_gain,spike_gain:.spike.gain,status}' .improve/bets.jsonl

    # has this hypothesis been tried
    jq -s --arg sub "$SUB" 'map(select(.subsystem==$sub)) | map({id,kind,target,status,note})' .improve/bets.jsonl

    # journey scoreboard against target
    jq '.target as $t | .journeys | to_entries[] | {journey:.key,
          baseline:(.value.baseline|add/length), current:(.value.current|add/length),
          gain:((.value.baseline|add/length)/(.value.current|add/length)),
          target:($t[.key] // $t["*"] // null)}' .improve/run.json
