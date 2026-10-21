#!/usr/bin/env bash
# Nexus ASR Server — macOS local start
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "No virtualenv found; running ./setup-mac.sh ..."
  ./setup-mac.sh
fi

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

echo "Checking model files..."
.venv/bin/python -m app.preflight

HOST="${NEXUS_ASR_HOST:-127.0.0.1}"
PORT="${NEXUS_ASR_PORT:-${PORT:-19899}}"
echo
echo "Nexus ASR Server: http://${HOST}:${PORT}/"
echo "  health : /health"
echo "  models : /api/asr/models"
echo "  test   : http://${HOST}:${PORT}/  (open in Chrome)"
echo

exec .venv/bin/uvicorn app.main:app \
  --host "$HOST" \
  --port "$PORT" \
  --workers 1 \
  --proxy-headers \
  --forwarded-allow-ips=127.0.0.1
