#!/usr/bin/env bash
#
# Keeps an /improve run going after the session that started it has gone.
#
# ## What this exists to fix
#
# /improve paces itself with ScheduleWakeup, and a wakeup lives and dies with the
# session that armed it. Close the terminal, let the laptop sleep, lose the
# process to a crash or an OOM, and the run stops - silently, mid-item, with a
# ledger that still says "running". Nothing notices and nothing resumes. On a
# six-hour run that is most of the run.
#
# Print mode (`claude -p`) does not hold a wakeup either, so this does not
# supervise a self-pacing session. It IS the pacing: one `claude -p` per cycle,
# each starting cold and picking the run up from `.improve/`. That is exactly the
# shape the skill was built for - "Read run.json and backlog.jsonl at the start of
# every cycle. Never carry loop state only in your context" - so a cycle that
# starts with no memory of the last one is the normal case rather than recovery.
#
# The daemon therefore owns: when a cycle starts, when to stop, and what happens
# when a cycle dies. The skill owns everything inside a cycle.
#
# ## Read this before running it
#
# This starts an agent that edits and COMMITS code with no human at the keyboard,
# repeatedly, for hours. It needs --permission-mode bypassPermissions to do that,
# which turns off every confirmation prompt in the session. The safety does not
# come from the prompts; it comes from the skill's own hard rules (branch only,
# never main, never push, never a migration, never a dependency bump) and from the
# bounds below. If you have not read the skill's "Hard rules" section, read it
# before you run this.
#
#   improve-daemon.sh --repo ~/Code/bonsai --for 6h
#   improve-daemon.sh --repo ~/Code/bonsai --for 90m --dry-run
#   improve-daemon.sh --skill improve-max --repo ~/Code/bonsai --for 3d --args "--target 5x"
#   improve-daemon.sh --status --repo ~/Code/bonsai
#   improve-daemon.sh --stop   --repo ~/Code/bonsai
#
# Run it under nohup, or in a tmux window, if you want it to outlive the shell:
#
#   nohup improve-daemon.sh --repo ~/Code/bonsai --for 6h >/dev/null 2>&1 &
#
set -euo pipefail

readonly VERSION="1.3.0"

# How long one cycle may run before it is killed.
#
# Generous, because a cycle legitimately includes a full iOS build and suite, and
# killing one mid-commit is worse than waiting. It exists for the wedged case: a
# process holding a lock or waiting on a simulator that will never boot, which
# without this pins the whole run behind it for ever.
# For improve-max the default is four hours: one cycle there may be a spike, and a
# spike's own time-box is four hours. Set IMPROVE_CYCLE_TIMEOUT to override either.
CYCLE_TIMEOUT_SECONDS=${IMPROVE_CYCLE_TIMEOUT:-}

# How long to wait between cycles. Short, because the skill decides for itself
# whether there is anything to do and returns quickly when there is not.
CYCLE_GAP_SECONDS=${IMPROVE_CYCLE_GAP:-20}

# A cycle that exits faster than this did not do any work, whatever it printed.
# Used only to detect a crash loop, never to judge a real cycle.
TOO_FAST_SECONDS=${IMPROVE_TOO_FAST:-25}

# Consecutive failures - or too-fast exits - before the daemon gives up.
#
# The failure mode this prevents is the expensive one: a repo whose gate cannot
# run, or an expired credential, turns into hundreds of launches overnight, each
# billing tokens to discover the same thing. Three is enough to rule out a blip.
MAX_CONSECUTIVE_FAILURES=${IMPROVE_MAX_FAILURES:-3}

# The first wait when a cycle ends because the ACCOUNT hit its usage limit.
#
# A limit is not a failure. Nothing about the repo, the gate or the skill is
# wrong, and the only thing another launch will find is the same wall - so a
# limited cycle is kept out of the breaker entirely. What the daemon does not
# know is how long the wall stands, and the CLI's message is not reliable enough
# to plan around: sometimes a reset time, sometimes nothing, sometimes a time in
# a zone this box is not in. So it does not try to parse one. It waits this
# long, then twice that, then twice again, capped below, until a cycle gets
# through. Cheap when the limit lifts soon; never a hot loop when it does not.
LIMIT_WAIT_SECONDS=${IMPROVE_LIMIT_WAIT:-900}
LIMIT_WAIT_CAP_SECONDS=${IMPROVE_LIMIT_WAIT_CAP:-3600}

# Total time the daemon will spend waiting on the account before it gives the
# repo back. The deadline bounds the run, but a limit that never lifts should not
# hold the lock all day.
LIMIT_WAIT_BUDGET_SECONDS=${IMPROVE_LIMIT_WAIT_BUDGET:-14400}

usage() {
  sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

log() {
  # Every line is timestamped and goes to both the log file and stdout, so a
  # daemon under nohup and one in a tmux window are equally readable.
  local line
  line="$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"
  printf '%s\n' "$line"
  [[ -n "${LOG_FILE:-}" ]] && printf '%s\n' "$line" >>"$LOG_FILE"
  return 0
}

die() {
  log "FATAL: $*"
  exit 1
}

# to_seconds turns 90m / 6h / 2d / 3600 into seconds, or returns non-zero.
#
# It reports failure through its EXIT STATUS rather than by calling die, and that
# is not style. It is called inside $( ), so a die here would print into the
# captured output instead of the terminal and its exit would end the subshell
# rather than the script: `--for 6weeks` failed silently, with no message at all,
# until this was written the other way round.
to_seconds() {
  local raw="$1" n unit
  [[ "$raw" =~ ^([0-9]+)([smhd]?)$ ]] || return 1
  n="${BASH_REMATCH[1]}"
  unit="${BASH_REMATCH[2]:-s}"
  case "$unit" in
    s) echo "$n" ;;
    m) echo $((n * 60)) ;;
    h) echo $((n * 3600)) ;;
    d) echo $((n * 86400)) ;;
  esac
}

# human turns seconds back into something a log line can say.
human() {
  local s="$1"
  if ((s >= 3600)); then printf '%dh%02dm' $((s / 3600)) $(((s % 3600) / 60))
  elif ((s >= 60)); then printf '%dm' $((s / 60))
  else printf '%ds' "$s"; fi
}

# hit_usage_limit reads one cycle's output and reports, via exit status, whether
# the cycle ended on an account limit rather than on its own work. The phrases are
# the ones the CLI and the API actually print. They are deliberately narrower than
# "rate limit": the security finder's own brief says "missing rate limiting on auth
# endpoints", so a successful cycle that reported such a finding would otherwise
# read as a limited one and put the daemon to sleep. Matched against the cycle's
# own slice of the log, never the whole file, and only for a cycle that exited
# non-zero - a cycle that finished cleanly was not limited.
hit_usage_limit() {
  grep -qiE "hit your (usage )?limit|usage limit reached|rate_limit_error|429 too many requests|out of extra usage" "$1"
}

# journal_gap writes the wait into the run's journal, next to the hourly reports.
# The human reads journal.md afterwards to judge the run; a six-hour run that
# spent two of them waiting on the account should say so in the same place as
# its results, not only in a daemon log nobody opens.
journal_gap() {
  local seconds="$1" nth="$2" tree
  # Say what state the tree is actually in rather than asserting nothing was
  # lost: a limit can land mid-item, and then the next cycle will refuse the
  # dirty tree, which is exactly what the human needs to know here.
  if [[ -n "$(dirty_tree)" ]]; then
    tree="The cycle stopped mid-item: the working tree is dirty and the next cycle's preflight will refuse it until someone commits or reverts."
  else
    tree="The working tree is clean; the cycle in flight committed or reverted before it stopped."
  fi
  printf '\n## %s - daemon: account usage limit\n\nCycle ended on the account limit; waiting %s before the next attempt (wait %s). %s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(human "$seconds")" "$nth" "$tree" >>"$STATE_DIR/journal.md"
}

REPO=""
DURATION=""
DRY_RUN=0
ACTION="run"
MODEL="${IMPROVE_MODEL:-}"
SKILL="improve"
# Extra skill arguments (e.g. improve-max's --target / --kinds), appended to the
# invocation verbatim on every cycle. The skill writes them into run.json at
# preflight and reads them from there afterwards, so passing them each cycle is
# belt and braces: it is what makes the first cycle right.
EXTRA_ARGS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="${2:-}"; shift 2 ;;
    --for|--duration) DURATION="${2:-}"; shift 2 ;;
    --model) MODEL="${2:-}"; shift 2 ;;
    --skill) SKILL="${2:-}"; shift 2 ;;
    --args) EXTRA_ARGS="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --status) ACTION="status"; shift ;;
    --stop) ACTION="stop"; shift ;;
    --version) echo "improve-daemon $VERSION"; exit 0 ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 1 ;;
  esac
done

[[ -n "$REPO" ]] || { echo "--repo is required" >&2; usage 1; }
case "$SKILL" in
  improve) : "${CYCLE_TIMEOUT_SECONDS:=3600}" ;;
  improve-max) : "${CYCLE_TIMEOUT_SECONDS:=14400}" ;;
  *) echo "--skill must be improve or improve-max, not '$SKILL'" >&2; usage 1 ;;
esac
REPO="$(cd "$REPO" 2>/dev/null && pwd)" || die "no such directory: $REPO"

readonly STATE_DIR="$REPO/.improve"
readonly LOCK_FILE="$STATE_DIR/daemon.lock"
readonly LOG_FILE="$STATE_DIR/daemon.log"
readonly RUN_FILE="$STATE_DIR/run.json"

mkdir -p "$STATE_DIR"

# ---------------------------------------------------------------- status / stop

lock_pid() { [[ -f "$LOCK_FILE" ]] && head -1 "$LOCK_FILE" 2>/dev/null || true; }
lock_alive() {
  local pid; pid="$(lock_pid)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

if [[ "$ACTION" == "status" ]]; then
  if lock_alive; then
    echo "running: pid $(lock_pid), started $(sed -n 2p "$LOCK_FILE" 2>/dev/null)"
  elif [[ -f "$LOCK_FILE" ]]; then
    echo "not running (stale lock from pid $(lock_pid); it will be cleared on the next start)"
  else
    echo "not running"
  fi
  [[ -f "$RUN_FILE" ]] && echo "ledger: $(tr -d '\n ' <"$RUN_FILE" | grep -o '"outcome":"[^"]*"' || true)"
  [[ -f "$LOG_FILE" ]] && { echo "--- last 10 log lines ---"; tail -10 "$LOG_FILE"; }
  exit 0
fi

if [[ "$ACTION" == "stop" ]]; then
  if lock_alive; then
    pid="$(lock_pid)"
    # TERM the daemon, not the cycle. The daemon's trap lets the cycle in flight
    # finish, because killing an agent mid-commit is the one state that leaves a
    # working tree nobody can cheaply reason about.
    kill -TERM "$pid" 2>/dev/null && echo "asked daemon $pid to stop after the cycle in flight"
  else
    echo "not running"
    rm -f "$LOCK_FILE"
  fi
  exit 0
fi

# ---------------------------------------------------------------------- preflight

[[ -n "$DURATION" ]] || { echo "--for is required (e.g. --for 6h)" >&2; usage 1; }
command -v claude >/dev/null || die "the claude CLI is not on PATH"
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || die "$REPO is not a git repository"

TOTAL_SECONDS="$(to_seconds "$DURATION")" \
  || die "cannot read a duration from '$DURATION' - try 90m, 6h, 2d or a plain number of seconds"
((TOTAL_SECONDS > 0)) || die "--for must be more than zero"

# dirty_tree reports uncommitted work, EXCLUDING the loop's own ledger.
#
# .improve/ is excluded for two reasons and the first one is a bug this had:
# mkdir'ing the state directory made the tree dirty, so on any repo that had not
# yet gitignored it the daemon refused to start because of a directory it had
# just created itself. The second is that the ledger changes constantly during a
# run by design, and counting it as unfinished work would be wrong even mid-run.
dirty_tree() { git -C "$REPO" status --porcelain -- ':!.improve'; }

# A dirty tree, refused HERE rather than by the agent.
#
# The skill refuses it too, but discovering that costs a full session launch and
# tells you through a log file. More importantly the daemon would then relaunch
# into the same refusal until the failure breaker tripped.
if [[ -n "$(dirty_tree)" ]]; then
  dirty_tree | head -20
  die "the working tree is dirty; commit or stash first (the loop's safety rests on being able to undo exactly one item)"
fi

if lock_alive; then
  die "a daemon is already running for this repo (pid $(lock_pid)); use --status or --stop"
fi
rm -f "$LOCK_FILE"

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
DEADLINE_EPOCH=$(($(date +%s) + TOTAL_SECONDS))

if ((DRY_RUN)); then
  echo "would run, once per cycle, until $(date -u -r "$DEADLINE_EPOCH" +%Y-%m-%dT%H:%M:%SZ):"
  echo
  echo "  cd $REPO && claude -p '/$SKILL $DURATION $REPO${EXTRA_ARGS:+ $EXTRA_ARGS} ...' \\"
  echo "    --permission-mode bypassPermissions \\"
  [[ -n "$MODEL" ]] && echo "    --model $MODEL \\"
  echo "    --output-format text"
  echo
  echo "cycle timeout $(human "$CYCLE_TIMEOUT_SECONDS"), gap $(human "$CYCLE_GAP_SECONDS"), give up after $MAX_CONSECUTIVE_FAILURES consecutive failures"
  echo "on an account usage limit: wait $(human "$LIMIT_WAIT_SECONDS"), doubling to $(human "$LIMIT_WAIT_CAP_SECONDS"), at most $(human "$LIMIT_WAIT_BUDGET_SECONDS") in total"
  exit 0
fi

printf '%s\n%s\n' "$$" "$STARTED_AT" >"$LOCK_FILE"

STOPPING=0
on_term() {
  # Set a flag rather than exiting: a cycle in flight is holding a working tree
  # and possibly a half-applied edit, and the skill's own boundary rule is to
  # finish and verify the item in hand before stopping.
  STOPPING=1
  log "stop requested; will exit after the cycle in flight"
}
trap on_term TERM INT
cleanup() { rm -f "$LOCK_FILE"; }
trap cleanup EXIT

# ---------------------------------------------------------------------- the loop

log "daemon $VERSION starting: skill=$SKILL${EXTRA_ARGS:+ args='$EXTRA_ARGS'} repo=$REPO for=$DURATION deadline=$(date -u -r "$DEADLINE_EPOCH" +%Y-%m-%dT%H:%M:%SZ)"
log "one cycle per launch; state carried in $STATE_DIR"

cycle=0
failures=0
limit_waits=0
limit_wait_next=$LIMIT_WAIT_SECONDS
limit_waited_total=0

while :; do
  now=$(date +%s)
  remaining=$((DEADLINE_EPOCH - now))

  if ((STOPPING)); then
    log "stopped on request after $cycle cycle(s)"
    break
  fi
  if ((remaining <= 0)); then
    log "deadline reached after $cycle cycle(s)"
    break
  fi
  if ((failures >= MAX_CONSECUTIVE_FAILURES)); then
    log "giving up: $failures consecutive failures. Something is wrong that another launch will not fix -"
    log "  check the tail of this log, then try one cycle by hand: cd $REPO && claude"
    break
  fi
  if ((limit_waited_total >= LIMIT_WAIT_BUDGET_SECONDS)); then
    log "giving up: $(human "$limit_waited_total") spent waiting on the account limit; releasing the repo"
    break
  fi

  cycle=$((cycle + 1))

  # The remaining time is passed, not the original duration, so a cycle that
  # re-runs preflight cannot quietly extend the run. The skill prefers the
  # deadline already in run.json when a run is in progress; this is what makes
  # the two agree on the first cycle and stay agreed after a restart.
  remaining_arg="$(human "$remaining")"

  prompt="/$SKILL $remaining_arg $REPO${EXTRA_ARGS:+ $EXTRA_ARGS}

You are ONE CYCLE of an externally paced loop. A daemon relaunches you; you are
not the thing that keeps this going.

- Do NOT call ScheduleWakeup. It would die with this session anyway, and the
  daemon is what re-enters. Finish the cycle and stop.
- A run already in progress keeps the deadline recorded in .improve/run.json.
  The duration above is what remains, for the case where there is no run yet.
- Everything you want the next cycle to know goes in .improve/, not in a summary
  you print. The next cycle starts with no memory of this one.
- If the deadline has passed, write the final summary, set the outcome in
  run.json, and stop without starting new work."

  log "cycle $cycle starting ($(human "$remaining") left)"
  started=$(date +%s)

  # The cycle's output goes to its own file as well as the shared log, so the
  # limit check below reads only this cycle and not everything before it.
  cycle_out="$STATE_DIR/cycle-$cycle.out"
  : >"$cycle_out"

  set +e
  # A subshell so a cd cannot leak, and the timeout so a wedged cycle cannot pin
  # the run. `timeout` is coreutils; gtimeout on a mac with brew coreutils.
  TIMEOUT_BIN="$(command -v timeout || command -v gtimeout || true)"
  if [[ -n "$TIMEOUT_BIN" ]]; then
    ( cd "$REPO" && "$TIMEOUT_BIN" "$CYCLE_TIMEOUT_SECONDS" \
        claude -p "$prompt" \
          --permission-mode bypassPermissions \
          ${MODEL:+--model "$MODEL"} \
          --output-format text ) 2>&1 | tee -a "$LOG_FILE" >"$cycle_out"
    rc=${PIPESTATUS[0]}
  else
    # No timeout available. Said out loud rather than silently dropped, because
    # the wedged-cycle protection is gone and that changes what this can promise.
    ( cd "$REPO" && claude -p "$prompt" \
          --permission-mode bypassPermissions \
          ${MODEL:+--model "$MODEL"} \
          --output-format text ) 2>&1 | tee -a "$LOG_FILE" >"$cycle_out"
    rc=${PIPESTATUS[0]}
  fi
  set -e

  elapsed=$(( $(date +%s) - started ))

  if ((rc != 0)) && hit_usage_limit "$cycle_out"; then
    # Not a failure and not a success: the account is the bottleneck, not the
    # repo. Leave the breaker alone, back off, try again.
    wait_s=$limit_wait_next
    limit_waits=$((limit_waits + 1))
    rm -f "$cycle_out"
    if ((wait_s > remaining)); then
      log "cycle $cycle hit the account usage limit; the next wait ($(human "$wait_s")) would pass the deadline, stopping"
      break
    fi
    log "cycle $cycle hit the account usage limit after $(human "$elapsed"); waiting $(human "$wait_s") (wait $limit_waits, $(human "$limit_waited_total") so far)"
    journal_gap "$wait_s" "$limit_waits"
    sleep "$wait_s"
    limit_waited_total=$((limit_waited_total + wait_s))
    limit_wait_next=$((wait_s * 2))
    ((limit_wait_next > LIMIT_WAIT_CAP_SECONDS)) && limit_wait_next=$LIMIT_WAIT_CAP_SECONDS
    continue
  fi
  # A cycle that got through resets the backoff, whatever else it did.
  limit_wait_next=$LIMIT_WAIT_SECONDS
  rm -f "$cycle_out"

  if ((rc == 124)); then
    failures=$((failures + 1))
    log "cycle $cycle TIMED OUT after $(human "$elapsed") (failure $failures/$MAX_CONSECUTIVE_FAILURES)"
  elif ((rc != 0)); then
    failures=$((failures + 1))
    log "cycle $cycle exited $rc after $(human "$elapsed") (failure $failures/$MAX_CONSECUTIVE_FAILURES)"
  elif ((elapsed < TOO_FAST_SECONDS)); then
    # Exit 0 in a couple of seconds means it did not work, whatever it printed:
    # a refused launch, a dirty tree, an auth prompt it could not answer. Counted
    # as a failure so a crash loop still trips the breaker.
    failures=$((failures + 1))
    log "cycle $cycle returned in $(human "$elapsed") - too fast to have done anything (failure $failures/$MAX_CONSECUTIVE_FAILURES)"
  else
    failures=0
    log "cycle $cycle finished in $(human "$elapsed")"
  fi

  # The tree is the daemon's business between cycles. A cycle that left work
  # uncommitted is a cycle that died mid-item, and the next one would refuse to
  # start on it - so say so here, where somebody reading the log can see it.
  if [[ -n "$(dirty_tree)" ]]; then
    log "WARNING: the working tree is dirty after cycle $cycle; the next cycle's preflight will refuse it"
    dirty_tree | head -10 >>"$LOG_FILE"
  fi

  sleep "$CYCLE_GAP_SECONDS"
done

branch="$(git -C "$REPO" rev-parse --abbrev-ref HEAD)"
log "daemon finished. branch=$branch cycles=$cycle"
log "review with: git -C $REPO log --oneline $branch"
