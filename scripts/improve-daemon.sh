#!/usr/bin/env bash
# Supervise fresh Codex or Claude Code cycles until the persisted deadline.
set -euo pipefail
exec python3 "$(cd -- "$(dirname -- "$0")" && pwd)/improve_runtime.py" daemon "$@"
