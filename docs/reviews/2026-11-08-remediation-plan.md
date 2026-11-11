# Remediation plan: long-run autonomy, knowledge graph, board, tests

Source: [2026-11-08-architecture-review.md](2026-11-08-architecture-review.md). Every item
below names the defect, the change, the files, and the test that proves it. Phases are
ordered by effect on a 48-72 hour unattended run; each is independently shippable.

Design rules that apply throughout:

- **The supervisor never dies and never halts on state it can repair.** A halt is reserved
  for a dirty tree that cannot be quarantined. Everything else is journaled, counted, and
  worked around.
- **Agent-written state is untrusted input.** Every field the supervisor reads from
  `run.json` or a ledger is validated and repaired, never assumed.
- **Environmental failure is not task failure.** A CLI crash, a rate limit, or a build
  artifact must not block the task the agent happened to be working on.
- **User knowledge is steering.** Nothing automatic archives or removes it.
- **Behavioural changes ship with a regression test that fails on the old code.**

## Phase 1: the daemon survives (P0)

| # | Change | Files | Test |
|---|---|---|---|
| 1.1 | Wrap each supervisor loop iteration in `except Exception`: journal the traceback, record `phase: crashed-cycle`, bump `crashes`, count a failed cycle, cool down, continue. After `IMPROVE_MAX_CRASHES` (5) consecutive crashes, halt with the traceback. `daemon()` catches `BaseException`, records `phase: crashed`, `last_error`, leaves `outcome` null so a plain relaunch resumes, re-raises. `sync_graph`, `learn`, `memory_brief` catch `Exception`. | `improve_runtime.py` | fake CLI writes a ledger that makes `snapshot()` raise `TypeError`; daemon continues; journal has the traceback |
| 1.2 | `repair_run()` validates and repairs every agent-written field the supervisor reads: `outcome` (unknown string -> null, journaled; `Halted:`/`halted ` -> `halted:`), `last_report_hour` (coerce integral float/digit string, else 0), `cycle` (coerce, else restore), `started_at`/`deadline` (reconcile already restores), `active_item` shape. Used by `repair_state()` and `reconcile()`. | `improve_resilience.py`, `improve_runtime.py` | fake CLI writes `outcome: "done"`, `last_report_hour: 3.0`, `cycle: "7"`; daemon continues; `clock` CLI exits 0 |
| 1.3 | Ledgers decode with `errors="replace"`; `evidence_rows()` tolerates and counts bad lines instead of raising; `repair_ledgers()` reports undecodable bytes. | `improve_runtime.py`, `improve_resilience.py` | ledger with `\xff` bytes; daemon continues |
| 1.4 | Stale child on start: if `supervisor.json.child_pid` is alive and its command is the saved engine, terminate its process group (TERM, grace, KILL) and journal it before any cycle; a dirty tree left by that child is quarantined, not refused. | `improve_runtime.py` | start a sleeping fake child, write its pid, restart daemon; child is dead, journal says so |
| 1.5 | `--supervise`: a watchdog loop that relaunches the daemon until the saved deadline, a terminal outcome, or `stop.request`; strips `--new-run` after the first launch; exponential relaunch backoff. `--status` reports `crashed: true` when the lock is free and the saved phase is not `ended`. Docs: launchd/systemd examples. | `improve_runtime.py`, `docs/running.md` | fake CLI kills its parent daemon (SIGKILL) once; watchdog relaunches; run completes |
| 1.6 | Fake CLI gains `FAKE_SCRIPT` (JSON list of per-cycle modes) and modes: `limit0`, `bad_outcome`, `bad_report_hour`, `bad_cycle`, `bad_utf8`, `untracked`, `crash_parent`, `exit2`, `limit_final`, `big_output`, `uses_helpers`. | `tests/test_runtime.py` | the tests above |

## Phase 2: park, do not halt; budget the child (P1)

| # | Change | Files | Test |
|---|---|---|---|
| 2.1 | Parking: when recoveries, worker halts, or the limit budget are exhausted and `IMPROVE_PARK=1` (default), the daemon enters `phase: parked`, waits `IMPROVE_PARK_WAIT` (900 s, doubling to `IMPROVE_PARK_WAIT_CAP` 3600 s, bounded by the deadline), resets the exhausted counter, journals, and continues. `IMPROVE_PARK=0` restores halting. | `improve_runtime.py` | failures forever with a short deadline: run ends `completed` after parking, never `halted` |
| 2.2 | `LIMIT_PATTERN` is matched regardless of exit code. A limited cycle that did not advance is neither failure nor progress and starts the limit wait. Finalization retries after a limit wait, up to three attempts. Default `IMPROVE_LIMIT_WAIT_BUDGET` is the larger of 4 h and a third of the run. | `improve_runtime.py` | `limit0` mode: `phase` becomes `account-limit`, no task blocked, no failure counted |
| 2.3 | Prompt carries the cycle timeout, kill time, grace, and a cycle-budget rule; `SKILL.md` gets a "Cycle budget" section; idle backoff: after `IMPROVE_IDLE_AFTER` (6) consecutive cycles with no new backlog rows or commits, the inter-cycle gap doubles up to `IMPROVE_IDLE_GAP_CAP` (900 s). | `improve_runtime.py`, `SKILL.md` | prompt contains the timeout; gap grows on empty cycles and resets on a commit |
| 2.4 | Dirt classification: untracked-only dirt is quarantined and cleaned but does not block the active task and does not count as a failure; the correction tells the agent to ignore the artifacts. `recover()` blocks the active task only when the failure streak was `no_evidence`; CLI crashes and limits never block a task. | `improve_runtime.py`, `improve_resilience.py` | `untracked` every cycle: no blocked task, no halt; `exit2` x3: no blocked task |
| 2.5 | `block_active_item()` rewrites the ledger atomically with one row per id, keeps `note`, writes `blocked_reason`, `blocked_by`, `blocked_at`, `supervisor_note`. Graph ingestion ignores supervisor-sourced blockers. Board shows `blocked_reason`. | `improve_resilience.py`, `improve_graph.py`, `improve_board.py` | one row per id after a block; original note intact; no `blocker:` node for a supervisor reason |
| 2.6 | `daemon.log` rotates at `IMPROVE_LOG_MAX_BYTES` (50 MB), keeping three generations. | `improve_runtime.py` | log over the limit rotates at cycle start |
| 2.7 | Tolerate small negative elapsed (clock skew) in `timing()`; `snapshot()` git failures count as a failed cycle, not a halt (covered by 1.1). | `improve_runtime.py` | `started_at` 30 s in the future still yields a running clock |

## Phase 3: knowledge graph policy and robustness (P1/P2)

| # | Change | Files | Test |
|---|---|---|---|
| 3.1 | `refine()` never fades, contradiction-archives, or caps insights whose `source` is `user` or `learning`, or whose `protected_until_tick` is in the future; agent `rate wrong` on user knowledge counts but does not archive. | `improve_graph.py` | the two confirmed scenarios from the review |
| 3.2 | `is_damage()` recognises only corruption (base `DatabaseError`, `InternalError`, corruption words); `IntegrityError`/`ProgrammingError`/`DataError` roll back and re-raise without restoring a checkpoint. | `improve_graph_store.py` | an injected `IntegrityError` leaves `graph.db` in place |
| 3.3 | Bounded writer lock: `IMPROVE_GRAPH_LOCK_WAIT` (30 s) then `GraphBusy`; CLI exits 3 for retryable errors; the cycle prompt says a graph error never blocks a task. | `improve_graph.py`, `improve_runtime.py` | a held lock makes `note` exit 3 within the bound |
| 3.4 | Lessons import once per key (meta `imported_lessons`), re-import only when `reinforced` grows; gate attribution uses whole-token matching and a structured `verification.gates` list when present; `co_changed` edges update in place; `restore_backup()` clears stale `-wal`/`-shm`; a graph written by a newer `version` is refused for writing. | `improve_graph.py`, `improve_graph_store.py` | capped lesson does not return next sync; gate `test` does not match `latest`; `created_at` stable across refines |
| 3.5 | `refine_due`/`dream_due` measure from first ingest (`refine.since`) instead of returning due immediately. | `improve_graph.py`, `improve_dream.py` | fresh graph with interval 1800 is not due |
| 3.6 | `print_context()` reports seeds found by directory/sibling even without stats; `history` CLI gets `--offset`. | `improve_graph.py` | CLI tests |

## Phase 4: board operability (P1/P2)

| # | Change | Files | Test |
|---|---|---|---|
| 4.1 | `/api/board` caches parsed ledgers by `(inode, mtime_ns, size)`, computes `learning`/`graph` only with `?include=`, returns a content `ETag` and honours `If-None-Match` (304), gzips when accepted, reads the snapshot without the operator lock, catches `sqlite3.Error` and any `Exception` as JSON. | `improve_board.py` | unchanged ledger -> 304; locked graph -> 200 with warning |
| 4.2 | Frontend: "data is N s old" is separate from "controls unavailable" (controls stay enabled while the last snapshot is under 30 s old); exponential backoff on failures; server `remaining_seconds` drives the countdown; `refresh()` after an action waits for the in-flight poll. | `assets/board/board.js`, `controls.js` | browser check |
| 4.3 | Expose `cycle_started_at`, `child_pid`, `cycle_timeout_seconds`, `finish_grace_seconds`, `quarantines`, `recoveries_total`, `crashes`, `limit_waited_seconds`, `next_limit_wait`, `park_until`, `ended_at`, `cycle_history`; render heartbeat and cycle age with amber/red thresholds, a cycle timeline, and opt-in browser notifications for halt/stop/completion/deadline/limit/failure streak/new blocked task/request response. | `improve_board.py`, `board.js`, `index.html`, `board.css` | unit test of exposed fields; browser check |
| 4.4 | Serve `journal.md` (`/api/journal`), a bounded `daemon.log` tail (`/api/log?lines=`), and quarantine refs (`/api/quarantine`); link them from run health. New request types `retry` (re-queue a blocked task) and `withdraw` (cancel a pending request) with their checkpoint contract. | `improve_board.py`, `improve_control.py`, `references/board-control.md`, frontend | endpoint tests; request validation tests |
| 4.5 | Stop re-rendering unchanged dialog content; compute `filteredTasks()` once per render; paginate Columns past 200 cards; dialog focus return; card semantics; dark theme; contrast; phone nav gains Overview and Knowledge. | frontend | browser checks |

## Phase 5: test infrastructure (P1/P2)

| # | Change | Files |
|---|---|---|
| 5.1 | GitHub Actions: `fast` job (unit + daemon, Python 3.9 and 3.13) on push/PR; `nightly` job with `IMPROVE_TEST_SLOW=1` (soak, scale, performance) and Playwright browser checks. | `.github/workflows/tests.yml` |
| 5.2 | Soak/scale/performance classes skip unless `IMPROVE_TEST_SLOW=1`; machine-speed thresholds relax by `IMPROVE_TEST_SPEED_FACTOR`. | `tests/test_graph_*.py` |
| 5.3 | Isolate git (`GIT_CONFIG_GLOBAL`, `GIT_CONFIG_NOSYSTEM`, identity env) in every fixture; skip FTS5 tests when unavailable; ephemeral port instead of 8765; `invoke()` timeout proportional to cycles; `unittest.main()` moved to the end. | `tests/*.py` |
| 5.4 | New tests for every Phase 1-3 item plus `unlink`/`proposals`/`consolidate` CLI, cooldown/backoff, limit budget, repeated quarantine, stale child, SIGTERM, malformed fields, invalid UTF-8, `block_active_item` fallback, `repair_ledgers` on all ledgers. | `tests/test_runtime.py`, `tests/test_graph*.py`, `tests/test_learning.py` |
| 5.5 | Browser checks: per-check pass/fail report and failure screenshots; voice pause/resume. | `tests/browser_board.py` |

## Documentation
- `docs/running.md`: parking, watchdog, crash recovery, new env vars, log rotation.
- `references/knowledge-graph.md`: protected knowledge, retryable exit code 3.
- `references/ledger.md`: `blocked_reason`/`supervisor_note`, canonical rows after a block.
- `SKILL.md`: cycle budget, graph errors never block, version 1.15.0 changelog.

## Implementation status (2026-11-08)

| Phase | Status | Verified by |
|---|---|---|
| 1 Daemon survives | Done: per-iteration crash containment and parking, `repair_run()` for agent-written fields, tolerant ledger decoding, stale-child termination, `--supervise` watchdog, `--status` `crashed`, `BaseException` crash record | `tests/test_runtime.py` `LongRunResilienceTests` (malformed fields, UTF-8, contained errors, crash + relaunch, stale child, SIGTERM, crashed status) |
| 2 Park, budget | Done: parking replaces every terminal breaker (`IMPROVE_PARK`), limits matched on any exit code, finalization retries after a limit, budget scales with duration, cycle budget in the prompt, idle backoff, artifact classification (`ARTIFACT_PATTERNS`, `run.json.artifact_paths`), failure-kind aware recovery, canonical ledger rows on block, log rotation, clock-skew tolerance, disk-space guard | `LongRunResilienceTests` (limit0, artifacts, exit2, parking, finalization retry, prompt budget, idle backoff, log rotation, helpers, false claims, disk space), `RecoveryHelperTests` |
| 3 Graph policy | Done: protected user/learning/restored knowledge, corruption-only damage, fair bounded lock with exit 3, lessons import once, whole-token gate matching, supervisor blocks not ingested, stable co-change links, dreams wait for their interval, context names unmeasured neighbors, `history --offset` | `tests/test_graph_policy.py`, existing graph suites (144 tests) and slow suites (12 tests) |
| 4 Board | Done: ledger cache, `include=` gating, ETag/304, gzip, lock-free snapshot, JSON errors with codes, `/api/journal`, `/api/log`, `/api/quarantine`, supervisor fields and ages, health endpoint fields, stale-data model with backoff, server-driven countdown, run-health panel with thresholds and cycle timeline, opt-in notifications and event log, retry/withdraw buttons, blocked-reason banner, dialog signature checks, single `filteredTasks()`, column paging, focus return, card semantics, dark theme and contrast, phone nav | `tests/test_board.py` (+18), `tests/browser_board.py` |
| 5 Tests | Done: CI workflow, `IMPROVE_TEST_SLOW` gate, `IMPROVE_TEST_SPEED_FACTOR`, git isolation, FTS5 skip, scripted fake CLI, `unittest.main()` at the end, `tests.test_control` importable alone | `.github/workflows/tests.yml`, `tests/support.py` |

Final fast suite: 353 tests, 12 skipped (slow), all passing. Slow suites (`IMPROVE_TEST_SLOW=1`): 12 tests passing. Browser check `tests/browser_board.py`: passing. Nothing is committed; the change set is on the working tree of branch `hfi/.../elm`.

Deferred (not in this change set): per-check reporting and failure screenshots in the browser scripts (5.5); voice pause/resume browser coverage; a passphrase for `--allow-host` tunnel mode; CSV export; a cross-repository board index.
