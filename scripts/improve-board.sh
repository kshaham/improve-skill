#!/usr/bin/env bash
# Serve the improvement ledger as a local Kanban board.
set -euo pipefail
# Resolve the real script directory: through symlinks (an installed link to this file), with
# CDPATH cleared so `cd` neither prints a path nor lands in a same-named directory elsewhere.
script_path="${BASH_SOURCE[0]}"
while [ -L "$script_path" ]; do
  link_dir="$(CDPATH= cd -P -- "$(dirname -- "$script_path")" >/dev/null && pwd)"
  script_path="$(readlink -- "$script_path")"
  case "$script_path" in
    /*) ;;
    *) script_path="$link_dir/$script_path" ;;
  esac
done
script_dir="$(CDPATH= cd -P -- "$(dirname -- "$script_path")" >/dev/null && pwd)"
exec python3 "$script_dir/improve_board.py" "$@"
