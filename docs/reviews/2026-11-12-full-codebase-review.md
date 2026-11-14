# Full codebase review and second Graphify pass

Date: 2026-11-12. Scope: the whole working tree on branch `hfi/.../oak` (origin/main `2a756ff`
plus the uncommitted 2026-11-10 work): supervisor and recovery (`improve_runtime.py`,
`improve_resilience.py`, `improve_control.py`, `improve_learning.py`, shell wrappers), the
board (`improve_board.py`, `assets/board/*`), the knowledge graph and helper output
(`improve_graph*.py`, `improve_dream.py`, `improve_code.py`, `improve_analysis.py`,
`improve_toon.py`, `improve_ledger.py`), the skill instructions and docs, the tests and CI, and
a second pass over Graphify (commit `35d1d20`, v0.9.106) for features not yet ported.

Method: five independent reviews, one per area, each told to confirm findings with probe
scripts against the real modules (on Python 3.9.6 and 3.13.3). Every high-severity finding
below was then re-run and reproduced. Nothing in the repository was modified by the review.
Probe scripts live in the session scratchpad (`runtime-probes/`, `board-probes/`,
`graph-probes/`, `graphify-probes/`, `docs-probes/`).

Status legend: **C** = confirmed by a probe, **R** = confirmed and re-run, **I** = by
inspection.

**Two fixes recorded as done in `2026-11-10-graph-toon-plan.md` are wrong or incomplete:**
B2 "queued writes are applied exactly once" (they can be lost, section 3.1) and B1 "journal
replay after recovery" (replay is rolled back if the recovering session fails, section 3.2).
Both were introduced in the 2026-11-10 work and must be fixed before that work is merged.

---

## 1. Summary of the most important problems

| # | Severity | Area | Problem | Status |
|---|---|---|---|---|
| 1 | High | Supervisor | Plain `/improve` agent writing `outcome: "target reached"` (the prompt offers it) or a corrupt `run.json` stops every cycle for the rest of the run; with a stop request it becomes an unstoppable hot loop (journal 2.5 MB in 3 s) | R |
| 2 | High | Supervisor | Relaunch after a mid-cycle crash refuses a dirty tree (exit 2), so `--supervise` gives up instead of quarantining as documented | R |
| 3 | High | Supervisor | Disk full mid-run: ENOSPC inside crash handling escapes containment and halts the run; watchdog does not relaunch | R |
| 4 | High | Graph | Queued agent writes are erased by the session that drained the queue | R |
| 5 | High | Graph | Journal replay after recovery is rolled back if the recovering session fails; sequence numbers are reused | R |
| 6 | High | Graph (security) | `safe_path` accepts `src\..\..\x`: a note can fingerprint (hash) files outside the repository | R |
| 7 | High | Graph (perf) | `--budget` re-renders the whole answer per dropped row: 65.9 s for `search --limit 1000 --budget 100` | C |
| 8 | High | Board | Cancelling a Retry or Withdraw form makes every saved draft unreadable and stops draft saving for that repo permanently (retry ids lost, duplicate requests possible) | R |
| 9 | High | Skill (max) | improve-max bet branches `improve-max/<date>/bet-<id>` cannot be created next to the run branch `improve-max/<date>` (git ref conflict); every bet fails at step 1 | R |
| 10 | High | Skill | SKILL.md says read run state only through `improve_ledger.py run`, but that view omits `preflight_complete`, `gates`, and `authorization`, which the instructions branch on | R |
| 11 | Medium | Code layer | Resolver guesses calls Python binds elsewhere (builtins like `open`, third-party imports, parameters, nested functions) and reports false dependents in `impact` | R |
| 12 | Medium | Code layer | A file with a syntax error loses all its symbols and incoming call edges until it parses again, exactly while the agent is editing it | R |
| 13 | Medium | Graph | Notes that differ in one content word ("read lock" / "write lock", "clock" / "network") still merge as duplicates | R |
| 14 | Medium | Tests | Exported `IMPROVE_FORMAT=json` breaks 7-14 tests; most daemon tests run only in strict mode, not the default resilient mode | R |


---

## 2. Supervisor, recovery, control, learning

### 2.1 High: a state error at the top of the loop stops all cycles (R)
`improve_runtime.py:1035-1037`, `:1011-1030`, `:345-357`, `:821-822`; `improve_resilience.py:28`.
`iterate()` calls `reconcile()` before the stop and deadline checks. The cycle prompt lists
`"target reached"` as a valid outcome for every skill, `repair_run` keeps it because it is in
`TERMINAL_OUTCOMES`, and `terminal_outcome` raises `StateError` unless the skill is improve-max
with `--stop-at-target`. Every iteration then crashes, cools down, parks, and repeats until the
deadline with zero launches. With `stop.request` or SIGTERM, `wait()` returns immediately and
the loop spins: the probe wrote 2.5 MB of tracebacks to `journal.md` and parked 509 times in 3
seconds, still alive; a relaunch re-enters the same loop. A corrupt `run.json` between cycles
behaves the same because `repair_state` runs only after a launch.
Fix: check `stop_requested()` first; clear unauthorized outcomes in `repair_run` (pass the
skill and args) or catch in `reconcile`; repair `run.json` at the top of each iteration; build
the prompt's outcome list per skill. Add a resilient-mode test (section 7.1).

### 2.2 High: relaunch after a mid-cycle crash refuses a dirty tree (R)
`improve_runtime.py:1331`, `:1255`. `daemon()` raises "working tree is dirty" before
`settle_stale_child()` and quarantine; exit 2 is final for the watchdog. A daemon killed by OOM,
SIGKILL, or a reboot while the agent had uncommitted edits (the normal mid-cycle state) can
never restart without a human; `docs/running.md:271-273` promises quarantine.
Fix: refuse dirty only on a first launch or `--new-run`; for an existing run without an
outcome, settle the stale child and call `quarantine_cycle(..., between=True)`.

### 2.3 High: ENOSPC halts the run (R)
`improve_runtime.py:1011-1016`, `:406-417`, `:1393-1398`. `crashed_iteration()` writes the log
and journal unguarded; the `OSError` escapes `run()`, `daemon()` records `halted:` and exits 2.
The low-disk park check runs only before a launch, so a build filling the disk mid-cycle ends
the run. Fix: suppress `OSError` inside `crashed_iteration`; treat ENOSPC as park.

### 2.4 Medium
- **Killed during the final-report limit wait, nothing can recover it** (C).
  `improve_runtime.py:981`, `:515-520`: `finalize()` saves `retry_at` past the deadline and
  `prepare()` rejects it, so relaunch, `--supervise`, `--finalize`, and `--resume` all exit 2.
  Use a separate `final_retry_at` or allow it for report-only runs.
- **`--resume --supervise` loses crash protection** (C). `:1263-1278`: the watchdog keeps
  `--resume` on relaunch; after the first crash the relaunch fails ("--resume requires a
  halted run") and the watchdog quits. Strip `--resume`/`--finalize` after the first launch.
- **Repeated worker halts end the run, docs say they park** (C via existing test
  `test_runtime.py:1156`). `:1106-1115` vs `docs/running.md:240-244,262`, `README.md:188-195`,
  `docs/workflow.md:127`. Decide which is intended; park is consistent with the rest.
- **A sleeping laptop lets a cycle run past the deadline** (I). `:872-888`: the cutoff uses the
  monotonic clock, which does not advance during sleep on macOS or Linux; after 8 h asleep the
  child keeps working up to its remaining cycle budget. Also stop at
  `time.time() >= deadline + finish_grace`.
- **Artifact quarantine commits build trees into git on every cycle** (I).
  `improve_resilience.py:244-274`: `refs/improve/quarantine/*-artifacts` keeps unignored
  `node_modules/`, `DerivedData/`, `build/` forever, growing `.git` by hundreds of MB per cycle
  over a long run. Clean artifacts without committing them, or record only the file list.
- **One malformed `block_count` stops recovery from blocking anything** (R).
  `improve_resilience.py:382`: `int("twice")` raises; the stuck task stays `in_progress` and the
  loop crashes and parks. Use `coerce_count`.
- **`--new-run` after a crash never stops the old agent** (I). `:1362-1378`, `:553-560`: the old
  `supervisor.json` (with `child_pid`) is archived before `settle_stale_child()`, so two agents
  can run on one tree.

### 2.5 Low
- A stop sent to the watchdog during relaunch backoff is lost (C): the loop relaunches after the
  sleep and the new daemon deletes `stop.request` (`:1249-1282`, `:1377`); `--stop` while the
  daemon is down does nothing.
- One lesson without `text` in `learning.json` drops the whole cycle brief (C):
  `improve_learning.py:168,173` index directly and `memory_brief()` wraps learning, graph, and
  recovery briefs in one `try` (`improve_runtime.py:849-860`).
- Shell wrappers break with `CDPATH` set or when invoked through a symlink (C on bash 3.2):
  `scripts/improve-clock.sh:4`, `improve-daemon.sh:4`, `improve-board.sh:4`. Use
  `CDPATH= cd -- ... >/dev/null` and resolve the link.
- A worker `halted:` outcome left in `run.json` across a daemon crash bypasses
  `soften_worker_halt` (`:1043`); the final-report retry only matches the limit pattern when
  `rc != 0` (`:979`); `LIMIT_PATTERN` over the 64 KiB tail can match repository output
  containing `429 Too Many Requests` and trigger a long limit wait (`:1094`).
- `repair_ledgers` rewrites `backlog.jsonl` without `ledger_lock` or fsync
  (`improve_resilience.py:461-484`); `learning.json` read-modify-write has no lock
  (`improve_learning.py:68-148`; concurrent runs share the global store); `write_rows`
  (`improve_resilience.py:47-59`) is now dead code.

Checked and sound: `atomic_json`; `ledger_lock` and `rewrite_tasks`; every daemon write of
`supervisor.json`/`run.json`/`intake.json` and control files under `operator_lock`; child
process groups (SIGTERM then SIGKILL, reaping on every path); stale-child pid-reuse guard;
limit-budget accounting across restarts; log rotation between cycles only; UTC timestamps; no
`shell=True`; repo paths `shlex.quote`d in prompts; Python 3.9 syntax and APIs.

---

## 3. Knowledge graph, code layer, helper output

### 3.1 High: queued writes erased by the session that drained them (R)
`improve_graph_journal.py:143-169`; `improve_graph.py:378`, `:396`. `drain_pending` reads the
pending file at session start; after commit `clear_drained` rewrites the whole file empty,
including rows appended meanwhile. Agent A queues, the daemon's sync drains it, the sync runs
longer than the 30 s lock wait (refine, dream, code index), agent B queues, the sync commits and
wipes B's note, which the CLI reported as queued with exit 0. Probe: second note absent, pending
file empty. Fix: at drain time `os.replace` the file to `graph-pending.draining` and delete that
after commit (new appends start a fresh file); or rewrite only rows whose ids were not applied.
The reads journal has the same read-then-rewrite race (`improve_graph_journal.py:243-268`) and
compares whole seconds, so same-second rows appended later are never applied (low).

### 3.2 High: journal replay lost when the recovering session fails (R)
`improve_graph.py:347-392`; `improve_graph_journal.py:118-132`. `recover()` installs
`graph.backup.db` at once, but the replay happens inside the caller's session transaction. If
that session raises (for example an agent `rate` on an unknown id, exit 2), the replay rolls
back; the next session sees a healthy store and never replays. `journal_seq` also returns to the
checkpoint value, so new writes reuse sequence numbers (`[1, 1]`), confusing any later recovery.
Fix: run recover + replay in its own session and commit before yielding to the caller, or keep
a persistent "replay pending" marker until a replay commits.

### 3.3 High (security): path escape through backslashes (R)
`improve_graph.py:544-547`. `safe_path` checks `..` before converting `\` to `/` and
normalizing, so `src\..\..\outside.txt` becomes `../outside.txt`. `note --about
'file:src\..\..\outside.txt'` (or a ledger `paths` entry) creates `file:../outside.txt` and
`stamp_fingerprints` hashes the file outside the repository (existence and content oracle).
Fix: convert, normalize, then reject `..` and anything starting with `../`; add a test.

### 3.4 High (performance): quadratic `--budget` (C)
`improve_graph_cli.py:99-122`. Each dropped row re-renders the whole result, and each render
runs `compact_rows`, which encodes three candidate forms of every list: 0.17 s without a budget,
65.85 s with `--budget 100` on 1000 search rows; `node area:general --budget 100` takes 1.5 s.
Fix: estimate per-row cost once and trim in one pass, or binary-search the kept count per list.

### 3.5 Medium
- **Resolver guesses** (R). `improve_code.py:507-525`: a bare name not bound by a *resolved*
  import falls through to "defined exactly once in the repository" (0.85). `open()` links to a
  repository `open`, a name from `from json import loads` links to a local `loads`, a parameter
  `callback()` and a nested `inner()` link to unrelated top-level functions; `impact` then lists
  false dependents, contrary to "never guessed". Fix: treat every import alias as bound even if
  unresolved; skip `dir(builtins)`; track parameters, assignment targets, loop/with/walrus
  targets, and nested definitions per scope; resolve star imports against the source module
  (honour `__all__`) instead of global uniqueness.
- **Inheritance edges have the wrong source** (C). `PythonFacts.visit_ClassDef` records bases with
  `self.caller()` before pushing the class, so `inherits` edges start at the module
  (`pkg/svc.py::`) instead of the class. File-level impact is unaffected; any class-hierarchy
  feature is blocked. Use `f"{path}::{qualname}"`.
- **Syntax error drops a file's symbols** (R). After a parse error every symbol and incoming call
  edge of the file disappears and `code find` exits 5, exactly while the agent is mid-edit. Keep
  the previous facts, set `error`, and say so in `impact`.
- **One-word content swaps still merge notes** (R). `duplicate()` at the 0.75 threshold: "Take
  the read lock before touching the session cache in the worker" vs "... write lock ..." scores
  0.78 and merges; "Mock the clock in billing tests ..." vs "Mock the network ..." scores 0.75 and
  merges. Graphify blocks this with a content-word swap guard (`dedup.py:_content_token_swap`):
  for equal-length notes, a position where both differing words are content words blocks the
  merge. Add the same guard; this is the same class of defect Phase A fixed.
- **`set --field id=X` rewrites a task's identity** (R). `improve_ledger.py:107-146`: `set t2
  --field id=t1` makes t2 vanish from the merged ledger and the graph prunes it as "vanished".
  Reject `id` and `created_at` in `--field`/`--json-field`.
- **Lone surrogates commit, then fail** (C). `improve_graph.py:393-396,463`;
  `improve_graph_journal.py:45`. A surrogate-escaped argv character passes `clean()`, the note
  commits, `journal.flush` raises `UnicodeEncodeError` (not caught by the post-commit
  `suppress(OSError, JournalError)`), the CLI exits 2, the agent retries (reinforced twice), the
  backup step is skipped, and every later `search` returning that node exits 2. Strip
  surrogates in `clean()`; catch `ValueError` after commit.
- **Restoring through a merge chain double-counts** (R). `improve_graph.py:1143,1188`:
  `superseded_by` is not resolved; A merged into B, B into C, restoring A gives back nothing from
  C (`useful` totals 10 instead of 5). `superseded = resolve(graph, superseded)`.
- **Code layer refreshed only every 30 minutes** (I). `sync` indexes only when refine or dream
  is due, so `impact`/`context` dependents can be a whole task behind; an unchanged index costs
  ~0.05 s, so refresh in every post-cycle sync, and print `stale: N files changed since
  indexed` on reads.
- **Communities ignore code-only files** (I). `improve_analysis.file_weights` drops files without
  a knowledge node, so never-touched files (where discovery should look) are never grouped.

### 3.6 Low
- `ledger --file` crashes on a non-list `paths` (`TypeError`, exit 1, `improve_ledger.py:73`) and
  splits a string value into characters (C). Reuse `improve_graph.task_files`.
- `cycles` is exponential on dense import graphs: 20 fully connected modules, 403,636 cycles in
  2.4 s; 30 modules, 3.6 M cycles in 31.9 s, all held in memory; `report` calls it (C). Stop
  enumerating past `limit`.
- `forget` calls `remove_nodes` once per node, O(forgotten x edges): 2.5 s for 1000 nodes over 30k
  edges (`improve_dream.py:404-405`). Group by reason.
- `acquire_lock` leaks the fd if `flock` raises in the waiter thread
  (`improve_graph.py:316-321`).
- "Oldest first" trimming of `aliases` (`:1574`) and imported `lessons` (`:902`) is really
  alphabetical because meta is stored with `sort_keys=True`; trimmed lessons are then re-imported
  every sync once over the cap. Store an insertion sequence.
- Agent notes are not passed through `improve_code.redact` before entering every cycle's brief;
  code notes are.
- `toon.flatten` silently overwrites if a row has both `stats: {done}` and a real `stats.done`
  key (theoretical: no current output has dotted keys; a 20,000-case round-trip fuzz found no
  mismatches). A truncated cell ends in `...`, which is ambiguous with real text ending in `...`.
- Nondeterminism: `board_view` focus selection orders by `list(seen)` (hash order); the 600-edge
  `co_changed` cut has no id tie-break.
- Gate shapes disagree across docs and code: `verification.md` nests gates (`go: {fast,
  batch}`), `ledger.md` keys `gate_seconds` as `go.fast`, the graph expects flat names
  (`improve_graph.py:764-773`), so nested gate costs never reach the brief.

Checked and sound: partial-session conflict detection; edge loading only between loaded nodes;
meta read inside `BEGIN IMMEDIATE`; commit/flush/backup/truncate ordering; replay not
re-journaled; alias cycle guard; history caps and compression; every meta key and side file
bounded over 72 h; damage classification and verified restore; exit-code mapping (except 3.5
surrogates and 3.6 `--file`); Python 3.9 for all graph modules; TOON round trips through the
CLI renderer; the racily-clean stat check; dream inference costs at the documented bounds.

---

## 4. Board

### 4.1 High: cancelled Retry/Withdraw forms break all saved drafts (R)
`assets/board/drafts.js:36-38`, `:124-128`, `:175-177`. `savedDrafts()` accepts only
`task, guidance, priority, decision, note`; a stored `retry` or `withdraw` row makes the whole
read fail, after which `storeDrafts()` refuses to write. Cancelling a Retry or Withdraw form
saves an empty draft of that type (the empty check covers only task/guidance/note). From then on
every poll shows "Saved drafts could not be read", Resume draft disappears, new drafts and their
retry ids are not saved (a lost response after reload can create a duplicate request), and the
bad row is never removed. Node harness with the real `drafts.js`: readable drafts 1 -> 0. Fix:
add both types, treat them as empty-means-delete, skip invalid rows one at a time, and add a
browser test.

### 4.2 Medium
- **New graph signals are computed but never shown** (C). `improve_board.py:562` calls
  `node_detail` without `repo`, so `code_changed` is never computed there; `graph.js:702-739`
  drops `code_changed`, `contradicted_by`, `resolved`, `gates`, `code`, and `groups` from the task
  dialog; `renderNodeDetail` drops link `tier`; there is no `rel-contradicts` style. The user sees
  stale or contradicted knowledge as plain "Knowledge to apply" and resolved blockers as active.
- **Every poll during a pause resends the full snapshot** (I with measured size). The ETag hashes
  `current.heartbeat_at`, which the pause loop rewrites every 5 s
  (`improve_board.py:48-49,428-438`; `improve_runtime.py:449-451`): with 1,500 tasks about 2.9 MB
  JSON (38 KiB gzipped) re-parsed per tab every ~6 s for hours. Move liveness to a small
  `/api/health` and exclude it from the hash, or stop the 5 s rewrite.

### 4.3 Low
- An already-answered request can still be withdrawn (C): `improve_control.py:249-255` never
  checks receipts, contrary to `references/board-control.md:38`.
- Quarantine restore commands are shown unquoted (`improve_board.py:543`, `health.js:339`); git
  accepts ref names containing `$(...)`, so a hostile ref name becomes a command the user pastes.
  `shlex.quote`, or list only refs matching the supervisor's pattern.
- The 64-entry ledger cache evicts oldest-first, so with more than ~12 archived runs the default
  All runs view re-parses every ledger on every poll (C).
- `GET /api/graph/history?offset=<huge>` raises `OverflowError` and drops the connection (C); graph
  GETs and POSTs lack the catch-all `/api/board` has.
- Read endpoints check Host but not `Sec-Fetch-Site`; a cross-site page can loop `/api/report`,
  which takes `operator_lock` with no timeout, contending with the worker's `pending`/`ack` (I).
- The task dialog filters its own task after `context()` ran instead of passing `exclude=`, so it
  still weights results and takes a slot (`improve_board.py:576-579`).
- A withdrawn focus request can display as the applied focus (`focus.js:47-53`).
- `gaps`, `dropped`, `counts`, and `runs` are sent with every graph load and never rendered;
  dream knowledge gaps are invisible on the board.

Checked and sound: Host check (403) and 127.0.0.1 binding; `/api/session` cross-origin refusal and
preflight; POST Origin, browser token, `Sec-Fetch-Site`; private stop token; 0600 `board.json`;
64 KiB body limit, content-type, no `Transfer-Encoding`, read timeout, deep-JSON 400; static
allowlist (`../`, `%2e%2e` -> 404); symlink refusal on ledgers and logs; strict CSP with no inline
script; no XSS sink (all untrusted text via `textContent`/`setAttribute`, `CSS.escape`, blob-only
hrefs); every endpoint and field the scripts use exists; ETag/304 countdown anchored to server
time; board graph writes idempotent and validated through the shared write path; snapshot cost
~7 ms for 1,500 tasks.

---

## 5. Skill instructions and documentation

### 5.1 High
- **improve-max bet and spike branches cannot be created** (R). `max/SKILL.md:83-84`,
  `max/references/bets.md`: `improve-max/<date>/bet-<id>` next to the run branch
  `improve-max/<date>` fails with "cannot lock ref". Use `improve-max/<date>-bet-<id>` or a
  separate `improve-max-bets/<date>/` prefix.
- **The helper rule hides required run state** (R). `SKILL.md:152-157`,
  `references/continuation.md:168`, `improve_ledger.py:35-36,192-203`: "read run state through the
  ledger helper, not the raw files", but `run` (even `--full`) omits `preflight_complete`
  (SKILL:171 branches on it), `gates` (step 4), `authorization` (continuation.md: read on every
  re-entry), `continuation`, `journeys`, `artifact_paths`. There is also no helper to write
  `run.json` or append to `discovery.jsonl`. Make `run --full` complete, include those three by
  default, and add `run-set`/`discovery-add`, or drop the "not raw" rule.
  `tests/test_ledger.py:93` asserts `gates` is absent and should change with it.
- **"Pass `--format json` to any helper" is false for the clock and board** (C).
  `SKILL.md:164-165`, prompt `improve_runtime.py:800`: `improve-clock.sh . --format json` exits 2,
  which SKILL step 0 treats as invalid state (repair or halt); `improve_board.py` also exits 2;
  `context --format json` prints text. Name the helpers precisely, or teach those parsers
  `--format`.

### 5.2 Medium
- improve-max's characterization corpus lives in ignored `.improve/corpus/`, yet committed replay
  tests read it, and `--new-run` archives it (`max/SKILL.md:116-119,171-180`,
  `improve_runtime.py:1367`). Store it in a tracked path committed with the replay test, or add it
  to the keep set.
- `queue` ignores `focus.board_areas` prefer/skip, critical-security preemption, and scope
  (`improve_ledger.py:53-62`), while `references/ledger.md:142` implies it applies them. An agent
  trusting the order starts work in skipped areas.
- Board requests needing clarification can never be resolved under the daemon:
  `references/board-control.md:26` says to leave them pending "and explain in the current
  conversation", which a daemon cycle does not have, so they are re-read every checkpoint.
- The cycle prompt contradicts itself and SKILL.md: it asks to read raw `run.json`, `backlog.jsonl`,
  `discovery.jsonl`, and `supervisor.json` (48 cycles of history: wasted tokens) and also to read
  the backlog through the helper; "persist cycle + 1" vs continuation.md "only after real work";
  its TOON grammar has drifted from SKILL.md's copy. Keep one source.
- Missing failure guidance: nothing says what to do when `improve_ledger.py` or
  `improve_control.py pending` exits 2; graph exits 4 and 5 are normal answers that could be read
  as failures.
- A baseline gate longer than the cycle limit (1 h) kills every cycle with 124 and the run never
  starts; the improve-max spike timebox (4 h) equals the default max cycle timeout (14,400 s).
  Add guidance (partial or scoped baseline, raise the timeout) and keep the spike below the
  limit.
- Docs say repeated worker halts park; code ends the run (2.4); the "only three things end a run"
  lists omit "finalization modified repository work", quarantine-save failure, strict mode, and
  `IMPROVE_PARK=0`; `IMPROVE_MAX_RECOVERIES` is described as "before halt" though the default
  parks.

### 5.3 Low
`VERSION = "1.15.0"` in `improve_runtime.py:29` vs SKILL 1.16.0; the ledger.md jq rewrite recipe
bypasses `ledger.lock`; placeholders `<skill-directory>` vs `/path/to/improve`; `intake.md:79` and
`workflow.md:46` omit focus ranking and say "impact" for `blast`; `workflow.md:119` forbids
installing tooling, SKILL.md does not; `workflow.md:128` says dirty edits need manual recovery
(the daemon quarantines); `--dry-run` fails on a dirty tree; late in an improve-max run the prompt
passes `/improve-max <remaining>s` for durations max/SKILL says should use `/improve`.

---

## 6. Second Graphify pass: features worth adding

Graphify modules read in depth: `extract.py`, `extractors/engine.py`, `extractors/resolution.py`,
`symbol_resolution.py`, `paths.py`, `affected.py`, `serve.py`, `dedup.py`, `reflect.py`,
`analyze.py`, `cluster.py`, `report.py`, `watch.py`, `hooks.py`, `prs.py`; the rest at docstring
level. All candidates below are feasible with the standard library. Size: S < 100 lines, M
100-400. Reimplement from behaviour (as the first pass did); copying code would bring
Apache-2.0 NOTICE obligations.

| Rank | Feature | Graphify source | Value for the agent | Size |
|---|---|---|---|---|
| 1 | **Change-set impact** (`impact --changed [REV]`): map `git diff -U0` hunks onto stored symbol line ranges, then impact the changed symbols; list dependents, tests, and groups touched | `hooks.py`, `watch.py:_rebuild_code`, `prs.py:compute_pr_impact` (pieces) | Choose gates for the change actually made, not one named file; feeds the hourly report | S-M |
| 2 | **Test selection**: reverse impact restricted to test files (including `conftest.py` fixtures) | `paths.py:_is_test_path`, fixture lookup in `_resolve_python_member_calls` | Directly answers SKILL.md section 4's "the test supposed to catch it" and a focused pre-gate run; present as "run first", never as a gate replacement | S |
| 3 | **Instance, inherited, and `super()` calls**: type a variable from `Cls()`, `with Cls() as c`, annotations, `self.x = Cls()`; follow single inheritance; refuse rebound ("poisoned") names | `extract.py:_resolve_python_member_calls`, `engine.py:_python_local_class_bindings` | Largest recall gap for object-oriented Python: none of `s.handle()`, `svc.handle()`, `self.ping()` (inherited), `super().hook()` produce an edge today (probe). Needs the inheritance-source fix first | M |
| 4 | **Symbol references**: `from m import name` as a file-to-symbol `uses` edge; decorators, annotations, `isinstance`/`except`, callbacks passed by name (inferred) | `engine.py` reference edges, `affected.py:DEFAULT_AFFECTED_RELATIONS` | `impact` on a decorator or annotation-only class is empty today (probe) | S-M |
| 5 | **Index freshness**: refresh every post-cycle sync and flag a stale index on reads; record the HEAD indexed | `report.py` Graph Freshness, `watch.py:_inputs_unchanged` | Every read during a cycle is current | S |
| 6 | **Keep the last good parse and a shrink guard** | `watch.py:_check_shrink` | Impact stays usable while a file is mid-edit; refuses to drop most of the index if `ls-files` suddenly shrinks | S |
| 7 | **Impact through overrides and class members**: callers of a base method reach the override; a class seed includes its methods | `affected.py:_overridden_members` | Completeness of the blast radius | S |
| 8 | **Code search**: identifier splitting (`retry_upload`, `UploadManager`), IDF weighting, test/doc demotion, one seed per query word, near-miss suggestions, `code find pkg.mod.name` qualified lookup | `serve.py:_search_tokens`, `_compute_idf`, `_query_path_weight`, `_pick_seeds`, `_near_miss_message`, `_resolve_python_qualified` | Finders locate code without a file sweep; refill is the expensive phase. Keep separate tokens so note dedup is unaffected | S-M |
| 9 | **Coverage check**: indexed coverage by language and parse failures on `impact` and `report` | `report.py` Corpus Check | An empty impact on an unindexed language must not read as "small blast radius" | S |
| 10 | **JS/TS resolution**: tsconfig `paths`/`baseUrl` (JSONC, `extends`), workspaces, named-import call edges only when the file was imported | `extractors/resolution.py:_read_tsconfig_aliases`, import-evidence gate in `extract.py` | `@/lib/x` imports and all JS calls are missing today | S-M |
| 11 | **Regex imports for more languages**: Go (`go.mod` module path), Java/Kotlin (`package`/`import`), Ruby `require_relative`, C/C++ quoted `#include`; imports only, no calls | `resolution.py:_go_import_path_for_file`, `extract.py:_jvm_package_name`, `_resolve_c_include_edges` | Impact and groups on non-Python repos | M |
| 12 | **Skip generated and vendored code**: `DO NOT EDIT`, `@generated`, protobuf, migrations; `dist`, `build`, `node_modules`, `site-packages`; keep as import targets only | `extract.py:_is_autogenerated_python`, `detect.py:_SKIP_DIRS` | Keeps "defined exactly once" honest, the 4,000-file budget useful, and finders away from generated code | S |
| 13 | **Recency-weighted verdicts**: signed outcomes with a half-life (in runs or ticks), at least two distinct successes before "proven" | `reflect.py:_decay`, `_record_node`, `_finalize_sources` | Old successes no longer outweigh recent failures in `assess` | S |
| 14 | **All call sites per edge** (`also_seen`): a `sites` count plus the first few lines | `build.py:_fold_also_seen` | Renames and signature changes need every site; today only the first line is kept | S |
| 15 | **Split oversized groups**: re-run propagation on groups over 25% of files (min 10) or of 50+ files with cohesion < 0.05 | `cluster.py:_split_community` | Label propagation tends toward one giant group | S |

Fidelity fixes to existing ports (all probe-confirmed unless noted): wrong source on `inherits`
edges; builtin, parameter, nested, and third-party names guessed (3.5); star imports guessed and
test helpers resolved to non-test files (prefer same file, then other tests, then path
proximity); hard-coded `SOURCE_ROOTS` (derive roots from package boundaries:
`backend/app/api.py: from app.models import User` produces no edge today); re-exports through
`__init__.py` (and JS `export ... from` barrels) not followed (bounded chain, refuse ambiguity);
test-path detection misses `specs/`, `FooTest.java`, `*Tests.cs`, `*_spec.rb`; a JS note sharing a
line with a URL is lost (the fragment is cut at the first `//`); the content-word swap guard for
note dedup (3.5).

Not worth porting: tree-sitter extractors, Leiden, MinHash/LSH, Jaro-Winkler and the entropy gate
(dependencies, or the wrong scale: notes are capped at 400 sentences), the MCP server and PR
triage (duplicate the CLI and board; LLM triage needs a key), git hooks, watch mode, merge driver,
and hook-guard blocking (they change the user's repository, against the guardrails; take only the
freshness idea), wiki/Obsidian/HTML/SVG exports and the global graph (the board and learning store
cover these), URL/paper/tweet ingestion and introspection pipelines, interface dispatch for typed
languages, hyperedges, `.graphifyrc` (use `run.json`), the query log (`graph-reads.jsonl` exists),
sensitive-file detection (only source extensions are read and notes are redacted), and the
benchmark harness.

Graphify caveats worth knowing: its README says queries are logged by default, the code is
opt-in; its benchmark figures have no harness in the repo; it silently falls back from Leiden to
Louvain.

---

## 7. Tests and CI

### 7.1 High
- No test covers a persistent error raised before the deadline check:
  `test_supervisor_errors_are_contained` (`test_runtime.py:1352`) only breaks `pause.json`, which is
  read after it, so section 2.1 passes the suite.
- 71 of 114 runtime tests run only in strict mode (`DaemonTests` sets `IMPROVE_MAX_RECOVERIES=0`,
  `IMPROVE_MAX_WORKER_HALTS=0`, `IMPROVE_QUARANTINE=0`, `test_runtime.py:349-350`), where `run()`
  re-raises instead of containing; engine routing, pause, intake, finalize, and limit tests never
  run in the default resilient mode, which is where 2.1 lives. Add a resilient subclass.

### 7.2 Medium
- Tests inherit the host's `IMPROVE_*` environment: with `IMPROVE_FORMAT=json` exported,
  `test_ledger` alone has 7 errors (R) and the ledger, ledger-safety, and output suites 14. Strip
  `IMPROVE_*` in `tests/support.py`.
- Wall-clock ceilings ignore `IMPROVE_TEST_SPEED_FACTOR` despite `support.py:4`:
  `test_graph_store.py:398` (0.5 s) failed at 0.95 s under load; `test_graph_server` curation tests
  timed out on fixed 10/30 s HTTP timeouts; others at `test_graph_store.py:362`, `test_board.py:457`
  and its 3/5 s timeouts, `test_control.py:91`, `test_runtime.py:898-903,1112,1121`, and the slow
  suites (`test_graph_lifecycle.py:494`, `test_graph_dream.py:663`). Wrap all in `budget()`.
- Sources of the long-standing ResourceWarnings: `test_graph_store.py:290`
  (`connect(...).execute(...).close()` closes the cursor, not the connection; 4 leaks), `:424`,
  `:465`; the `improve-test-git` TemporaryDirectory is a module global in `tests/support.py:30` with
  no cleanup (`atexit.register(_GIT_HOME.cleanup)`); browser runs leave `mkdtemp` directories.
- Tests that pass for the wrong reason: `test_queue_ranks_like_the_skill` has no `board_areas`;
  `test_control.py:438-440` expects a 0.3 s `TimeoutExpired`, which also occurs if the subprocess
  has not started; `test_runtime.py:872` (orphan check after `sleep(2.2)` with writes at 3 s);
  `test_runtime.py:1401` `assertIsNotNone(poll() or wait())` cannot fail.
- Untested: account-limit budget exhaustion; `--supervise` giving up after
  `IMPROVE_MAX_RELAUNCHES`; the `IMPROVE_FINAL_TIMEOUT` kill; custom `artifact_paths`; the
  quarantine-save failure halt; `--new-run` with a `corpus/`; bet/spike branch names; the clock's
  JSON flag; the 2026-11-10 findings in sections 3.1-3.3 (pending-file race, replay rollback,
  backslash escape). Shared class fixtures in `test_graph_server.LargeBoardGraph`,
  `test_graph_queries`, `test_graph_store`, and `test_graph_lifecycle` are mutated by curation tests
  (order dependence).
- CI (`.github/workflows/tests.yml`): only `ubuntu-latest` although macOS is the primary platform
  (fcntl, `ps -o stat=`, process groups); Python 3.9 runs only the fast suite; no linting (no
  pyflakes/ruff despite `# noqa` markers, no `node --check` for board JS); no `timeout-minutes` on
  the fast job (6 h default); Playwright unpinned and no screenshot upload on failure;
  `browser_graph.py` runs twice (it is also called from `browser_board.py`). The 3.9 TOON
  recursion failure fixed on 2026-11-10 means CI's 3.9 job on `origin/main` is expected to be red.

---

## 8. Recommended order of work

1. **Before merging the 2026-11-10 work:** 3.1 pending-file race, 3.2 replay in its own committed
   session, 3.3 backslash path escape, 3.4 budget performance, 3.5 surrogate handling, with
   regression tests for each.
2. **Long-run safety:** 2.1 (stop check first, outcome repair, per-skill prompt outcomes) with a
   resilient-mode test, 2.2 dirty relaunch, 2.3 ENOSPC, then the 2.4 recovery gaps (final retry,
   `--resume --supervise`, sleep, artifact quarantine, `block_count`, `--new-run` child).
3. **Instruction correctness:** 5.1 (bet branch names, complete `run` view, `--format` wording),
   then 5.2.
4. **Board:** 4.1 drafts, 4.2 show the new graph signals and fix pause polling.
5. **Code-layer fidelity:** resolver binding (builtins, parameters, unresolved imports, star
   imports), inheritance source, keep last good parse, content-swap note guard, refresh every sync.
6. **Graphify features in rank order:** change-set impact and test selection first (they turn the
   code layer into gate choices), then instance/inherited calls, symbol references, freshness,
   code search, coverage check, JS/TS, more languages.
7. **Tests and CI:** strip `IMPROVE_*`, scale every ceiling, fix the leaks, macOS and 3.9 slow
   jobs, lint, job timeouts.
