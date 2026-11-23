#!/usr/bin/env bash
# Repo-root convenience launcher for the Microduck console monitor.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
exec python3 "$ROOT/console/linux-v297/tools/duck_monitor.py" "$@"
