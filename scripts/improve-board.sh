#!/usr/bin/env bash
# Serve the improvement ledger as a local Kanban board.
set -euo pipefail
exec python3 "$(cd -- "$(dirname -- "$0")" && pwd)/improve_board.py" "$@"
