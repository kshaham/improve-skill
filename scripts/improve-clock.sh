#!/usr/bin/env bash
# 0 = running, 10 = deadline reached, 2 = invalid state.
set -euo pipefail
exec python3 "$(cd -- "$(dirname -- "$0")" && pwd)/improve_runtime.py" clock "$@"
