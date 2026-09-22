#!/usr/bin/env bash
#
# Tells an /improve run what time it is, from .improve/run.json and the system clock.
#
# ## What this exists to fix
#
# A model has no sense of elapsed time. It counts what it can see - cycles, reports,
# items - and converts that into hours. Measured on a real 2h run: the loop wrote
# "hour 2 of 2 (final)" seventeen minutes after it started, said it had "twenty
# minutes left", declined an item as not startable in the time remaining, and did not
# re-arm. The deadline in run.json was right the whole time; nothing read it.
#
# So nothing in the loop reasons about time. It runs this, and uses what it prints:
# the hour label on a report, whether a report is due, whether the run is over.
#
#   improve-clock.sh [repo]              human-readable block, exit status says RUNNING or OVER
#   improve-clock.sh --remaining [repo]  seconds until the deadline (negative once past)
#
# Exit status: 0 the deadline is in the future, 10 it has passed, 2 no usable run.json.

set -euo pipefail

EXIT_RUNNING=0
EXIT_OVER=10
EXIT_NO_RUN=2

mode="block"
if [[ "${1:-}" == "--remaining" ]]; then
  mode="remaining"
  shift
fi
repo="${1:-.}"
run_json="$repo/.improve/run.json"

if [[ ! -f "$run_json" ]]; then
  echo "improve-clock: no run at $run_json" >&2
  exit "$EXIT_NO_RUN"
fi

# to_epoch parses the ISO-8601 UTC stamps run.json holds, on BSD date (macOS) or GNU.
to_epoch() {
  date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$1" +%s 2>/dev/null || date -u -d "$1" +%s
}

human() {
  local s="$1" sign=""
  if ((s < 0)); then sign="-"; s=$((-s)); fi
  if ((s >= 3600)); then printf '%s%dh%02dm' "$sign" $((s / 3600)) $(((s % 3600) / 60))
  elif ((s >= 60)); then printf '%s%dm' "$sign" $((s / 60))
  else printf '%s%ds' "$sign" "$s"; fi
}

started="$(jq -r '.started_at // empty' "$run_json")"
deadline="$(jq -r '.deadline // empty' "$run_json")"
if [[ -z "$started" || -z "$deadline" ]]; then
  echo "improve-clock: $run_json has no started_at or deadline" >&2
  exit "$EXIT_NO_RUN"
fi

now_epoch=$(date -u +%s)
started_epoch=$(to_epoch "$started")
deadline_epoch=$(to_epoch "$deadline")
elapsed=$((now_epoch - started_epoch))
remaining=$((deadline_epoch - now_epoch))
total=$((deadline_epoch - started_epoch))

if [[ "$mode" == "remaining" ]]; then
  echo "$remaining"
  ((remaining > 0)) && exit "$EXIT_RUNNING" || exit "$EXIT_OVER"
fi

# Hours are wall-clock hours since started_at, never cycles or reports.
hour=$((elapsed / 3600 + 1))
hours_total=$(((total + 3599) / 3600))
if ((hour > hours_total)); then hour=$hours_total; fi

last_report_hour="$(jq -r '.last_report_hour // 0' "$run_json")"
if ((remaining <= 0)); then
  status="OVER - write the final report and stop"
  report="final"
elif ((elapsed / 3600 > last_report_hour)); then
  status="RUNNING - keep working; do not write a final report"
  report="hour $((elapsed / 3600)) is due"
else
  status="RUNNING - keep working; do not write a final report"
  report="not due (next at $(human $(((last_report_hour + 1) * 3600 - elapsed))))"
fi

printf 'now        %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'started    %s  elapsed   %s\n' "$started" "$(human "$elapsed")"
printf 'deadline   %s  remaining %s\n' "$deadline" "$(human "$remaining")"
printf 'hour       %d of %d\n' "$hour" "$hours_total"
printf 'report     %s\n' "$report"
printf 'status     %s\n' "$status"

((remaining > 0)) && exit "$EXIT_RUNNING" || exit "$EXIT_OVER"
