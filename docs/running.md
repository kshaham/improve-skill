# Running and recovering improvement sessions

[← Back to README](../README.md)

Use this guide for background operation, saved state, command options, and recovery.

[Launch](#running-under-the-daemon) · [Assistant settings](#assistant-and-permissions) ·
[Recovery](#resume-replace-or-finalize) · [Commands](#command-reference-and-troubleshooting) ·
[Troubleshooting](#troubleshooting) · [Configuration](#runner-configuration)

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

### Assistant and permissions

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

### Progress, deadlines, and account limits

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

### Resume, replace, or finalize

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

| Option                             | Behavior                                                                                                                                |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `--repo PATH`                      | Required Git repository root, not a subdirectory                                                                                        |
| `--for 90m` / `--duration 90m`     | Required duration for a launch; positive integer with `s`, `m`, `h`, or `d` (bare integers are seconds); saved deadlines win on restart |
| `--engine auto\|codex\|claude`     | Select the assistant for a new run; saved engine wins on recovery                                                                       |
| `--codex-sandbox MODE`             | Carry over the current Codex session's permitted mode; saved on recovery                                                                |
| `--model NAME`                     | Save an explicit model for the run; otherwise use the initial environment or CLI default                                                |
| `--intake FILE`                    | JSON with all ten answers; required before new improvement work                                                                         |
| `--no-board` / `--board-port PORT` | Skip automatic board startup, or select its local port; `0` chooses a free port                                                         |
| `--skill improve-max --args "..."` | Select the max variant and its parameters; repeat these on recovery                                                                     |
| `--new-run`                        | Archive old state; requires a new duration and completed intake                                                                         |
| `--resume`                         | Continue a recovered halt/stop with the original deadline and saved settings                                                            |
| `--finalize`                       | Retry an ended run's pending report, without new improvement work                                                                       |
| `--status [--json]` / `--stop`     | Inspect state or request a bounded stop; no CLI launch                                                                                  |
| `--dry-run` / `--version`          | Preview the command without launching, or print the runner version                                                                      |

Daemon exit codes: `0` for completion, an intentional stop, target reached, or a successful
read-only command; `1` for a recorded halt; `2` for invalid input/state or a missing
prerequisite. Always inspect `outcome` and `summary_pending`; exit `0` alone does not mean
the final narrative report was generated. These differ from the clock's `0`/`10`/`2` codes.

### Troubleshooting

| Symptom                                 | Next step                                                                                                                       |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| Host detection is missing or ambiguous  | Pass `--engine codex` or `--engine claude` for a new run; use `--engine auto` to keep a saved run's provider                    |
| Selected CLI missing                    | Put that CLI on PATH and authenticate it; the daemon never switches providers automatically                                     |
| Permission errors                       | Check the selected CLI's configuration and saved sandbox against the authorized scope; retries never broaden access             |
| Dirty tree or unresolved `active_item`  | Inspect the recorded item and diff, verify/commit/revert only owned work, then `--resume`; do not reset unrelated edits         |
| `phase: account-limit` after a restart  | Check `retry_at`; the daemon deliberately waits for the saved retry time                                                        |
| Halt after failed/uncheckpointed cycles | Read `daemon.log`, `last_error`, and discovery evidence; resolve the blocker before `--resume`                                  |
| `summary_pending: true`                 | Restore the required CLI/environment, then `--finalize`; the original reason and end time remain intact                         |
| Board unavailable                       | Read `.improve/board.log`, check the helper's `--status`, and retry `--start`; use `--port 0` if your explicit port is occupied |
| Board connected but daemon not running  | The viewer outlives work. Inspect daemon `--status`; recover the saved run as appropriate before expecting more work            |
| Skill not appearing or appearing twice  | Check the installed link and skill name; keep one installation per host, reload if needed                                       |

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

## State on disk

| File under `.improve/`                  | Purpose                                                                                                |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `intake.json`                           | All ten user answers                                                                                   |
| `run.json`                              | Focus, authorization, deadline, gates, cycle, next action, active item, outcome                        |
| `backlog.jsonl`                         | Candidates, evidence, acceptance checks, status, commit SHA                                            |
| `bets.jsonl`                            | Improve-max experiments, phases, measurements, and implementation pieces; also shown on the board      |
| `discovery.jsonl`                       | Scoped scans and their results, including empty/failed passes                                          |
| `journal.md`                            | Reports, steering, recovery and account-limit gaps                                                     |
| `final-report.md`                       | Durable final handover; retryable independently of work                                                |
| `supervisor.json`                       | Daemon-owned engine/model/sandbox, timestamps, retry accounting, and finalization status               |
| `daemon.log`                            | Timestamped supervisor and cycle output                                                                |
| `launcher.log`                          | Optional `nohup` output, kept out of the work tree                                                     |
| `daemon.lock`                           | OS-held exclusive lock; file presence alone does not mean running                                      |
| `board.json`, `board.lock`, `board.log` | Local board identity/URL/private stop token, exclusive lock, and startup log; retained across new runs |
| `history/`                              | Explicitly archived previous run state                                                                 |

The primary session owns atomic state updates. See [references/ledger.md](../references/ledger.md).
Review changes against the recorded baseline. Replace the two values below with the
run's `baseline_commit` and branch:

```sh
improve_baseline="BASELINE_COMMIT_SHA"
improve_branch="improve/YYYY-MM-DD"
git log --oneline "$improve_baseline..$improve_branch"
git diff "$improve_baseline..$improve_branch"
```

## Runner configuration

| Environment variable        | Default                  | Purpose                                                                              |
| --------------------------- | ------------------------ | ------------------------------------------------------------------------------------ |
| `IMPROVE_ENGINE`            | `auto`                   | Detect the host for a new run; `--engine codex` / `claude` overrides                 |
| `IMPROVE_CYCLE_TIMEOUT`     | `3600` (`14400` for max) | Maximum seconds per working cycle                                                    |
| `IMPROVE_FINAL_TIMEOUT`     | `300`                    | Maximum seconds for finalization                                                     |
| `IMPROVE_FINISH_GRACE`      | `120`                    | Maximum cleanup seconds after deadline or stop                                       |
| `IMPROVE_CYCLE_GAP`         | `2`                      | Seconds between daemon cycles; capped at deadline                                    |
| `IMPROVE_TOO_FAST`          | `25`                     | Diagnostic threshold; speed alone never fails a checkpointed cycle                   |
| `IMPROVE_MAX_FAILURES`      | `3`                      | Consecutive failed or uncheckpointed cycles before halt                              |
| `IMPROVE_LIMIT_WAIT`        | `900`                    | First account-limit wait                                                             |
| `IMPROVE_LIMIT_WAIT_CAP`    | `3600`                   | Maximum individual account-limit wait                                                |
| `IMPROVE_LIMIT_WAIT_BUDGET` | `14400`                  | Total wall-clock account-limit wait budget, including downtime inside a pending wait |
| `IMPROVE_MODEL`             | unset                    | Initial optional model; `--model` overrides; recovery keeps the saved selection      |
