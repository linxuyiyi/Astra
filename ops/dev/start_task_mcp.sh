#!/usr/bin/env bash
# Task MCP on NEXUS_TASK_MCP_PORT (default 18202).
# usage: ops/dev/start_task_mcp.sh [start|stop|status]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/ops/dev/load_env.sh"
ACTION="${1:-start}"
export NEXUS_TASK_MCP_HOST="${NEXUS_TASK_MCP_HOST:-127.0.0.1}"
export NEXUS_TASK_MCP_PORT="${NEXUS_TASK_MCP_PORT:-18202}"
PY="$ROOT/services/nexus-api/.venv/bin/python"
case "$ACTION" in
  start|stop|status)
    exec "$PY" "$ROOT/ops/dev/service_ctl.py" task-mcp "$ACTION"
    ;;
  *)
    echo "usage: $0 [start|stop|status]" >&2
    exit 2
    ;;
esac
