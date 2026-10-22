#!/usr/bin/env bash
# Gateway data plane on 18110. Chat, embeddings and ASR all go through it.
# usage: ops/dev/start_gateway.sh [start|stop|status]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/ops/dev/load_env.sh"
ACTION="${1:-start}"
export ASTRA_SERVICE_GATEWAY_PORT="${ASTRA_SERVICE_GATEWAY_PORT:-18110}"
PY="$ROOT/services/nexus-api/.venv/bin/python"
case "$ACTION" in
  start|stop|status)
    exec "$PY" "$ROOT/ops/dev/service_ctl.py" gateway "$ACTION"
    ;;
  *)
    echo "usage: $0 [start|stop|status]" >&2
    exit 2
    ;;
esac
