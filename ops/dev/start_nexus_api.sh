#!/usr/bin/env bash
# Nexus API on 18120. Chat model config comes from NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG.
# usage: ops/dev/start_nexus_api.sh [start|stop|status]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/ops/dev/load_env.sh"
ACTION="${1:-start}"
export NEXUS_DATA_DIR="${NEXUS_DATA_DIR:-$ROOT/services/nexus-api/data}"
export NEXUS_AGENTSCOPE_BASE_URL="${NEXUS_AGENTSCOPE_BASE_URL:-http://127.0.0.1:18000}"
export NEXUS_AGENTSCOPE_USER_ID="${NEXUS_AGENTSCOPE_USER_ID:-nexus}"
export NEXUS_TASK_MCP_URL="${NEXUS_TASK_MCP_URL:-http://127.0.0.1:18202/mcp}"
export NEXUS_CHAT_PROVIDER="${NEXUS_CHAT_PROVIDER:-openai}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://api.deepseek.com}"
export OPENAI_MODEL="${OPENAI_MODEL:-deepseek-flash}"
if [ -z "${OPENAI_API_KEY:-}" ] && [ -n "${DEEPSEEK_API_KEY:-}" ]; then
  export OPENAI_API_KEY="$DEEPSEEK_API_KEY"
fi
PY="$ROOT/services/nexus-api/.venv/bin/python"
case "$ACTION" in
  start|stop|status)
    exec "$PY" "$ROOT/ops/dev/service_ctl.py" nexus-api "$ACTION"
    ;;
  *)
    echo "usage: $0 [start|stop|status]" >&2
    exit 2
    ;;
esac
