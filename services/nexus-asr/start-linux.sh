#!/usr/bin/env bash
# Nexus ASR Server — Linux ARM64 start (after install-offline.sh)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "No .venv found. Run ./install-offline.sh first." >&2
  exit 1
fi

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

.venv/bin/python -m app.preflight

HOST="${NEXUS_ASR_HOST:-127.0.0.1}"
PORT="${NEXUS_ASR_PORT:-19899}"
exec .venv/bin/uvicorn app.main:app \
  --host "$HOST" \
  --port "$PORT" \
  --workers 1 \
  --proxy-headers \
  --forwarded-allow-ips=127.0.0.1
