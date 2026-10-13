#!/usr/bin/env bash
# Development launcher (POSIX). Windows uses scripts\start_backend.bat; both call
# the same Python entry point so the two platforms cannot drift.
set -euo pipefail
cd "$(dirname "$0")"
PYTHON="${PYTHON:-.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  echo "[run_dev] no virtual environment at $PYTHON — creating one"
  python3 -m venv .venv
  ./.venv/bin/python -m pip install --quiet --upgrade pip
  ./.venv/bin/python -m pip install --quiet -r requirements.txt
fi
if [ ! -f .env.dev ]; then
  echo "[run_dev] no .env.dev — generating one with fresh secrets"
  "$PYTHON" scripts/bootstrap_env.py
fi
exec "$PYTHON" run_dev.py "$@"
