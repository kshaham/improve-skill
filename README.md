# /improve

An autonomous improvement skill for Codex and Claude Code. It asks ten questions about what matters,
then discovers and implements verified improvements for the requested duration. The focus
can be features, UI, graphics/assets, performance, bugs, security, or supporting code quality.
An empty backlog starts another discovery pass; it does not finish the run.
Every run also gets a local Kanban board showing queued, active, completed, blocked,
proposed, and rejected tasks, with live updates and searchable history.

```text
Codex:       $improve 4h
Claude Code: /improve 90m ~/Code/api
```

## Install and update

For a fresh installation, keep one checkout and link both skills into each assistant:

```sh
improve_checkout="$HOME/Code/improve-skill"
git clone https://github.com/kshaham/improve-skill.git "$improve_checkout"

for improve_skills_dir in "$HOME/.claude/skills" "$HOME/.agents/skills"; do
  mkdir -p "$improve_skills_dir"
  for improve_name in improve improve-max; do
    improve_target="$improve_checkout"
    if [ "$improve_name" = improve-max ]; then improve_target="$improve_checkout/max"; fi
    if [ -e "$improve_skills_dir/$improve_name" ] || [ -L "$improve_skills_dir/$improve_name" ]; then
      printf 'Keeping existing installation: %s\n' "$improve_skills_dir/$improve_name"
    else
      ln -s "$improve_target" "$improve_skills_dir/$improve_name"
    fi
  done
done
```

Codex documents `~/.agents/skills` and supports symlinked skill directories; see
[Build skills](https://learn.chatgpt.com/docs/build-skills). Existing `~/.codex/skills`
installations also work with the tested Codex CLI 0.160.0. Keep an existing installation
in its current root rather than adding a duplicate under another root. The commands above
preserve existing paths, including broken links; review those deliberately before replacing
them. `improve-max` needs the parent `improve` checkout and its shared scripts/references.

For a linked installation, update the checkout with
`git -C "$HOME/Code/improve-skill" pull --ff-only`; both assistants then use the same files.
For copied installations, sync the same revision into each existing skill directory and
preserve executable permissions on `scripts/*.sh`. Reload the assistant if the updated skill
does not appear. Requirements: Git and Python 3.9+; daemon mode additionally needs the selected
assistant's authenticated CLI. The runner supports macOS/Linux.

## Start with the user's priorities

Every new run begins with exactly ten questions, before the timer starts:

1. Top three desired outcomes, in order.
2. Features or journeys and the behavior that should change.
3. Screens, components, interactions, and layouts needing attention.
4. Graphics/assets and the desired style or reference.
5. Performance pain points and the affected workload/device.
6. Known bugs and reproduction examples.
7. Security and privacy concerns.
8. Tests, accessibility, resilience, maintainability, docs, and developer workflow.
9. Exclusions, compatibility, design, tooling, and cost constraints.
10. Observable success criteria and how to check them.

The exact questions and answer format are in [references/intake.md](references/intake.md).
Users can answer all ten in one message, including “none” or “use your judgment.” The agent
waits for answers, saves them, and derives an ordered focus plan. Resumed cycles and context
compaction reuse that plan. A fresh run gets a fresh intake.

An intake explicitly marked `awaiting_answers` cannot launch unattended work. Mark it
`complete` after the user has answered; legacy files without a status still accept ten
nonempty answers. Resupplying the original `--intake` on recovery preserves later saved
authorization and steering notes. New answers require a new run or explicit in-session steering.

Most discovery effort goes to the highest-priority unfinished outcomes. Other lanes support
those goals; easy cleanup cannot crowd out a requested feature or visual improvement.
Explicit exclusions apply throughout. User steering updates the plan without resetting time.

## Local Kanban board

The skill creates a board and shares its local URL when a run starts. Daemon runs start
it automatically with either Codex or Claude; foreground runs launch the same helper.
You can also open a board for any repository with an existing improvement ledger:

```sh
improve_board="$HOME/Code/improve-skill/scripts/improve-board.sh"
improve_repo="$HOME/Code/my-app"
"$improve_board" --repo "$improve_repo" --start
# Prints the actual URL, normally http://127.0.0.1:8765
"$improve_board" --repo "$improve_repo" --status --json
"$improve_board" --repo "$improve_repo" --stop
```

The board includes:

- **Four working columns:** Queued, In progress, Blocked, and Proposed.
- **Recent completions:** five compact rows below the board, with a link to the full history.
- **History view:** completed and rejected tasks in rows with title, outcome, area, recorded
  date, and commit. Twenty rows per page keep long runs manageable; click any row for details.
- **Task details:** evidence, acceptance checks, verification, files, notes, and commits.
- **Search and filters:** find tasks by text, area, or run, including archived history.
- **Live progress:** task counts, deadline, latest checkpoint, and recent investigations.
- **Run health:** engine/model, cycle, checkpoint times, retry schedule, failure count,
  and pending final report. A connected board is separate from a running daemon.
- **Run controls:** pause after a clean checkpoint, resume, or stop a running daemon.
  Pause keeps the original wall-clock deadline; it works with either saved engine.
- **New tasks and guidance:** send desired outcomes, acceptance checks, or updated focus
  directly from the board. Change a current task's priority or approve/decline a proposal
  from its details.
- **Requests:** search current and archived requests, including the skill's responses.
  Filter by run and status, browse twenty rows per page, and export every matching page.
  Live updates preserve your place on older pages.
- **Related work:** open a request's resulting tasks or target bet to see its status,
  evidence, and verification. Links stay within their original run when IDs are reused.
- **Reports:** browse and download saved final reports across runs. The selected run's
  report opens first when available; the picker lets you switch without leaving the board.
- **Export:** download matching tasks as JSON. History includes all matching pages and
  respects the outcome filter; Board exports all matching tasks, including finished work.

It reads `.improve/backlog.jsonl` directly, includes `improve-max` experiments from
`bets.jsonl`, and refreshes every three seconds while visible.
There is no second task database to maintain. The skill records tasks before starting,
updates their status as work progresses, and retains finished/rejected work. Existing
ledgers work without migration; older work that was never recorded cannot be reconstructed.
Opening task details does not approve work. Explicit controls save user intent separately
from worker-owned ledgers, and the skill applies it at a checkpoint. A saved request stays
**Pending** until the skill records a response. **Applied** means its plan or task ledger
was updated; normal task verification determines whether implementation reaches Done.
Completed work stays in the ledger. History searches every recorded task, including rows
outside the current page. New results keep your place while you browse older pages; use
**Latest** to return to the newest results. Page controls stay visible as you scroll.

Run controls require a live daemon started with version 1.11.0 or newer. **Pause requested**
means its current cycle is still settling; **Paused** means no new improvement cycle is
running. The deadline keeps counting down, and expiry still permits the bounded final
report. **Resume** also cancels a pending pause. **Stop run** ends work after cleanup and
leaves the board online; restarting an ended/halted run uses the existing CLI recovery
checks. The board never launches a model or changes its saved engine, model, or sandbox.
Foreground sessions can read the same task/guidance requests through the skill's helper.
Requests apply to the current run; new-run archival preserves them with that run's files.
Archived pending requests stay in history and are never automatically applied to a new run.
Submitting a task or guidance while browsing an archive saves it to the current run and
returns the Requests view there. Search includes receipt text as well as request details.
The worker reads its pending requests and run identity together under the archival lock.
See [request handling and controls](references/board-control.md) for the checkpoint contract.

The current run panel flags an inactive daemon and missing final report, and shows when a
report-only retry is running. It uses the supervisor's confirmed outcome, so a worker's
premature completion cannot make an active supervised run look finished. Checkpoint times
show the latest saved observations, not a guarantee that an agent is still making progress.

Malformed records and non-finite benchmark values such as `NaN` produce warnings while
valid task history remains visible. The viewer never rewrites those source records.

The board stays available after the improvement run ends. Stopping it does not stop the
daemon, and daemon `--stop` leaves the board available. Restarting the computer stops the
service; `--start` brings the saved history back. Daemon `--new-run` archives old tasks
without changing a running board's URL. Multiple repositories receive separate ports;
if 8765 is busy the helper chooses a free port. Use helper `--port 9000` or daemon
`--board-port 9000` for a specific port, or `0` to choose any free port. Repeated starts
reuse the existing board. Daemon `--no-board` skips startup; an unavailable board never
prevents improvement work.

Only Python 3.9+ is required. The server binds to `127.0.0.1` and uses local assets, with
no npm setup or hosted service. Keep `.improve/` untracked. See
[board lifecycle and task schema](references/board.md) for details and remote-host access.

## Lanes and evidence

| Lane | Typical work | Required evidence |
|---|---|---|
| Features (when requested) | Existing journeys and bounded new behavior | User acceptance gap, executed journey, regression/acceptance test |
| UI (when requested) | Hierarchy, spacing, feedback, responsiveness, interaction states | Actual rendering before/after at matching states and viewports |
| Assets (when requested) | Icons, images, illustration, animation, loading and decoding | Asset usage/provenance and inspection in its actual screen |
| Quality | Bugs, error handling, boundaries, coupling | Concrete failure scenario and regression check |
| Coverage | Untested behavior and error paths | Test that detects a meaningful mutation |
| Security | Reachable injection, authorization, sensitive-data handling | Concrete threat path and adversarial check |
| Performance | Launch, scrolling, screen loading, endpoint latency | Journey profile and interleaved before/after measurements |
| Concurrency | Races, lifecycle/cancellation, locks and actors | Detector or reproducible scheduling scenario |
| Resilience | Timeouts, retries, fault handling | Fault-injection check |
| Gate speed | Slow builds/tests, redundant setup | Timings with equivalent verification coverage |
| Docs | Broken commands and inaccurate behavior descriptions | Executed command or checked behavior |
| Accessibility (relevant UI) | Labels, keyboard/focus, contrast, dynamic text | Platform audit and interaction checks |
| Contracts (relevant client/server) | Fields, optionality, enum and format drift | Actual schema/round-trip check |

The user's scope determines which lanes are active. A finding needs evidence, not personal
taste. Feature and visual requests can be improvements against the user's acceptance criteria;
they need not masquerade as bugs. Creation tools must be available and fit the user's costs
and constraints. Unavailable rendering is reported, never treated as a verified visual result.

## How the loop works

After intake, record the start/deadline, discover the repository's gates, establish a green
baseline, and create a unique `improve/YYYY-MM-DD` branch. Setup time counts toward the
requested working duration.

Each cycle reads the clock and durable state, then:

1. Refill below five ready items, with only one refill generation active. Use up to four
   read-only finders within available agent slots, or inspect serially if agents are absent.
2. Rank by user priority, then severity × confidence × impact. An in-scope critical
   vulnerability preempts lower-risk work.
3. Fix one item at a time. Save its starting commit and owned paths before editing.
4. Run the relevant gates and acceptance checks. Commit a verified result or roll back only
   that item's edits. Save rejected findings with their evidence.
5. Report completed wall-clock hours and checkpoint the next concrete action.
6. Continue immediately in the foreground, verify an actual scheduler's re-entry, or return
   to the external daemon for the next cycle.

No scheduling tool is assumed to exist. Foreground mode does not send its final response
just because a batch or report finished. The execution contract is in
[references/continuation.md](references/continuation.md). The
[work-selection guide](references/work-selection.md) turns priorities into acceptance checks,
confirms findings before fixes, and budgets editing plus verification rather than coding alone.

## When the queue runs dry

Discovery progresses through defects, coverage, structure, then bounded refactors. A tier
is dry only after eligible user priorities have been inspected; a failed or cancelled scan
is not an empty result. At the deepest tier, inspect the run's changed code, unscanned
subsystems, applicable previously-disabled lanes, and deferred work that has become feasible.

Each pass records its paths, hypothesis, revision, accepted/rejected results, and next scope.
Use a different hypothesis or source of evidence on later passes. Do not repeatedly scan
identical code with an identical prompt. Do not manufacture cosmetic changes or a commit quota.

There is **no 20-minute dry-sweep sleep**. Keep investigating useful evidence until the
clock expires. Wait only on real tools, dependencies, or account limits. Rejections trigger
diagnosis and rotation; three weak ideas do not automatically terminate a whole run.

## Verification and boundaries

Behavior changes require the repository gate, a meaningful mutation/regression check, and
an executed counter-scenario from an independent reviewer when one is available. When no
subagent is available, do a separate adversarial pass and disclose that limitation. Test
counts are compared for matching commands/filters; builds and linters use appropriate signals.
A documented silent-success command is not a failure just because it prints nothing.

Performance starts at user journeys and profiles, not guessed micro-optimizations. Build a
missing native harness first. Alternate before/after runs for at least five pairs; require
nonoverlapping timings and at least a 5% median gain before landing a performance change.
UI and assets also require inspection of the rendered result. Pure docs/assets do not need
an artificial code mutation. Tests and fixes land together if the test needs the fix to pass.

By default, autonomous `/improve` work does not:

- Edit `main`, push, force-push, or open a PR.
- Delete/weaken tests or add skips to get green.
- Bump dependencies/toolchains, migrate schemas, or rewrite money/once-only invariants.
- Read/write secret or credential files or edit outside the target repo.
- Install new tooling, start from an unexplained dirty tree, or silently weaken a gate.

A finding needing broader authority becomes a proposal while other eligible work continues.
Explicit user authorization takes precedence within its stated scope. For example, a request
to push and merge is carried into `run.json.authorization` and honored after the checks;
the skill does not ask for that permission again. A general improvement request alone does
not grant publishing, spending, or rewrite permission. Existing exclusions still apply.
Unavailable verification gets bounded recovery, then affected work is benched; halt when
nothing eligible can be verified. Interrupted dirty edits require manual recovery. Never
reset/clean the whole tree or drop a stash to recover an item.

## Clock and reports

```sh
"$HOME/Code/improve-skill/scripts/improve-clock.sh" /path/to/repo
"$HOME/Code/improve-skill/scripts/improve-clock.sh" --json /path/to/repo
"$HOME/Code/improve-skill/scripts/improve-clock.sh" --remaining /path/to/repo
```

Exit `0` means time remains, `10` means the deadline passed, and `2` means invalid state.
Handle these explicitly when using `set -e` or command chaining. `report_hour` is the completed
hour to report; `hour` is the current interval. At 1h05m elapsed, report hour 1.
For supervised runs, the clock takes its start/deadline from `supervisor.json`, even before
the agent creates `run.json`. Report progress still comes from `run.json.last_report_hour`.
An agent resetting its own timestamps cannot extend the clock.

Reports go to the session and `.improve/journal.md`; notifications are optional when available
and authorized. Report progress against user priorities, inspected scopes, completed discovery,
commits, rejected/proposed items, measurements, and the next action. “No changes this hour”
is valid. An hourly report is a progress update, followed by more work.

Only expiry permits `completed`. User stop and environmental halt are labeled separately.
Deadline mid-item means settle that item safely, then stop; no new fixes start after expiry.
A finalization failure records `summary_pending: true`, never invented verification.

## Running under the daemon

For work that should survive terminal closure, complete the interactive intake first.
Use the runner from the skill checkout, and set the target separately so commands work
from any working directory:

```sh
improve_daemon="$HOME/Code/improve-skill/scripts/improve-daemon.sh"
improve_repo="$HOME/Code/api"
"$improve_daemon" --engine codex --repo "$improve_repo" --for 6h --intake /path/to/intake.json
"$improve_daemon" --engine claude --repo "$improve_repo" --for 90m --dry-run
"$improve_daemon" --status --repo "$improve_repo"
"$improve_daemon" --stop --repo "$improve_repo"
```

To detach a new launch after intake:

```sh
mkdir -p "$improve_repo/.improve"
nohup "$improve_daemon" --engine codex --repo "$improve_repo" --for 6h \
  --intake /path/to/intake.json >"$improve_repo/.improve/launcher.log" 2>&1 </dev/null &
```

Keep the launcher log under `.improve/`; putting it in the target's tracked/untracked work
area can make the tree dirty before the daemon starts. Check `--status --json` after launch
to confirm the lock is held and inspect the phase. A shell job ID alone is not proof that
preflight succeeded. The daemon's detailed log is `.improve/daemon.log`.

Requires **Python 3.9+, Git, and the selected assistant's authenticated CLI** on macOS/Linux;
no jq or GNU timeout is needed. The skill passes `--engine codex` when running in Codex,
or `--engine claude` when running in Claude Code. Both `improve` and `improve-max` use the
same routing. The location of the installed script does not determine the engine.

With `--engine auto` (the default), a new run detects Codex from `CODEX_THREAD_ID` or
`CODEX_CI`, and Claude Code from `CLAUDECODE` or `CLAUDE_CODE_ENTRYPOINT`. A plain shell
or conflicting markers requires an explicit engine; it never guesses from PATH order.
`IMPROVE_ENGINE` sets the default, and `--engine` overrides it. A saved run always keeps
its engine across restarts, `--resume`, and `--finalize`; a conflicting explicit choice
is rejected. Pre-1.6 supervisor files keep Claude, their original engine. Change providers
with a new intake and `--new-run`. A missing selected CLI never falls back to another provider.

Codex cycles use `codex --no-daemon --ask-for-approval never exec --ephemeral --color never`.
When launching from Codex, the skill carries over its current session's sandbox with
`--codex-sandbox read-only`, `workspace-write`, or `danger-full-access`, within the existing
authorization. The choice is saved for restarts. Without this option, Codex uses its CLI
configuration, which may differ from the interactive session and default to read-only.
Repository edits, tests, and commits need corresponding permissions; the runner never
retries with broader access. User rules remain in force. `--no-daemon` keeps cycle processes
outside Codex's shared server so stop/timeouts can
terminate their process group. These flags were checked with Codex CLI 0.160.0.
See the official [noninteractive execution guide](https://learn.chatgpt.com/docs/non-interactive-mode).
Claude cycles retain `claude -p --permission-mode bypassPermissions --output-format text`;
the inherited interactive-session marker is cleared for each independent cycle. Both use
existing CLI authentication/configuration and accept `--model` for that engine. The initial
explicit model, including one from `IMPROVE_MODEL`, is saved across restarts and report retries.
Later environment defaults do not change it. Omit `--model` on recovery; an explicit different
model requires `--new-run`. With no selection, the CLI chooses its default; that default and
model aliases are not pinned to a specific model revision. Launch only within the
user-authorized unattended scope.

If `IMPROVE_ENGINE` names a different provider when recovering an existing run, use
`--engine auto` to keep the saved provider. Omit `--codex-sandbox` on recovery to reuse its
saved choice. Changing either explicit setting requires a new run.

Each cycle reads the skill beside the runner, so development checkouts and installations in
other locations work. Child stdin is closed, keeping terminal input out of agent prompts.

The daemon preserves one deadline in `supervisor.json` across restarts and preflight failures.
It restores an agent-reset deadline and rejects premature completion. A fast empty scan is
valid if it checkpoints new evidence. Incrementing a counter or rewriting an identical
scan’s timestamps does not count. New commits, findings, scans, and meaningful preflight
results do count. Improve-max measurements and implementation-piece evidence in `bets.jsonl`
also count. Changing task IDs, titles, status labels, or lifecycle timestamps alone does not;
record the actual findings, verification, measurements, or commits behind a status transition.
The next prompt redirects a stalled cycle toward a fresh scope; repeated
failed/uncheckpointed cycles trip a bounded breaker. Failure and wait totals survive
ordinary process restarts.
Completed finders are persisted before a cycle exits; live subagents cannot cross processes.

Account limits get bounded exponential backoff, with waits capped at the actual remaining
time and journaled. A restart honors the saved retry time before launching another agent.
Elapsed wall time within the pending wait, including downtime, counts once toward the wait
budget. Stopping checkpoints a partial wait for recovery. Pre-1.7 state has no wait-start
checkpoint, so it preserves the retry time but can only newly account the remaining wait.
Waits respond promptly to `--stop`; a running cycle settles first. A
portable process-group timeout bounds wedged work. Dirty trees and recorded halts stop
relaunches. At expiry one bounded finalization cycle runs even if the last wait crossed the
deadline. Work in progress may overrun only within `IMPROVE_FINISH_GRACE` (120 seconds by default);
the earlier cycle timeout still applies. The original deadline is never extended. A stop
request applies the same cleanup bound, preventing a wedged one-hour cycle from delaying it. The daemon cannot execute while the machine is asleep or powered off.

To resume an unfinished clean run, repeat its command; the recorded deadline wins over
`--for`. To start again after reviewing a finished run, collect a new intake and use:

```sh
"$improve_daemon" --engine codex --repo "$improve_repo" --for 4h --new-run --intake /path/to/new-intake.json
```

After resolving a halt, resume the same run without extending its deadline:

```sh
"$improve_daemon" --repo "$improve_repo" --resume
```

Recovery requires a clean tree and resolution of any recorded `active_item`. Repeat the
original `--skill` and `--args` for an improve-max run. A resume after the original deadline
performs finalization only; it does not grant extra work time.

To regenerate a pending final report without opening another improvement cycle:

```sh
"$improve_daemon" --repo "$improve_repo" --finalize
"$improve_daemon" --repo "$improve_repo" --status --json
```

A report retry preserves the original stop reason and end time, writes `final-report.md`,
and checks that no work-tree changes or new commits occurred. Status JSON includes the
saved engine/model, phase, current child process, retry time, clock, and next action.
Even if a successful worker writes `completed` exactly at expiry, the daemon still requests
a final report. A nonzero worker exit cannot claim completion or target success. An old
report cannot clear `summary_pending` for a later halt or stop.

`--new-run` archives the previous state under `.improve/history/`. It does not discard code or
reset branches. Status and dry-run do not create state or launch a paid agent.

## Command reference and troubleshooting

| Option | Behavior |
|---|---|
| `--repo PATH` | Required Git repository root, not a subdirectory |
| `--for 90m` / `--duration 90m` | Required duration for a launch; positive integer with `s`, `m`, `h`, or `d` (bare integers are seconds); saved deadlines win on restart |
| `--engine auto\|codex\|claude` | Select the assistant for a new run; saved engine wins on recovery |
| `--codex-sandbox MODE` | Carry over the current Codex session's permitted mode; saved on recovery |
| `--model NAME` | Save an explicit model for the run; otherwise use the initial environment or CLI default |
| `--intake FILE` | JSON with all ten answers; required before new improvement work |
| `--no-board` / `--board-port PORT` | Skip automatic board startup, or select its local port; `0` chooses a free port |
| `--skill improve-max --args "..."` | Select the max variant and its parameters; repeat these on recovery |
| `--new-run` | Archive old state; requires a new duration and completed intake |
| `--resume` | Continue a recovered halt/stop with the original deadline and saved settings |
| `--finalize` | Retry an ended run's pending report, without new improvement work |
| `--status [--json]` / `--stop` | Inspect state or request a bounded stop; no CLI launch |
| `--dry-run` / `--version` | Preview the command without launching, or print the runner version |

Daemon exit codes: `0` for completion, an intentional stop, target reached, or a successful
read-only command; `1` for a recorded halt; `2` for invalid input/state or a missing
prerequisite. Always inspect `outcome` and `summary_pending`; exit `0` alone does not mean
the final narrative report was generated. These differ from the clock's `0`/`10`/`2` codes.

| Symptom | Next step |
|---|---|
| Host detection is missing or ambiguous | Pass `--engine codex` or `--engine claude` for a new run; use `--engine auto` to keep a saved run's provider |
| Selected CLI missing | Put that CLI on PATH and authenticate it; the daemon never switches providers automatically |
| Permission errors | Check the selected CLI's configuration and saved sandbox against the authorized scope; retries never broaden access |
| Dirty tree or unresolved `active_item` | Inspect the recorded item and diff, verify/commit/revert only owned work, then `--resume`; do not reset unrelated edits |
| `phase: account-limit` after a restart | Check `retry_at`; the daemon deliberately waits for the saved retry time |
| Halt after failed/uncheckpointed cycles | Read `daemon.log`, `last_error`, and discovery evidence; resolve the blocker before `--resume` |
| `summary_pending: true` | Restore the required CLI/environment, then `--finalize`; the original reason and end time remain intact |
| Board unavailable | Read `.improve/board.log`, check the helper's `--status`, and retry `--start`; use `--port 0` if your explicit port is occupied |
| Board connected but daemon not running | The viewer outlives work. Inspect daemon `--status`; recover the saved run as appropriate before expecting more work |
| Skill not appearing or appearing twice | Check the installed link and skill name; keep one installation per host, reload if needed |

## State on disk

| File under `.improve/` | Purpose |
|---|---|
| `intake.json` | All ten user answers |
| `run.json` | Focus, authorization, deadline, gates, cycle, next action, active item, outcome |
| `backlog.jsonl` | Candidates, evidence, acceptance checks, status, commit SHA |
| `bets.jsonl` | Improve-max experiments, phases, measurements, and implementation pieces; also shown on the board |
| `discovery.jsonl` | Scoped scans and their results, including empty/failed passes |
| `journal.md` | Reports, steering, recovery and account-limit gaps |
| `final-report.md` | Durable final handover; retryable independently of work |
| `supervisor.json` | Daemon-owned engine/model/sandbox, timestamps, retry accounting, and finalization status |
| `daemon.log` | Timestamped supervisor and cycle output |
| `launcher.log` | Optional `nohup` output, kept out of the work tree |
| `daemon.lock` | OS-held exclusive lock; file presence alone does not mean running |
| `board.json`, `board.lock`, `board.log` | Local board identity/URL/private stop token, exclusive lock, and startup log; retained across new runs |
| `history/` | Explicitly archived previous run state |

The primary session owns atomic state updates. See [references/ledger.md](references/ledger.md).
Review changes against `baseline_commit`, not an assumed `main` branch:

```sh
git log --oneline <baseline_commit>..<run_branch>
git diff <baseline_commit>..<run_branch>
```

## Runner configuration

| Environment variable | Default | Purpose |
|---|---|---|
| `IMPROVE_ENGINE` | `auto` | Detect the host for a new run; `--engine codex` / `claude` overrides |
| `IMPROVE_CYCLE_TIMEOUT` | `3600` (`14400` for max) | Maximum seconds per working cycle |
| `IMPROVE_FINAL_TIMEOUT` | `300` | Maximum seconds for finalization |
| `IMPROVE_FINISH_GRACE` | `120` | Maximum cleanup seconds after deadline or stop |
| `IMPROVE_CYCLE_GAP` | `2` | Seconds between daemon cycles; capped at deadline |
| `IMPROVE_TOO_FAST` | `25` | Diagnostic threshold; speed alone never fails a checkpointed cycle |
| `IMPROVE_MAX_FAILURES` | `3` | Consecutive failed or uncheckpointed cycles before halt |
| `IMPROVE_LIMIT_WAIT` | `900` | First account-limit wait |
| `IMPROVE_LIMIT_WAIT_CAP` | `3600` | Maximum individual account-limit wait |
| `IMPROVE_LIMIT_WAIT_BUDGET` | `14400` | Total wall-clock account-limit wait budget, including downtime inside a pending wait |
| `IMPROVE_MODEL` | unset | Initial optional model; `--model` overrides; recovery keeps the saved selection |

## /improve-max

[The max variant](max/SKILL.md) pursues measured, larger transformations over days. It inherits
the intake, focus, continuation, and verification rules, then adds characterization corpora,
throwaway spikes, incremental replacement behind interfaces, and measured acceptance gates.
Only its explicit rules permit dependency, schema, wire, framework, or stack changes.

```text
/improve-max 3d ~/Code/api --target 5x --kinds design,data
```

```sh
scripts/improve-daemon.sh --engine codex --skill improve-max --repo ~/Code/api --for 3d \
  --args "--target 5x --kinds design,data" --intake /path/to/intake.json
```

`--target` defaults to `3x`; `--spike` defaults to `max(1.5x, target/3)`; `--kinds` limits the
allowed bet types. Meeting targets returns to ordinary improvements until expiry, unless
`--stop-at-target` explicitly permits `outcome: "target reached"`. Targets are measured goals,
not promises. See [bets](max/references/bets.md) and
[characterization](max/references/characterization.md) for the larger-change protocol.

## Development checks

For structural code navigation, optionally build a local CodeGraph index with
`codegraph init -i` from this checkout. Generated `.codegraph/` data stays untracked.
When the MCP server starts from a parent directory, pass this checkout's absolute path
as `projectPath` to its tools. CodeGraph is a development aid; skill runs do not require it.

From the skill checkout:

```sh
python3 -B -m unittest discover -s tests -v
bash -n scripts/improve-clock.sh scripts/improve-daemon.sh scripts/improve-board.sh
```

The regression suite uses temporary git repositories and fake Codex and Claude CLIs. It tests intake,
deadlines, early completion, discovery continuity, restart, rate limits, lock ownership,
timeouts, dirty state, recovery, repeated-scan detection, stop/deadline cleanup, report-only
retries, engine/model persistence, authoritative clocks, rate-limit restart accounting,
failed-success claims, stale reports, missing CLI failures, board startup/stop/history,
malformed ledgers, HTTP boundaries, live task data, experiment evidence, bookkeeping-only
churn, intake metadata preservation, authoritative board outcomes, and finalization without
launching a real model or editing a real app.
It does not establish that every model will find useful changes for a multi-hour run.

Optional browser checks use Playwright with an installed Chrome executable (the macOS
default is detected), or a Playwright-managed Chromium installation:

```sh
uv run --with playwright python tests/browser_board.py --chrome /path/to/chrome
```

These exercise filtering, task details, automatic refresh, export, escaped task text,
connection recovery, run-health/report states, and mobile/tablet overflow. A fixture with
over 2,000 finished tasks checks bounded row rendering, pagination, search across pages,
keyboard navigation, date fallbacks, and position preservation during live updates.
Control checks cover pause/resume/stop with fake Codex and Claude workers, unchanged
deadlines, stale tabs, concurrent submissions, same-origin capabilities, request
acknowledgments, lost-response retries, preserved drafts, and saved-report downloads.
Archive checks cover request search/export, links with reused IDs, task/bet status,
damaged archives, safe report selection, late report responses, and request reads during
run archival. Browser checks also verify archived views on mobile and stable pagination.
Screenshots are written to a temporary directory
unless `--artifacts PATH` is supplied. Playwright is only a development dependency.

Licensed under [MIT](LICENSE).
