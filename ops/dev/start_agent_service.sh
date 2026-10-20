#!/usr/bin/env bash
# AgentScope Agent Service on 18000.
# usage: ops/dev/start_agent_service.sh [start|stop|status]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/ops/dev/load_env.sh"
ACTION="${1:-start}"
export NEXUS_API_BASE="${NEXUS_API_BASE:-http://127.0.0.1:18120}"
export AGENTSCOPE_HOST="${AGENTSCOPE_HOST:-0.0.0.0}"
export AGENTSCOPE_REDIS_DB="${AGENTSCOPE_REDIS_DB:-1}"
export AGENTSCOPE_PORT="${AGENTSCOPE_PORT:-18000}"
export AGENTSCOPE_REDIS_HOST="${AGENTSCOPE_REDIS_HOST:-127.0.0.1}"
export AGENTSCOPE_REDIS_PORT="${AGENTSCOPE_REDIS_PORT:-6379}"
export AGENTSCOPE_WORKSPACE_DIR="${AGENTSCOPE_WORKSPACE_DIR:-$ROOT/services/agent-service/data/workspaces}"
export NEXUS_AGENTSCOPE_USER_ID="${NEXUS_AGENTSCOPE_USER_ID:-nexus}"
PY="$ROOT/services/nexus-api/.venv/bin/python"
case "$ACTION" in
  start|stop|status)
    exec "$PY" "$ROOT/ops/dev/service_ctl.py" agent "$ACTION"
    ;;
  *)
    echo "usage: $0 [start|stop|status]" >&2
    exit 2
    ;;
esac
