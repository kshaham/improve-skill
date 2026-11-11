# Architecture and code review: knowledge graph, query engine, supervisor, tests, board

Date: 2026-11-08. Branch reviewed: `hfi/.../elm` at commit 2a26c61 (clean tree).
Scope: `scripts/improve_graph.py`, `improve_graph_store.py`, `improve_dream.py`,
`improve_runtime.py`, `improve_resilience.py`, `improve_control.py`, `improve_learning.py`,
`improve_board.py`, `assets/board/*`, `tests/*`, `SKILL.md` and `references/*`.

Method: every production module was read in full. Suspected defects were confirmed with
throwaway scripts against temporary repositories (noted as **confirmed**); items marked
**by inspection** were verified by reading the code path but not executed. The full
unittest suite was run: 291 tests, all passing, 454 s, no skips.

Severity scale: **P0** ends or corrupts a long run; **P1** degrades a long run or loses
user intent; **P2** correctness/quality defect with bounded impact; **P3** improvement.

---

## 1. Executive summary

The system is well engineered for its size: durable state, atomic writes, a fair writer
lock plus WAL readers, bounded data structures, history for every removal, and a test
suite that exercises long sessions. The main risks for a 48-72 hour unattended run are
**not** in the graph engine. They are in the supervisor's exception and state-validation
boundaries, where an agent writing one malformed field, or one unexpected Python exception
type, ends the whole run instead of being repaired and routed around. Specifically:

1. Agent-written `run.json` fields (`outcome`, `last_report_hour`) that fail validation
   halt the daemon (P0, confirmed).
2. Any exception outside `(StateError, OSError)` escaping the supervisor loop kills the
   daemon process with no halt record and no restart (P0, confirmed for `UnicodeDecodeError`
   in a ledger; by inspection for `KeyError`/`TypeError`/`RuntimeError` from graph code).
3. No watchdog, no auto-restart, no stale-child check on restart (P0/P1).
4. The failure breaker (3 failures x 6 recoveries) halts a run that still has a deadline
   instead of parking it; untracked build artifacts and rate limits with exit code 0 feed
   that breaker (P1).
5. The child cycle is never told its cycle timeout, so long gates get killed, quarantined
   and blocked repeatedly (P1).
6. User-recorded knowledge (`source: user`) can be archived by refinement or by two agent
   `rate wrong` calls, contradicting the documented guarantee (P1, confirmed).

Everything else is P2/P3 and listed below by component. Test-suite and board findings are
in sections 7 and 8.

---

## 2. Knowledge graph engine (`scripts/improve_graph.py`)

### P1
- **Refinement archives user and learning knowledge.** `refine()` step 4
  (`improve_graph.py:1064-1073`) archives any unpinned, unarchived insight whose score
  falls under `FADE_SCORE` after `FADE_AFTER_RUNS` idle runs, or whose `wrong >= 2`
  exceeds `useful + cited_done`. It checks `pinned` only, not `source`, while
  `improve_dream.forget()` (`improve_dream.py:337`) and `propose()` protect
  `PROTECTED_SOURCES = ("user", "learning")`. `references/knowledge-graph.md` promises
  "User, pinned, learning-store, and recently restored knowledge is never cleaned up
  automatically." **Confirmed:** a user `decision` note idle for 20 runs was archived with
  "not used in 20 runs" and vanished from `brief()`; two agent `rate ID wrong` calls on a
  user `decision` archived it as "contradicted more often than confirmed". Since SKILL.md
  tells the agent to treat user `decision`/`convention` like intake constraints, this lets
  the agent silently override steering.
- **Size cap can remove user knowledge.** Step 5 (`improve_graph.py:1084-1091`) caps
  insights/files/blockers and exempts only `pinned` nodes. A user note with a lower score
  than agent notes is removed to history ("capped"). By inspection.
- **Learning-store churn.** `import_lessons()` (`:663`) re-adds every `learning.json`
  lesson on every sync with `reinforce=False`. A capped or archived lesson insight is
  therefore re-created or recalled on the next sync, growing `history` and defeating the
  cap. By inspection.

### P2
- **`load()` repair path is indirect.** `load(path, repair=True)` (`:164-168`) performs
  recovery by opening and immediately closing a `locked()` session so `session()` runs
  `recover()`. It works, but a read path acquiring the writer lock and mutating files is
  surprising and untested in isolation.
- **Gate attribution is substring matching.** `ingest_run()` links a task to a gate when
  the gate's name appears anywhere in `json.dumps(verification)` and `len(name) > 2`
  (`:627-630`). Gate names like `test`, `lint`, `build` match nearly every verification
  blob, inflating `verified_by` edges and `uses` counts.
- **Supervisor blocker noise.** Supervisor-written `blocked_reason` strings
  ("3 consecutive cycles failed or recorded no new evidence", "Cycle left uncommitted,
  unverified changes; saved to refs/...") are ingested as `blocker` nodes and inferred
  into `affects` links, polluting the knowledge graph with environmental events.
- **Co-change edges rewritten every refine.** `refine()` deletes all `co_changed` edges and
  recreates them (`:1016-1029`), so every edge gets a new `created_at` and is rewritten in
  SQLite each refine even when unchanged. Harmless but wasteful write churn; also loses
  `created_at` history of the pair.
- **`context()` validates arguments after doing the work** (`:1246-1247`). Cosmetic.
- **No schema/version check on load.** `VERSION = 1` is written to meta but never compared;
  there is no migration hook for future node shapes. `fill_defaults()` silently drops
  nodes/edges it does not recognise, so a downgrade loses data without a warning.
- **Two clocks.** Insight scoring uses `last_seq` (runs); dreaming uses `last_tick`
  (dreams). Documented, but `restore_node()` and `rate()` set `restored_seq`/`protected_until_tick`
  on different clocks, and refinement's fade ignores `protected_until_tick`. A restored
  node is protected from refine only because `restored_seq` resets `idle`.

### P3
- `search()` is bag-of-words only: no stemming, no phrase match, `exact` bonus only when
  the whole query is a substring of the label. Fine for ids and file names, weak for prose.
- `adjacency()` is rebuilt per query (sorted over all edges). Acceptable at current caps.
- `blocker_key()` duplicates `improve_learning.blocker_key()`; keep one.

## 3. SQLite store (`scripts/improve_graph_store.py`)

### P1
- **Non-corruption errors are classified as damage.** `is_damage()` (`:58-61`) returns
  True for every `sqlite3.DatabaseError` subclass except `OperationalError`, including
  `IntegrityError` and `ProgrammingError` (**confirmed**). On such an error, `session()`
  calls `recover()`, which **moves `graph.db` aside and restores the last refine/dream
  checkpoint**, discarding every write since (up to 30 minutes of sync data, longer when
  refine is disabled). A plain code bug (e.g. a constraint violation) therefore causes
  data loss rather than a rollback.

### P2
- **Writer lock has no timeout.** `writer_queue()` (`improve_graph.py:245-256`) takes
  `flock(LOCK_EX)` with no deadline. An agent CLI `note`/`link`/`rate` blocks forever behind
  a hung daemon dream; the child cycle then dies by cycle timeout and is counted as a
  failure. A bounded wait with a clear error would let the agent continue.
- **Write transaction spans subprocess calls.** `sync()` holds `BEGIN IMMEDIATE` while
  `refine()` runs `git ls-files` (20 s timeout) and while dreaming. Readers are unaffected
  (WAL), but every other writer waits.
- **Stale WAL after restore.** `session()` restores `graph.backup.db` when `graph.db` is
  missing but does not remove a leftover `graph.db-wal`/`-shm`; SQLite would replay an
  unrelated WAL onto the restored file. `set_aside()` handles the damaged path, not the
  missing path. Edge case.
- `open()` runs `PRAGMA auto_vacuum=INCREMENTAL` on every open; harmless but a no-op after
  creation and misleading.
- `fts_usable()`/`fts_ready()` run a query on every search; fine, but `index()` rebuilds
  the whole FTS index on each save until `fts_ready` is set.

### P3
- `MAX_HISTORY = 20000` trimming is by `seq` only; restored rows are never purged.
- `backup()` copies the whole database on every refine/dream (bounded, acceptable).

## 4. Dreaming (`scripts/improve_dream.py`)

### P2
- **First sync always dreams.** `dream_due()` and `refine_due()` return True when
  `last_at` is missing (**confirmed**), so the first sync of any graph refines and dreams
  immediately rather than after the configured interval.
- **Determinism depends on dict order from SQLite.** `infer()` iterates
  `edges.values()` in load order; results are keyed so output is order-independent except
  where lists are truncated (`MAX_EVIDENCE`, `MAX_CITERS`, `via[:12]`), which are sorted.
  OK today; fragile if future rules truncate unsorted lists.
- **Blocker evidence never fails.** `affects` uses `failures_count=False`, so a blocker that
  stopped two tasks on a file is permanent until the tasks are pruned and retained counts
  remain. There is no decay for environmental blockers that were since fixed.
- `forget()` sets `last_tick` on legacy nodes at first dream, so idle counts start from
  the first dream, not from real age. Acceptable.

### P3
- `MAX_DREAM_LOG = 30` entries x report size are stored in meta and returned in every
  `board_view()`; bounded.

## 5. Query engine and CLI (`improve_graph.py` queries, `brief`, `context`, board views)

### P2
- **Partial `context` can load most of the graph.** `load_context()` fetches every
  neighbour of an `area:` seed (all tasks in the area) and their blockers/gates/insights.
  For a graph dominated by one area this approaches a full load, negating the partial
  session's purpose. Consider capping per-seed neighbours by score.
- **`brief()` hides learning lessons by design** but `guidance()` is capped at 1800 chars and
  the graph brief at 1800 chars, so a prompt carries at most ~3.6 KB of memory. That is
  reasonable; however `brief()` truncates mid-list with `rsplit("\n", 1)` and gives no
  indication that items were dropped.
- **`print_context()` drops `seeds` and `stale` hints** for files with no stats; a new file
  gets "The graph has nothing on these yet" even when sibling files had history (seeds were
  found but `files` only lists seeds with `stats`).
- **CLI exit codes.** All graph CLI errors exit 2 with a message; the agent cannot tell a
  transient lock/busy error from bad input. Distinguish retryable (lock, busy) from fatal.
- **`history` CLI has no `--offset`**; the board has paging, the agent does not.

### P3
- `node_detail()` ranks links by neighbour score, so for a task the strongest files appear
  first; good. `edge_evidence()` shows at most 5 tasks; fine.
- `board_view()` recomputes `counts` over all nodes and `hotspots()` on every poll.

## 6. Supervisor and long-run autonomy (`improve_runtime.py`, `improve_resilience.py`, `SKILL.md`)

### P0
- **Malformed agent-written `run.json` halts the run.** `reconcile()` (`:553-570`) calls
  `terminal_outcome()` which raises `StateError` for any `outcome` that is not null, a
  string in the accepted set, or `halted:...` (**confirmed** for `"done"`, `"finished"`,
  `"completed early"`, `"Halted: ..."`, `false`, an object). `timing()` raises for
  `last_report_hour` of `3.0`, `"3"`, `-1`, `null` (**confirmed**). Both propagate from
  `Supervisor.run()` to `daemon()` which records `halted: <message>` and exits. The
  supervisor already repairs premature `completed` and reset deadlines; it should repair
  these too (coerce or null the field, journal it, continue), exactly as `repair_state()`
  does for a bad `cycle`.
- **Unexpected exception types crash the daemon without a halt record.** `daemon()`
  (`:1016`) and `main()` (`:1062`) catch only `(StateError, OSError)`. `Supervisor.sync_graph()`
  catches `(StateError, OSError, ValueError, RecursionError, sqlite3.Error)`; `learn()` and
  `memory_brief()` catch fewer. **Confirmed:** a ledger line containing invalid UTF-8 makes
  `evidence_rows()` (`:256`) raise `UnicodeDecodeError` out of `snapshot()`, which is not
  caught anywhere, so the process dies with a traceback, the lock is released, `supervisor.json`
  still says `phase: working`, and the board shows "daemon not running". By inspection the
  same applies to `KeyError`/`TypeError`/`RuntimeError`/`subprocess.TimeoutExpired`
  escaping graph sync, `block_active_item`, or `quarantine_changes`. Recommendation: a
  top-level `except Exception` around each loop iteration that journals the traceback,
  counts it as a failed cycle, and continues; and `except BaseException` at `daemon()` that
  records `halted:` before re-raising.
- **No watchdog or restart path.** If the daemon process dies (crash above, OOM, reboot,
  `kill -9`), nothing restarts it; a 72-hour run silently stops. Provide a launchd/systemd
  template or a `--supervise` wrapper that relaunches with `--resume` semantics until the
  saved deadline, and have `--status` flag "lock free but phase=working" as "crashed".

### P1
- **Stale child on restart.** `prepare()`/`run()` never check `supervisor.json.child_pid`.
  After a daemon crash the `claude`/`codex` child (own session, `start_new_session=True`)
  keeps running and editing; a restarted daemon launches a second agent concurrently, and
  the first one's edits later trip `dirty()` or get quarantined. On start, if `child_pid`
  is alive and its command line is the saved engine, either wait for it or terminate its
  process group before launching.
- **Breaker halts a run that still has time.** `IMPROVE_MAX_FAILURES=3` x
  `IMPROVE_MAX_RECOVERIES=6` ends the run after 18 non-advancing cycles plus cooldowns
  (`:828-834`). For a 72-hour instruction a persistent but temporary environment problem
  (CI runner down, simulator broken, network out) should park the run with long backoff
  and keep retrying until the deadline, not `halted:`. Make the terminal halt opt-in or
  scale limits with duration.
- **Rate limit with exit code 0 is misclassified.** `LIMIT_PATTERN` is consulted only when
  `rc != 0` (`:876`). If the CLI prints a limit message and exits 0, the cycle counts as
  "no new evidence", the active task is wrongly blocked at the third failure, and the
  recovery/halt breaker runs instead of the limit wait. Check the pattern regardless of
  exit code, and treat a matched limit as neither failure nor progress.
- **Limit wait budget is absolute.** `IMPROVE_LIMIT_WAIT_BUDGET` defaults to 4 hours total
  for the whole run. A 72-hour run crossing several 5-hour usage windows can exhaust it and
  end with `halted: account-limit wait budget exhausted`. Default the budget to a fraction
  of the duration (or per-24h), and journal remaining budget.
- **Child is never told its cycle budget.** `prompt()` (`:628-681`) gives the remaining
  run time but not `IMPROVE_CYCLE_TIMEOUT` (3600 s) or `finish_grace`. A cycle that runs a
  40-minute gate twice is killed at 60 minutes with `rc=124`, its edits quarantined and the
  task blocked, and the same happens next cycle. Put the per-cycle budget and the kill
  time in the prompt and tell the agent to checkpoint before it.
- **Untracked artifacts poison every cycle.** `dirty()` includes untracked files; a repo
  whose tests emit non-ignored artifacts (coverage files, `.xcresult`, `DerivedData`,
  `__pycache__` without a `.gitignore`) is quarantined every cycle, each quarantine blocks
  the active task and counts as a failure (`:890`), so the breaker trips after ~18 cycles.
  Distinguish "untracked only" from "modified tracked" dirt, let preflight record an
  artifact allowlist, or let the agent commit a `.gitignore` fix as SKILL.md already permits.
- **Supervisor breaks its own ledger contract.** `block_active_item()`
  (`improve_resilience.py:96-101`) appends a second row for the task id with
  `note = reason`, overwriting the original rejection/evidence note on merge and leaving a
  stale `in_progress` row visible to any reader that does not merge by id (the jq recipes
  in `references/ledger.md` do not). The supervisor has exclusive access between cycles and
  should rewrite the ledger atomically with one canonical row, preserving `note` in a
  separate `supervisor_note` field.
- **User knowledge archival** (see section 2) also matters here: the brief fed to each
  cycle drops archived nodes, so a user's standing decision can disappear mid-run.

### P2
- **Recovery blocks a task for environmental failure.** `recover()` (`:495-514`) blocks the
  active/in-progress task whenever three cycles fail, even when the cause is a CLI crash,
  network error, or rate limit. The block reason becomes a graph blocker (section 2).
- **Quarantine deletes untracked files.** `quarantine_changes()` runs
  `git clean -fdq -e .improve` after committing to a ref (`improve_resilience.py:79`).
  The content is recoverable from the ref, but build caches and tool state are deleted
  every time, slowing the next cycle.
- **Pause and waits poll the filesystem 4-10 times per second** (`:403`, `:600-601`), and
  the paused heartbeat rewrites `supervisor.json` with fsync every 5 s for the whole pause.
  Use a 1-2 s poll; it is cooperative anyway.
- **No log rotation.** `daemon.log` receives every child's full stdout; `journal.md` and
  `backlog.jsonl` grow unbounded. A 72-hour run can write hundreds of MB. Rotate
  `daemon.log` per cycle or cap it.
- **Finalization after rate limit leaves `summary_pending`.** `finalize()` makes one
  300 s attempt; if it hits a limit the run ends with exit 0 and no report, requiring a
  manual `--finalize`. Retry finalization after the limit wait while time budget allows.
- **Worker halt threshold is absolute.** `IMPROVE_MAX_WORKER_HALTS=2` consecutive worker
  halts end the run. For multi-day runs this should scale or convert to a long park.
- **`snapshot()` runs `git rev-parse` and parses ledgers before and after every cycle**;
  any `git` failure (lock file, disk full) raises `StateError` and halts the run rather
  than counting a failed cycle.
- **`advanced()` requires the agent to increment `cycle`.** A cycle that commits verified
  work but forgets to bump `cycle` counts as a failure. Treat a new commit or new evidence
  as progress regardless of the counter.
- **Deadline clock trusts wall time.** A backwards NTP/DST correction making `started_at`
  later than now raises in `timing()` and halts. Tolerate small negative elapsed values.
- **No disk-space preflight.** The changelog records a run lost to ENOSPC; `atomic_json`
  raising `OSError` still halts the run. Check free space at preflight and before refills.

### P3
- `prompt()` embeds absolute script paths; fine, but a moved checkout mid-run breaks the
  next cycle. Record the skill root in `supervisor.json` and warn if it changed.
- `wait()` sleeps in 0.25 s slices; fine.
- Quarantine refs accumulate forever; add a `--prune-quarantine` or list them in `--status`.
- Machine sleep is a documented gap; the daemon could call `caffeinate -i` on macOS while a
  child is running.

### SKILL.md and references (agent-side autonomy)
- The written contract is strong on "mark blocked and move on", but the agent has no
  numeric guardrails for its own cycle: no per-cycle item cap, no cycle time budget, no
  rule for when a single gate run is too slow for the daemon's timeout. Add a "cycle budget"
  paragraph that references the supervisor's timeout.
- `ledger.md` says writers keep one canonical row per id, but the supervisor appends rows
  (above); the jq recipes shown to the agent do not merge duplicates, so the agent's own
  queue queries can double count after a supervisor block.
- `continuation.md` tells the daemon child never to sleep; the daemon's `gap` is 2 s. For
  a dry 72-hour run that means a model invocation every ~2 s of idle after a fast cycle.
  Idle cycles should back off (e.g. to 5-15 minutes) when the last N cycles produced only
  empty scans, to control cost without halting.
- No instruction exists for what the agent should do when the graph CLI itself fails
  (exit 2); the prompt should say "a graph error is never a reason to block a task".

---

## 7. Test suite

Result of `python3 -B -m unittest discover -s tests -v`: 291 tests, OK, 454 s, 0 skipped.
No CI configuration exists in the repository (no `.github/`, GitLab, or CircleCI files), so
nothing runs the suite or the browser scripts automatically.

### 7.1 Untested long-run scenarios (the most important gap)

| Scenario | Tested? | Severity |
|---|---|---|
| Rate-limit message with exit code 0; any `LIMIT_PATTERN` alternative other than "usage limit reached" | No (`FAKE_MODE=limit` exits 1, `tests/test_runtime.py:215-217`) | P0 |
| Repeated rate limits: doubling, `IMPROVE_LIMIT_WAIT_CAP`, budget exhaustion halt (`improve_runtime.py:878-886`) | No; only one limit then a 2 s deadline (`:803-808`) | P0 |
| Agent writes malformed `outcome` / `last_report_hour` / `cycle` (valid JSON, wrong type or value) at daemon level | No (unit tests of `terminal_outcome`/`timing` only) | P0 |
| Ledger with invalid UTF-8 (reproduced crash, section 6) | No | P0 |
| Unexpected exception type escaping the loop (`KeyError`, `TypeError`, `UnicodeDecodeError`) | No; only the `StateError` path | P0 |
| Repeated quarantine (dirty every cycle) and quarantine failure (`:858-859`) | No; `once_dirty` tests exactly one | P1 |
| `graph.lock` held by a hung writer; foreign SQLite writer holding a 60 s busy wait | No | P1 |
| Daemon SIGKILL mid-cycle, restart with stale `child_pid`, orphan child still editing | No | P1 |
| SIGTERM/SIGINT delivered to the daemon (launchd/systemd stop) | No; stops always via `stop.request` | P1 |
| Finalization hang past `IMPROVE_FINAL_TIMEOUT` | No | P2 |
| Pause lasting hours: heartbeat refresh, pause during recovery cooldown | Partially (pause spanning deadline only) | P2 |
| Clock jumps forward/backward; sleep/resume divergence of monotonic vs wall clock | No | P2 |
| Agent deletes `run.json`/`supervisor.json`, edits `supervisor.json`, leaves `.git/index.lock` or a mid-rebase state | No | P2 |
| Disk full in `atomic_json` (second `OSError` escapes the handler) | No | P2 |
| `daemon.log`/`journal.md` growth; board death mid-run; many `history/` archives on the board | No | P3 |

### 7.2 Coverage gaps by module

- **`improve_runtime.py`:** every daemon test sets `IMPROVE_RECOVERY_COOLDOWN=0`, so the
  cooldown/backoff/`phase: recovering` path (`:510-514`) never executes; `prepare()`
  validation branches for `--resume`/`--finalize` misuse, non-finite counters, `retry_at`
  past deadline, and `limit_wait_started_at` ordering are unasserted; `quarantine_cycle`
  with a claimed `completed`, `quarantine_changes` failing, `launch()` `OSError`, the final
  timeout kill, the 64 KiB tail, the `learn()`/`sync_graph()`/`memory_brief()` error
  branches, `daemon()` entry errors (dirty at launch, not a repo root, `--new-run` without
  intake), `--status` text output, `--version`, `--board-allow-host` are untested.
- **`improve_resilience.py`:** `block_active_item` fallback to the last `in_progress`
  task, `clear_worker_halt` without `run.json`, `repair_ledgers` on `discovery.jsonl`/
  `bets.jsonl`/symlinks, `quarantine_changes` with binary files or nested repos.
- **`improve_graph.py`:** CLI subcommands `unlink`, `proposals`, `consolidate` have no
  test although the cycle prompt instructs agents to use `unlink`; `sync --history`,
  `note --about gate:NAME`, `history --id/--type`, the 40-gate cap, `import_lessons` over
  `MAX_INSIGHTS`, and the `load()` repair branch are untested.
- **`improve_graph_store.py`:** busy-timeout behaviour against a foreign writer, schema
  upgrade from an older version, `history_count`/`forgotten_insights`/`size_bytes`.
- **`improve_dream.py`:** `dream_due` directly, `proposal_view`, `MAX_EVIDENCE`/`MAX_CITERS`
  truncation, forgetting of protected gates/files.
- **`improve_control.py`:** 8 MiB `read_local` limit, "ledger is full" paths, `acknowledge`
  CLI error cases, `pause_requested` with a non-bool `paused`.
- **`improve_board.py`:** foreground `serve()` with signal handlers, `port_number`, the
  32 MiB `load_file` limit, instance mismatch on the saved port, missing asset file
  (unhandled in `do_GET`), `Transfer-Encoding` rejection, 12 of 15 static assets never
  fetched by unittest.
- **`improve_learning.py`:** `board_summary`, `guidance`/`summary` CLI, `MAX_RUNS`/
  `MAX_LESSONS`/`MAX_BLOCKERS` caps, non-dict JSON store.

### 7.3 Test quality and CI reliability

- **Machine-speed assertions run in default discovery** with no opt-out: `test_graph_performance.py`
  (median CLI < 400 ms, RSS < 90 MB, relative ratios), `test_graph_server.py:96-311`,
  `test_graph_lifecycle.py:376-513`, `test_graph_store.py:360,396`, `test_graph_dream.py:660`,
  `test_runtime.py:947` (`ended_at - deadline < 7` s). These will flake on a loaded CI
  runner. P1.
- **Soak and scale fixtures are not separable**: 288-cycle and 432-cycle soaks, 3,000-file
  repos and 6,000-task graphs all run by default; env vars only lengthen them. P1.
- **Hard 15 s subprocess timeout** on every daemon `invoke()` (`test_runtime.py:342`) while
  some tests perform 4-6 full cycles each spawning Python plus git. P1.
- **Git global config is not isolated** (no `HOME`/`GIT_CONFIG_GLOBAL`/`GIT_CONFIG_NOSYSTEM`);
  `commit.gpgsign`, `core.hooksPath` or `init.templateDir` on the host breaks fixtures and
  `quarantine_changes`. P2.
- **FTS5 assumed present**: `test_graph_store.py:188-199` fails rather than skips without it. P2.
- **Real port 8765 is bound** by `test_board.py:226`; collides with a live board or parallel
  runs. P2.
- **Shared mutable class fixtures with order dependence** in `GraphCurationTests`,
  `DreamEndpointTests`, `LongRunScale`; `LargeBoardGraph.setUpClass` rebuilt by 5 subclasses,
  `RichGraph` by 6. P2.
- **`tests/test_runtime.py:1058` places `if __name__ == "__main__"` before
  `class ResilienceTests` (`:1062`)**, so running the file directly skips that class. P2.
- `ResilienceTests` inherits `DaemonTests` and nulls every inherited test; obscures counts. P3.
- `test_runtime.py` imports `improve_runtime.py` twice under two module names, so
  `StateError` identities differ between them. P3.
- **Weak assertions**: `assertTrue(data["warnings"])`, `assertTrue(message)`,
  `test_graph_queries.py:135` compares a call with itself, several daemon tests check only
  the exit code and one field; journal/log messages are never asserted. P3.
- Duplicated fixtures: temp repo creation in 5 places, HTTP helpers in 4 files,
  `IMPROVE_LEARNING_HOME` mutation in 3 places. P3.

### 7.4 Browser tests

- `browser_*.py` are not discovered by unittest and nothing runs them; ~1,600 lines of UI
  assertions and 12 JS assets are verified only by hand. Default Chrome path is the macOS
  bundle; elsewhere `--chrome` or a Playwright Chromium install is required. P1.
- `browser_board.py` runs seven checks sequentially against one page; the first failure
  aborts the rest, there is no per-check report and no failure screenshot. P2.
- Timing-based waits (`wait_for_timeout(100)`, `time.sleep(3.5)`, frame time < 120 ms). P2.
- Promised in docs but not covered: voice "pause the run"/"resume", the fifty-draft cap,
  the HTTPS tunnel flow end to end, column-layout sorting. P3.

### 7.5 Fake CLI realism (`tests/test_runtime.py:152-255`)

The fake `claude`/`codex` script selects one `FAKE_MODE` per run. It cannot express
multi-cycle sequences ("limit, success, limit", "dirty every cycle"), exit codes other than
0/1, a limit message with exit 0, output over 64 KiB, invalid UTF-8 output, slow SIGTERM
handling inside the grace window, hanging only during finalization, torn `run.json` writes,
wrong field types, real branch/commit activity on tracked files, or use of the helper CLIs
(`improve_control.py pending/ack`, `improve_graph.py`, `improve_learning.py`) that a real
agent is instructed to call. P1 for a suite whose purpose is unattended resilience.

---

## 8. Task board web application

Overall the frontend escapes everything through `textContent`, handles stale-run identity
and idempotent retries carefully, and has no listener accumulation or unbounded in-memory
growth. The weaknesses for a multi-day run are poll cost on both sides, the way any slow
response disables all controls, and the missing operational surface.

### 8.1 Frontend bugs and correctness
- **P1: Any slow or aborted poll disables every control.** `refresh()` aborts after 7 s
  (`assets/board/board.js:656-657`) and any error sets `connected = false` (`:681`);
  `controls.js:85-107`, `drafts.js:99,112`, `focus.js:166-171`, `voice.js:185-186` then
  disable pause/stop/requests/notes/focus/voice. A large `run=all` snapshot or lock
  contention with the worker (8.2) produces exactly this, so the board flaps and the
  operator cannot act during the flap.
- **P2: `await refresh()` after an action is a no-op while a poll is in flight**
  (`board.js:651`), so `submitRequest`, `controlRun`, `saveFocus`, `sendVoice` switch to a
  Requests list that does not yet contain the new request.
- **P2: Task dialog rebuilt every poll** (`board.js:644-648`, `:112-129`); selection and
  scroll inside long evidence blocks reset every 3 s. The thread section signature-checks,
  `#detail-content` does not.
- **P2: Countdown uses the browser clock** (`board.js:496,501`) although the server sends
  `clock.remaining_seconds`; skewed phones show the wrong deadline state.
- **P2: Whole-collection `JSON.stringify` signatures on every poll** (`board.js:290,336,367`,
  `workflow.js:72`, `controls.js:181`, `triage.js:115`, `overview.js:232`, `mobile.js:90`)
  over tasks that carry `evidence`/`verification`/`measurement`; megabytes of string work
  every 3 s for days.
- **P2: `filteredTasks()` evaluated several times per render** and `JSON.stringify(task)`
  per task per keystroke when searching (`board.js:58,375`, `triage.js:53-55`, `mobile.js:82`).
- **P2: Columns layout is unpaginated**; every active task becomes a card and all columns
  are rebuilt on any change (`board.js:301-308`).
- **P3:** string-compared server errors (`board.js:665`, `graph.js:163`); non-JSON error
  bodies surface as parser noise (`board.js:663`, `controls.js:32`); bookmark fallback
  drops the rest of the bookmark (`board.js:666-672`); `openGraphTask` resets run selector,
  selection and paging silently (`graph.js:629-631`); drafts re-parsed from localStorage
  every poll (`drafts.js:17-71,95`); `history.replaceState` on every render (Safari limit
  of 100 per 30 s, `board.js:464`); `/api/graph/context` refetched on every graph revision
  while a dialog is open (`graph.js:676-677`); unsaved focus edits survive a run change
  (`focus.js:21-24`); hands-free voice defaults on and auto-starts the microphone
  (`voice.js:318,329`); Shift+V tests `event.key === "V"` (layout-dependent).

### 8.2 Server-side issues affecting the UI (`scripts/improve_board.py`)
- **P1: `/api/board` is fully recomputed per request under the cross-process operator
  lock** (`:505-512`): reads every `history/` directory's `run.json`/`supervisor.json` even
  for `run=current`, parses all JSONL ledgers (`:233-239`), requests and receipts, learning,
  and opens SQLite for graph counters, with no mtime cache, no `ETag`/304 (`updated_at:
  stamp()` makes every body differ), `Cache-Control: no-store`, no compression and no
  delta. Multiplied by tabs x every 3 s x 72 h.
- **P1: The blocking `operator_lock` serialises all tabs with each other and with the
  worker's `pending`/`ack` and new-run archival** (`improve_control.py:99-109`, `:273`,
  `:308`). A slow snapshot delays acknowledgements; a worker hold stalls tabs past the 7 s
  abort. `ThreadingHTTPServer` has no thread cap and aborted clients do not cancel handlers.
- **P1: An uncaught `sqlite3.OperationalError` drops `/api/board` for every tab.**
  `graph_summary` (`:310-313`) and the handler (`:511`) catch `(StateError, OSError,
  ValueError)`; `improve_graph.load()` re-raises non-damage SQLite errors such as
  "database is locked" (`improve_graph.py:161-163`). The exception escapes `do_GET`, a
  traceback goes to `board.log`, and the browser sees a network error. The graph endpoints
  do catch `sqlite3.Error` (`:500`).
- **P2:** `/api/health` is identity-only (no snapshot age, heartbeat age, daemon liveness);
  `cycle_started_at`, `child_pid`, `quarantines`, `recoveries_total`, `limit_waited_seconds`,
  `next_limit_wait`, `ended_at` exist in `supervisor.json` but are not exposed (`:212-222`);
  `learning_view` and `graph_summary` computed on every poll though only two tabs need them.
- **P3:** framework error pages are HTML without the security headers; static assets read
  from disk with `no-store` on every request; module-level `improve_graph._cache` mutated
  from handler threads without a lock (benign under the GIL, but concurrent polls after a
  revision bump each do a full load); GET sockets have no timeout; a missing asset file
  raises unhandled in `do_GET` (`:530`).

### 8.3 Missing features for a 48-72 hour unattended run (verified absent)
- **P1: Heartbeat and cycle age with thresholds, stuck-cycle detection.** The UI prints
  absolute checkpoint times (`board.js:516-523`) with no staleness colour or alert, and the
  fields needed to tell "long cycle" from "hung child" are not served (8.2).
- **P1: Browser notifications** (or favicon badge, or sound) on halt, stop, completion,
  deadline, limit wait, failure streak, new blocked/proposed task, request applied/declined.
  No `Notification` API use anywhere.
- **P1: Failure-trend visibility**: only instantaneous `consecutive_failures` and
  `last_error` (`board.js:539-544`); no per-cycle timeline, durations, quarantine count,
  recoveries, limit time waited.
- **P2:** no view of `journal.md` hourly reports (only `final-report.md` is served); no
  bounded `daemon.log` tail; no list of `refs/improve/quarantine/*`; no "unblock/retry/
  re-queue" request type for blocked tasks (`REQUEST_TYPES`, `improve_control.py:19`); no
  way to withdraw a pending request; no dark mode (`board.css` has no
  `prefers-color-scheme`); disconnected state shows no data age and does not back off;
  tunnel mode (`--allow-host`) has no passphrase/token/read-only mode.
- **P3:** no file or commit facet in search; commits are 7-char text with no link or copy;
  only two keyboard shortcuts; JSON-only export; no index of boards across repositories;
  token-set graph search (no prefix/fuzzy); "Updated" shows a time, not an age.

### 8.4 Accessibility and responsiveness
- **P2:** focus is lost when the task dialog closes because the opener card is recreated by
  the next poll (`board.js:276-295,747-757`); secondary text contrast below AA
  (`--muted #747987` on `#fafafc` at 11-13 px, `board.css:4,7,198,272,353,365`) and
  `button:disabled { opacity: .55; cursor: wait }`; `<h3>` inside `<button class="task-card">`
  with an `aria-label` that hides the ID/priority/note (`board.js:135-154`); phone bottom nav
  omits Overview and Knowledge (`index.html:588-594`).
- **P3:** five `role="status"` regions re-announce on every flap; `aria-current="false"`
  noise; `<legend>` wrapping a button in focus tiles; graph SVG captures wheel and has no
  keyboard zoom/pan; no skip link; `.filters select { max-width: 120px }` truncates archive
  ids at <= 1100 px; dialog action rows scroll below the fold on short phones.

### 8.5 Docs versus implementation
- Docs describe checkpoint times as a liveness signal with caveats, but the UI gives no
  staleness measure and the field that would allow one (`cycle_started_at`) is not served.
- "Every text field in request forms also has a dictation microphone" (`docs/board.md`):
  `VOICE_FIELDS` covers three fields; Add-knowledge fields have none (`voice.js:5`).
- Docs do not say that all controls are disabled while "Disconnected".
- "Search covers task text, files, evidence, and commits" is implemented as substring over
  `JSON.stringify(task)` (`board.js:58`), so field names and punctuation match too.
- "Knowledge export downloads the nodes and links currently shown" exports the whole
  response including hotspots/refine/dream logs (`graph.js:744`).
- Reports picker: with the default `All runs` selection the first report is the current
  run's or newest archive, not "the selected run's" as `docs/board.md` says.
- Consistent and verified: four columns, worklist, 20-row paging, 50-task bulk select,
  3 s/10 s poll, 50-draft cap, Shift+V, "confirm stop", `?layout=`, 20 category tiles,
  HTTPS-origin rule for `--allow-host`, 512 KiB reports, version-1 control gate.

---

## 9. Remediation plan outline

Ordered by effect on a 48-72 hour unattended run. Each phase is independently shippable.

**Phase 1: the daemon never dies and never halts on repairable state (P0)**
1. Wrap each `Supervisor.run()` iteration in `except Exception`: journal the traceback,
   count a failed cycle, continue; wrap `daemon()` in `except BaseException` that records
   `halted:` before re-raising. Extend `sync_graph`/`learn`/`memory_brief` to `Exception`.
2. Extend `repair_state()` to validate and repair every agent-written field the supervisor
   reads (`outcome` not in the accepted set, `last_report_hour`, `cycle`, timestamps):
   restore from the pre-cycle copy or null the field, journal it, continue.
3. Read ledgers with `errors="replace"` in `evidence_rows()`, or route damaged bytes through
   `repair_ledgers()` before `snapshot()`.
4. On start/restart, check `supervisor.json.child_pid`: if alive and matching the engine,
   wait for it or terminate its process group before launching a cycle.
5. Ship a restart mechanism: `--supervise` loop or launchd/systemd templates that relaunch
   with the saved deadline until it passes; `--status` reports "crashed" when the lock is
   free but `phase` is `working`.
6. Tests: fake CLI modes for each of the above, including a scripted multi-cycle mode.

**Phase 2: park instead of halt; budget the child (P1)**
7. Replace the terminal breaker with a parked state: after `IMPROVE_MAX_RECOVERIES`, sleep
   with long backoff (15-60 min) and retry until the deadline; keep `halted:` for dirty
   trees that cannot be quarantined. Scale `IMPROVE_LIMIT_WAIT_BUDGET` and
   `IMPROVE_MAX_WORKER_HALTS` with duration.
8. Match `LIMIT_PATTERN` regardless of exit code; treat a limit as neither failure nor
   progress; retry finalization after a limit wait.
9. Put the cycle timeout, kill time and grace in the prompt; add a "cycle budget"
   paragraph to `SKILL.md`; back off idle cycles after N consecutive empty scans.
10. Distinguish untracked-only dirt from modified tracked files; let preflight record an
    artifact allowlist; do not block the active task for environmental failures (CLI crash,
    limit, network).
11. `block_active_item()` rewrites the ledger atomically with one row per id and keeps the
    original `note`; supervisor reasons go to `supervisor_note` and are excluded from graph
    blocker ingestion.
12. Rotate `daemon.log` per cycle; cap `journal.md` growth or paginate it on the board.

**Phase 3: knowledge graph policy and robustness (P1/P2)**
13. Exempt `source in ("user", "learning")` and `protected_until_tick` from refine's fade,
    contradiction archival and size caps; require a user action (or `rate wrong` by the
    user) to archive user knowledge. Add tests mirroring the dream protections.
14. Narrow `is_damage()` to true corruption (`DatabaseError` with corruption text,
    `NotADatabase`); roll back and re-raise on `IntegrityError`/`ProgrammingError`.
15. Bounded `writer_queue()` wait (e.g. 30 s) with a retryable exit code; distinguish
    retryable from fatal in the graph CLI; tell the agent a graph error never blocks a task.
16. Stop re-importing lessons every sync (import once per lesson key; respect archival);
    fix gate substring matching (exact gate names in a structured `verification.gates`
    list); keep `co_changed` edges stable across refines; clear stale `-wal`/`-shm` on
    restore; add a schema version check.
17. Make `dream_due`/`refine_due` honour the interval from first ingest.

**Phase 4: board operability (P1/P2)**
18. Cache parsed ledgers by `(inode, mtime, size)`, compute learning/graph only when asked,
    emit a content `ETag` and honour `If-None-Match`, gzip; read snapshots without the
    operator lock; catch `sqlite3.Error`/`Exception` in `/api/board`.
19. Separate "data is stale (N s)" from "controls unavailable"; back off on failure; use
    server `remaining_seconds`.
20. Expose `cycle_started_at`, `child_pid`, timeouts, `quarantines`, `recoveries_total`,
    limit accounting; render heartbeat/cycle age with thresholds and a failure timeline;
    opt-in browser notifications; serve `journal.md` and a bounded `daemon.log` tail; list
    quarantine refs; add `retry`/`unblock` and `withdraw` request types.
21. Stop re-rendering unchanged dialog content; compute `filteredTasks()` once; paginate
    Columns past a few hundred cards; fix dialog focus return and card semantics; dark
    theme and contrast fixes; add Overview/Knowledge to the phone nav.

**Phase 5: test infrastructure (P1/P2)**
22. Add CI (GitHub Actions) with a fast job (unit + daemon tests) and a nightly job (soak,
    scale, performance, browser). Gate soak/perf tests behind an env var or a `-k` marker.
23. Isolate git config (`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`), skip FTS5
    tests when unavailable, use an ephemeral port instead of 8765, raise `invoke()` timeout
    or make it proportional to cycles, move `unittest.main()` to the end of
    `test_runtime.py`, replace machine-speed thresholds with relative or opt-in checks.
24. Upgrade the fake CLI to scripted multi-cycle behaviour and real helper usage; add tests
    for `unlink`/`proposals`/`consolidate`, cooldown/backoff, limit budget, repeated
    quarantine, stale child, SIGTERM, malformed fields, invalid UTF-8.
25. Run browser checks in CI with Playwright's Chromium, per-check reporting and failure
    screenshots; cover voice pause/resume.
