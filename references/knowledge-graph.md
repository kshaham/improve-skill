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
| `contradicts` insight ↔ insight | the same wording with the polarity reversed ("always X" / "never X") (`opposite-wording`); decide which holds and `rate` the other |

Each file and area carries `stats`: done/rejected/blocked attempts and a smoothed `success`
rate. Each insight carries `reinforced`, `useful`, `wrong`, `cited_done`, `cited_failed`, a
`score` that decays when nothing reinforces it, and a `verdict` (`proven` or `contested`)
once outcomes are clear. Verdicts weigh each citing task's outcome by its age (the weight
halves every 8 runs), so recent failures outweigh old successes; `proven` also needs at
least three successful tasks. Two notes count as one only when their wording overlaps and
they agree on negation, numbers, the files they name, and every content word (a note about
the read lock and one about the write lock stay apart); connectives such as "because" and
"since" do not count.

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
   - `code changed since: re-verify` means a file the knowledge was recorded against has
     different content now; check it still holds before relying on it. A done task that
     cites it re-proves it against the current code.
   - `contradicted by ID` means another note says the opposite; decide which is right and
     `rate` the wrong one.
   - A blocker marked `resolved` stopped earlier work, but later work on that file succeeded.
   - Knowledge cited by tasks that were rejected ranks below knowledge cited by successes.
   - `--exclude TASK_ID` leaves out the task you are working on.
   - When the code layer is indexed, `context` also lists the files that import or call these
     files (with the line), what they import, and the code's own `# WHY:` / `# NOTE:` /
     `# HACK:` comments (secrets removed), and the module group the file belongs to.
3. **While investigating**, use focused lookups instead of re-reading history:
   - `G search "deadlock retry"` ranks nodes by words
   - `G node file:src/api/db.py` lists everything linked to one node, inferred links with
     their confidence, rule, and evidence
   - `G hotspots` lists files that resist change
   - `G path file:a.py area:security` shows how two nodes connect, each step with its `tier`
     and `confidence` and the weakest step marked. It does not route through `in_run` /
     `in_area` links or the busiest 1% of nodes (everything is two steps from those);
     `--all` allows them and `--via REL` restricts the relations.
   - `G history --query "deadlock"` lists removed nodes and why they were removed (`--offset` pages)
   - `G impact src/api/db.py` (or `PATH::Class.method`, `pkg.mod.name`, or a bare name) lists
     the files that depend on it through imports, calls, inheritance, `uses` (imported names,
     decorators, annotations, `isinstance`/`except` classes), and `indirect` references
     (callables passed as arguments), with the line that uses it, two levels deep (`--depth`,
     at most 4), plus each dependent's success rate. A class includes its methods; a method
     includes the base-class methods it overrides (their callers may be running it). Run it
     before a change with a wide reach.
   - `G impact --changed [REV]` does the same for the change you actually made: the working
     tree (staged or not, untracked source files included) against `REV` (default `HEAD`),
     mapped onto the functions and classes the hunks touch (module-level changes count as the
     whole file; a deleted or renamed function counts as changed). It lists the changed
     symbols, every dependent, the tests among them, and the module groups touched. Use it to
     choose gates for the diff before verifying.
   - `G tests-for TARGET` (or `G tests-for --changed [REV]`) lists the test files that reach
     the target through calls, imports, or uses (pytest fixtures in `conftest.py` included),
     nearest first, each with the test functions involved and a command that runs just them
     (`python -m pytest path::test`, `python -m unittest discover ... -k`, `npx vitest run`,
     `go test ./dir`, ...). Run them first for fast feedback; they never replace the
     verification gate. An empty list means no test reaches it: run the whole suite.
   - `G code search WORDS` ranks symbols, docstrings, and code notes by identifier-aware words
     (`retry upload manager` finds `UploadManager.retry_upload`); tests and docs rank lower
     unless the words mention tests. Nothing found exits 5 with near-miss suggestions. Use it
     before sweeping files with grep.
   - `G code find NAME` (exit 4 lists candidates, 5 none) accepts `pkg.mod.Class.method`;
     `G code symbols PATH` lists a file's definitions; `G code stats` shows size, coverage by
     language, parse problems, and links by relation and tier; `G code index` refreshes the
     layer (every sync does it; `--force` prunes even when `git ls-files` shrank suspiciously).
   - `G cycles` lists import cycles of up to five files, shortest first (imports inside
     functions and `TYPE_CHECKING` blocks are left out; `--all` includes them). On a dense
     import graph the search stops at `--limit` (50) and says "at least N".
   - `G questions` lists what is worth a look: contested or contradicted knowledge, knowledge
     whose code changed, failing files with nothing recorded, bridge files, loose groups.
   - `G communities`, `G report` (Markdown; `--out FILE`), `G export --as json|graphml|markdown`,
     and `G diff` (what the latest dreams added and removed).

   `context`, `brief`, and `path` print text; `context --toon` or `--json` prints the
   structure. Every other command prints TOON by default (`--format json` for JSON;
   `IMPROVE_FORMAT=json` for all helpers). Output switches work before or after the command.
   Long text cells are cut at `--width` characters (160) and end in `…[+N]` (N characters
   cut); ids and paths never are; `--full` shows everything. `context`, `search`, and `node`
   take `--budget TOKENS`: the lowest-ranked rows go first and the first line says how many
   were left out and which budget shows everything. A `context` answer whose code section
   says `stale` was built from an index older than the latest commit; the next sync
   refreshes it.

   Exit codes: `0` an answer (or a write that was queued), `2` an error, `3` busy (a
   whole-graph write such as `restore` while another process holds the graph; retry later),
   `4` an ambiguous name (the candidates are printed), `5` nothing by that name.

## Code layer

The code layer is what the code itself says, kept in its own tables of `graph.db` beside the
knowledge (it never counts against the graph's size bounds and can be rebuilt any time).
Every sync refreshes it incrementally; an unchanged repository costs about 0.1 s.

What is read:

| Language | Read with | What it gives |
| --- | --- | --- |
| Python | `ast` | symbols with line ranges and docstrings, imports, calls, inheritance, uses, indirect references, pytest fixtures, code notes |
| JavaScript / TypeScript | regular expressions over code with comments and strings removed | top-level functions and classes with body ranges; every import form; calls through named, default, and namespace imports; tsconfig/jsconfig `paths` and `baseUrl` (comments, trailing commas, and relative `extends` handled); package.json `workspaces` names; `export ... from` barrels |
| Go, Java, Kotlin, Ruby, C, C++ | regular expressions | imports only: `go.mod` module paths to package directories, `import a.b.C` to files, `require_relative`, quoted `#include` (beside the file, then `include/` roots) |
| Rust, Swift, C#, PHP, shell, ... | not read | counted as "not indexed", never as "nothing depends on this" |

Resolution never guesses. Python's own binding rules decide what a name is where it is used:
a parameter, a local assignment, a loop, `with`, `except` or walrus target, a comprehension
variable, a nested definition, or an import (even of a third-party module) is that, and a
builtin never links to a repository symbol. A reference through an import, the same module,
`self`/`cls`, `super()`, or an instance whose class is known (`x = Client()`, `with Client()
as x`, `x: Client`, `self.client = Client()` in any method, inherited along single
inheritance) is **observed** (confidence 1.0). A name bound nowhere in its module and defined
once in the repository is **inferred** (0.85; 0.75 when keeping only tests for a test caller,
or only non-tests otherwise, makes it unique; 0.6 for the nearest test helper). A callable
passed as an argument is `indirect` (inferred, 0.6). A name that could be two things (two
classes assigned to one variable, two star imports providing it, a module name two files
share) gets no link. Imports are found from package boundaries: every directory above a
file's outermost package can be a root (`backend/app/models.py` is `app.models`); star imports
honour `__all__`; re-exports through `__init__.py` and barrels are followed up to 8 hops.
Each link counts all its call sites (`sites`) and keeps the first five lines.

Generated code (`DO NOT EDIT`, `@generated`, `Code generated by`, protobuf, Alembic and Django
migrations) is kept as an import target without its own symbols or calls; vendored and
minified code (`node_modules`, `dist`, `build`, `vendor`, `site-packages`, `.venv`,
`*.min.js`) is skipped. Both are counted in the coverage line.

When a file stops parsing mid-edit, its last good symbols and links stay (marked stale) until
it parses again: `impact` warns "does not parse now; using its last good parse" and `code
symbols` shows `stale_parse`. If `git ls-files` suddenly lists under half the indexed files
while HEAD has not moved, the index is kept rather than pruned and the sync report says why.

Answers say what they cannot see: `impact` and `tests-for` add `coverage` when some tracked
code is not indexed or does not parse, `warnings` for stale or failed parses of the files
involved, and `code_stale` when the index is older than `HEAD`. `impact` on a file in an
unsupported language exits 5 naming the language ("its dependents are unknown, not zero").
Module groups (`G communities`) include files the work has never touched when the code links
them, and an oversized group (over a quarter of all files with ten or more members, or fifty
or more with cohesion under 0.05) is split.

## When to write

- **Cite what you used.** Put the ids of insights you relied on in the task's `graph_refs`
  list, with `improve_ledger.py set ID --graph-ref insight:1c4aec93a439` (each call adds to the
  ones already recorded; `--clear-graph-refs` replaces them). A done task
  credits them; a rejected task counts against them; a blocked task is neutral (a blocker
  is environmental). Citations are also the main evidence dreaming infers links from.
- **Record non-obvious knowledge** as one sentence, linked to what it is about:
  ```sh
  G note --kind pitfall --text 'db.py retry tests flake under parallel pytest; run with -p no:xdist' --about file:src/api/db.py area:resilience
  ```
  Good notes: a gate quirk, a flaky test and its fix, a hidden coupling, a dead end with the
  reason, the command that reproduces a bug. Bad notes: task status (the ledger already has
  it), generic advice, secrets, personal data. Repeating a note, or writing a close
  paraphrase, reinforces the existing node; writing a forgotten note again restores it. Only a
  true paraphrase counts: a note that negates or reverses another ("never" for "always",
  "after" for "before"), names different numbers, or names different files is new knowledge.
  When the user rewords agent knowledge, the user's wording is kept and the agent's note
  folds into it (its id still resolves). If another process holds the graph, `note`, `link`,
  `rate`, and `unlink` are queued (`queued: true`) and applied by the next writer.
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

**Refinement** first follows file renames from `git log -M` (a moved file keeps its
statistics, links, and knowledge; the old path resolves to the new one), then merges
near-duplicate insights (old ids keep resolving; the keeper is chosen by provenance, then
pinning, then proof), recomputes file and
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
- flags `proven` and `contested` knowledge, lists knowledge gaps, proposes consolidations
  (only groups in which every note is linked to every other), infers `contradicts` between
  notes with the same wording and opposite polarity (rule `opposite-wording`), groups files
  that change together or depend on each other into module groups (stable ids, a cohesion
  score), records what it changed (`G diff`), and repairs alias chains, dangling rows, and
  rejected links whose ends were merged or renamed.

Co-changed file pairs of pruned tasks are kept as counts, like outcomes, so a co-change link
and the knowledge links built on it do not fade with age. Knowledge that `context`, `search`,
or `node` returned is touched at the next sync, so advice that is read and followed is not
archived as dormant (it still fades by score if nothing proves it).

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
A writer waits at most `IMPROVE_GRAPH_LOCK_WAIT` seconds (30) for the lock; a queueable
agent write is then queued in `graph-pending.jsonl`, any other write exits `3`; readers never
wait. The next writer renames the queue aside (`graph-pending.jsonl.<n>.draining`) before
applying it, so writes queued meanwhile go to a fresh file, and deletes it only after its own
commit; applied ids are remembered, so nothing is applied twice. Every curated write (agent
and board notes, links, ratings, rejections, restores, consolidations) is also appended to
`graph-writes.jsonl`; a checkpoint empties it. After a recovery from the checkpoint, the newer
writes are replayed in a transaction of their own that commits before any other work, and the
marker `graph.recovering` stays until that replay has committed, so a failure in between is
retried by the next writer. Earlier damaged stores are kept as `graph.damaged.<time>.db` (the
newest three). Retrievals (`graph-reads.jsonl`) drain the same way; the last two hours move to
`graph-reads.history.jsonl` for the ledger's hint. Notes are cleaned of control characters,
undecodable bytes, and anything that looks like a credential before they are stored.

**Code layer.** See the Code layer section above. Each sync refreshes it for at most
`IMPROVE_CODE_INDEX_SECONDS` (20; `0` disables) and continues where it stopped next time. Supervisor blocks (no evidence, uncommitted changes) are not
ingested as blockers; worker and agent blockers are. An older `graph.json` is migrated once. A graph
problem never stops a run. The user can see, pin, archive, correct, restore, reject inferred
links, add knowledge, and dream from the board's **Knowledge** tab; that reaches you in the
next cycle's brief.
