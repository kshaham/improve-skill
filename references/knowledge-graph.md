# Knowledge graph

`.improve/graph.json` is the skill's long-term memory of this repository: every run, task,
file, area, gate, blocker, and piece of knowledge, and how they connect. It survives
`--new-run` archival. Read it before work so you do not repeat a failed approach, and
write to it when you learn something a future run would need.

The helper is standard-library Python. Below, `G` stands for:

```sh
python3 <skill-directory>/scripts/improve_graph.py --repo /absolute/repo
```

## What is in it

| Node              | Id form                     | Comes from                                                  |
| ----------------- | --------------------------- | ----------------------------------------------------------- |
| run               | `run:<run_id>`              | each supervised or foreground run                           |
| task              | `task:<run_id>:<task_id>`   | `backlog.jsonl` rows: status, claim, rejection/blocked note |
| file              | `file:<repo-relative path>` | task `paths`, `owned_paths`, `file`                         |
| area              | `area:<name>`               | task `area`                                                 |
| gate              | `gate:<name>`               | `run.json.gates` and `gate_seconds`                         |
| blocker           | `blocker:<hash>`            | `blocked_reason`, grouped so numbers and SHAs do not split  |
| insight           | `insight:<hash>`            | `note` (agents), the board (user), `learning.json` lessons  |

Insight kinds: `pitfall` (what fails and why), `technique` (what works), `convention`
(project rule), `decision` (settled choice, do not relitigate), `fact`, `lesson`.

Structural links (`touches`, `in_area`, `in_run`, `verified_by`, `blocked_by`,
`co_changed`) are derived from ledgers. You may add `about`, `relates_to`, `causes`,
`fixes`, `avoids`, `depends_on`, `supersedes`.

Each file and area carries `stats`: done/rejected/blocked attempts and a smoothed
`success` rate. Each insight carries `reinforced`, `useful`, `wrong`, `cited_done`, and a
`score` that decays by run when nothing reinforces it.

## When to query

1. **Cycle start.** The supervisor puts `G brief` output in the prompt. That brief lists the
   strongest knowledge, files where past attempts often failed, and gate costs. Pinned and
   user knowledge comes first. Treat user `decision` and `convention` entries like intake
   constraints. In a foreground run, run `G sync` and then `G brief` yourself.
2. **Before selecting or editing a task.** Always query its files and area:
   ```sh
   G context --files src/api/db.py src/api/pool.py --area performance --text "connection pool"
   ```
   The output lists the knowledge to apply, earlier attempts on those files with their
   rejection/blocked notes, blockers seen there, gates that verified them, and files that are
   usually changed together. A directory or a new file finds its neighbors' history.
   - An earlier attempt was rejected for the same idea: do not retry it unless the evidence
     has changed. Record that new evidence in `reopens`.
   - A file has a low success rate: budget a smaller change and its strongest gate.
   - Co-changed files: read them too, and include them in the change when the coupling is real.
3. **While investigating**, use focused lookups instead of re-reading history:
   - `G search "deadlock retry"` ranks nodes by words
   - `G node file:src/api/db.py` lists everything linked to one node
   - `G hotspots` lists files that resist change
   - `G path file:a.py area:security` shows how two nodes connect
   Add `--json` to `context` when a script consumes it.

## When to write

- **Cite what you used.** Put the ids of insights you relied on in the task's `graph_refs`
  list in `backlog.jsonl`, for example `"graph_refs": ["insight:1c4aec93a439"]`. When that task
  is done, the insight gains credit. When it is rejected or blocked, the insight loses some.
  This feedback is how the graph improves.
- **Record non-obvious knowledge** as one sentence, linked to what it is about:
  ```sh
  G note --kind pitfall --text 'db.py retry tests flake under parallel pytest; run with -p no:xdist' --about file:src/api/db.py area:resilience
  ```
  Good notes: a gate quirk, a flaky test and its fix, a hidden coupling, a dead end with the
  reason, the command that reproduces a bug. Bad notes: task status (the ledger already has
  it), generic advice, secrets, personal data. Repeating a note, or writing a close paraphrase,
  reinforces the existing node instead of duplicating it.
- **Correct it.** `G rate ID wrong` when knowledge proved false. `G rate ID useful` when it
  saved real work that no task cites. `G link A supersedes B` when new knowledge replaces old.
- **At the final report,** record the one to three most useful notes. The supervisor then
  syncs and refines automatically. In a foreground run, finish with `G sync --refine`.

## Maintenance (automatic)

The daemon runs `sync` after every cycle (live tasks, gates, blockers, lessons) and
`sync --refine` when a run ends. Archived runs are backfilled on first use. Refinement is
deterministic and does the following:

- merges near-duplicate insights and keeps aliases, so old ids still resolve
- recomputes file/area success rates and rebuilds `co_changed` links from done tasks
- flags files that are no longer tracked as `stale`
- archives insights that are contradicted more often than confirmed, or unused for many runs
  (pinned ones are kept)
- prunes the oldest tasks beyond the size bound, folding their outcomes into file and area
  baselines
- logs a summary that the board shows

A damaged `graph.json` is set aside as `graph.damaged.json` and rebuilt from the ledgers. It
never stops a run. The user can see, pin, archive, correct, and add knowledge from the
board's **Knowledge** tab. That knowledge reaches you in the next cycle's brief.
