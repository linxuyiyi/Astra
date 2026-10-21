#!/usr/bin/env bash
# Create/refresh the local virtualenv on macOS. Does not touch system Python.
set -euo pipefail
cd "$(dirname "$0")"

if [ -x .venv/bin/python ] && [ "${1:-}" != "--force" ]; then
  echo ".venv already exists; use ./setup-mac.sh --force to recreate"
  exit 0
fi

PY="${PYTHON:-}"
if [ -z "$PY" ] && command -v python3.12 >/dev/null 2>&1; then
  PY="$(command -v python3.12)"
fi
if [ -z "$PY" ] && command -v uv >/dev/null 2>&1; then
  echo "Installing a managed Python 3.12 with uv..."
  uv python install 3.12 >/dev/null
  PY="$(uv python find 3.12)"
fi
if [ -z "$PY" ]; then
  echo "Python 3.12 not found. Install it (e.g. 'uv python install 3.12' or Homebrew) and retry." >&2
  exit 1
fi

"$PY" --version
if [ "${1:-}" = "--force" ]; then
  rm -rf .venv
fi
"$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r requirements-dev.txt
echo "Done. Start with ./start-mac.sh"
