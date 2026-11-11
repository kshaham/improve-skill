# Knowledge graph

`.improve/graph.db` is the skill's long-term memory of this repository: every run, task,
file, area, gate, blocker, and piece of knowledge, how they connect, and the links that
dreaming inferred from the evidence. It survives `--new-run` archival. Read it before work
so you do not repeat a failed approach, and write to it when you learn something a future
run would need. Nothing removed from it is lost: every removed node is kept in a history
and can be restored.

The helper is standard-library Python (SQLite). Below, `G` stands for:

```sh
python3 <skill-directory>/scripts/improve_graph.py --repo /absolute/repo
```

## What is in it

| Node    | Id form                     | Comes from                                                  |
| ------- | --------------------------- | ----------------------------------------------------------- |
| run     | `run:<run_id>`              | each supervised or foreground run                           |
| task    | `task:<run_id>:<task_id>`   | `backlog.jsonl` rows: status, claim, rejection/blocked note |
| file    | `file:<repo-relative path>` | task `paths`, `owned_paths`, `file`                         |
| area    | `area:<name>`               | task `area`                                                 |
| gate    | `gate:<name>`               | `run.json.gates` and `gate_seconds`                         |
| blocker | `blocker:<hash>`            | `blocked_reason`, grouped so numbers and SHAs do not split  |
| insight | `insight:<hash>`            | `note` (agents), the board (user), `learning.json` lessons  |

Insight kinds: `pitfall` (what fails and why), `technique` (what works), `convention`
(project rule), `decision` (settled choice, do not relitigate), `fact`, `lesson`.

Recorded links come from ledgers (`touches`, `in_area`, `in_run`, `verified_by`,
`blocked_by`, `co_changed`, `used_in`) or from you (`about`, `relates_to`, `causes`,
`fixes`, `avoids`, `depends_on`, `supersedes`). **Inferred** links come from dreaming and
always carry `confidence`, the `rule` that produced them, and the `evidence`:

| Link                          | Means                                                                        |
| ----------------------------- | ---------------------------------------------------------------------------- |
| `applies_to` insight → file   | successful tasks that cited it touched the file (rule `cited-on-file`), or a file it applies to strongly co-changes with this one (`via-co-change`) |
| `applies_to` insight → area   | most files it applies to are in that area (`files-in-area`)                  |
| `co_cited` insight ↔ insight  | successful tasks cite them together (`cited-together`)                       |
| `similar_to` insight ↔ insight| overlapping wording and a shared file, short of a duplicate (`shared-wording`) |
| `affects` blocker → file      | the blocker stopped several tasks on the file (`blocked-on-file`)            |

Each file and area carries `stats`: done/rejected/blocked attempts and a smoothed `success`
rate. Each insight carries `reinforced`, `useful`, `wrong`, `cited_done`, `cited_failed`, a
`score` that decays when nothing reinforces it, and a `verdict` (`proven` or `contested`)
once outcomes are clear.

## When to query

1. **Cycle start.** The supervisor puts `G brief` output in the prompt: the strongest
   knowledge (proven and contested flagged), files where past attempts often failed, files
   that keep failing with nothing recorded about them, gate costs, and consolidation
   proposals. Pinned and user knowledge comes first. Treat user `decision` and `convention`
   entries like intake constraints. In a foreground run, run `G sync` then `G brief` yourself.
2. **Before selecting or editing a task.** Always query its files and area:
   ```sh
   G context --files src/api/db.py src/api/pool.py --area performance --text "connection pool"
   ```
   The output lists knowledge to apply (including knowledge reached only through inferred
   links, weighted by their confidence, and knowledge cited together with it), earlier
   attempts with their rejection/blocked notes, blockers seen there, gates that verified
   them, and files usually changed together. A directory or a new file finds its neighbors'
   history. The query loads only the part of the graph it needs.
   - An earlier attempt was rejected for the same idea: do not retry it unless the evidence
     has changed. Record that new evidence in `reopens`.
   - A file has a low success rate: budget a smaller change and its strongest gate.
   - `contested` knowledge has mixed outcomes: verify it before relying on it.
3. **While investigating**, use focused lookups instead of re-reading history:
   - `G search "deadlock retry"` ranks nodes by words
   - `G node file:src/api/db.py` lists everything linked to one node, inferred links with
     their confidence, rule, and evidence
   - `G hotspots` lists files that resist change
   - `G path file:a.py area:security` shows how two nodes connect
   - `G history --query "deadlock"` lists removed nodes and why they were removed (`--offset` pages)
   `context` prints a text brief; `--toon` or `--json` prints the structure. Every other
   command prints TOON by default (`--format json` for JSON; `IMPROVE_FORMAT=json` for all
   helpers): a list of nodes is one header line and one row per node.

## When to write

- **Cite what you used.** Put the ids of insights you relied on in the task's `graph_refs`
  list in `backlog.jsonl`, for example `"graph_refs": ["insight:1c4aec93a439"]`. A done task
  credits them; a rejected task counts against them; a blocked task is neutral (a blocker
  is environmental). Citations are also the main evidence dreaming infers links from.
- **Record non-obvious knowledge** as one sentence, linked to what it is about:
  ```sh
  G note --kind pitfall --text 'db.py retry tests flake under parallel pytest; run with -p no:xdist' --about file:src/api/db.py area:resilience
  ```
  Good notes: a gate quirk, a flaky test and its fix, a hidden coupling, a dead end with the
  reason, the command that reproduces a bug. Bad notes: task status (the ledger already has
  it), generic advice, secrets, personal data. Repeating a note, or writing a close
  paraphrase, reinforces the existing node; writing a forgotten note again restores it.
- **Correct it.** `G rate ID wrong` when knowledge proved false. `G rate ID useful` when it
  saved real work no task cites. `G unlink SRC REL DST` when an inferred link is wrong; it
  is removed and never inferred again. `G link A supersedes B` when new knowledge replaces old.
- **Bring back what you need.** If knowledge you need was forgotten, `G history --query WORDS`
  finds it and `G restore ID --reason WHY` brings it back with its links, protected from
  cleanup for a while. Citing a forgotten id in `graph_refs` restores it automatically.
- **Consolidate when idle.** `G proposals` lists groups of overlapping knowledge. Write one
  stronger sentence that replaces them:
  ```sh
  G consolidate --proposal dream:1a2b3c4d5e6f --kind technique --text 'ONE BETTER SENTENCE' --consume ID ID
  ```
  Only that proposal's members are consumed; pinned and user knowledge never is. Consumed
  notes stay in history, and their ids resolve to the new note, so old citations still count.
- **At the final report,** record the one to three most useful notes. The supervisor then
  syncs and dreams automatically. In a foreground run, finish with `G sync --dream`.

## Maintenance (automatic)

The daemon runs `sync` after every cycle (live tasks, gates, blockers, lessons), refines every
`IMPROVE_GRAPH_REFINE_MINUTES` (30), dreams every `IMPROVE_GRAPH_DREAM_MINUTES` (180), and
dreams again when the run ends. Sync treats the ledger as the source of truth: a task's links
follow its current status, files, area, and `graph_refs`, and a task that leaves a readable
ledger leaves the graph. Archived runs are backfilled on first use.

**Refinement** merges near-duplicate insights (old ids keep resolving), recomputes file and
area success rates and co-change links, flags deleted files, archives knowledge contradicted
more often than confirmed or unused for many runs, prunes the oldest tasks beyond the size
bound (folding each outcome and each citation into file, area, and insight baselines
exactly once), and bounds blockers, files, knowledge, and aliases.

**Dreaming** advances the graph's tick (its clock, which moves within one long run) and:

- re-derives every inferred link from current evidence. Creating a link needs twice as many
  successes as failures and a real share of the evidence (one insight's two lucky citations
  on a busy file are noise); keeping one needs a quarter more successes than failures, so
  links do not flap. Evidence from pruned tasks is kept as counts, so age alone never erases
  a relationship. Second-generation links are built only from first-generation or recorded
  links, never from each other. Dropped links and the reason are logged.
- archives agent knowledge never validated (not cited by a success, not marked useful, not
  repeated) after 8 dreams unused, and forgets archived agent knowledge after 8 more. It
  also forgets deleted files nothing refers to and gates no task uses. User, pinned,
  learning-store, and recently restored knowledge is never cleaned up automatically: not
  by dreaming, not by refinement's fade or contradiction rules, and not by the size caps.
  An agent's `rate ID wrong` on user knowledge is recorded; only the user archives it.
- flags `proven` and `contested` knowledge, lists knowledge gaps, proposes consolidations,
  and repairs alias chains and dangling rows.

**History.** Every removal (pruned, merged, consolidated, forgotten, orphaned, capped,
vanished) is kept with the node, its links, the reason, and what it contributed to other
counts. Restoring reverses exactly that: a restored pruned task takes its folded counts back,
a restored merged note takes its merged counts back, and nothing is counted twice. Work that
returns to a forgotten file brings the file back with its statistics.

**Storage.** SQLite in WAL mode: readers never wait for writers and always see a whole
revision; writers take turns through `graph.lock`; each session writes only the rows that
changed; history is compressed and bounded; freed pages are returned to the disk. Each
refine or dream checkpoints `graph.backup.db`. A damaged or foreign `graph.db` is set aside
as `graph.damaged.db` and restored from that checkpoint (or rebuilt from the ledgers); a
missing one is restored from the checkpoint. Only real corruption counts as damage: a
constraint or programming error rolls the write back and leaves the live store intact.
A writer waits at most `IMPROVE_GRAPH_LOCK_WAIT` seconds (30) for the lock and then exits
`3`; readers never wait. Supervisor blocks (no evidence, uncommitted changes) are not
ingested as blockers; worker and agent blockers are. An older `graph.json` is migrated once. A graph
problem never stops a run. The user can see, pin, archive, correct, restore, reject inferred
links, add knowledge, and dream from the board's **Knowledge** tab; that reaches you in the
next cycle's brief.
