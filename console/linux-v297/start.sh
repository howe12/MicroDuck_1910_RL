#!/usr/bin/env bash
# Microduck Console Studio - Linux launcher
# Keeps the original console.py untouched except for the Python 3.11 f-string fix.
set -euo pipefail
cd -- "$(dirname -- "$0")"

PY="${MICRODUCK_PYTHON:-}"
if [ -z "$PY" ]; then
  for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && \
       "$candidate" -c 'import sys, tomllib; assert sys.version_info >= (3, 11)' >/dev/null 2>&1; then
      PY="$candidate"
      break
    fi
  done
fi

if [ -z "$PY" ]; then
  cat >&2 <<'MSG'
错误：没有找到 Python 3.11 或更高版本。

Ubuntu / Debian:
  sudo apt install python3.11 python3.11-venv openssh-client

如果系统只有 Python 3.10，请安装 3.11/3.12，或使用仓库中的补丁说明。
MSG
  exit 1
fi

command -v ssh >/dev/null 2>&1 || echo "警告：未找到 ssh 客户端；连接 Zero 前请安装 openssh-client。" >&2
[ -x ssh-askpass ] || chmod +x ssh-askpass 2>/dev/null || true

args=(--dashboard --feetech)
if [ "${MICRODUCK_NO_OPEN:-0}" != "1" ]; then
  args+=(--open)
fi
if [ -n "${MICRODUCK_PORT:-}" ]; then
  args+=(--port "$MICRODUCK_PORT")
fi

exec "$PY" console.py "${args[@]}" "$@"
