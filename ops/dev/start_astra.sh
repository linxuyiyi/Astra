#!/usr/bin/env bash
# Task platform supervisor: Task API 18100, gateway 18110, Celery, OpenCode, Nexus Web 15174.
# usage: ops/dev/start_astra.sh [start|restart|stop|status]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/ops/dev/load_env.sh"
ACTION="${1:-start}"
export PATH="${HOME}/.local/bin:${PATH}"
export ASTRA_GATEWAY_PROBE_TIMEOUT_SECONDS="${ASTRA_GATEWAY_PROBE_TIMEOUT_SECONDS:-30}"
export NEXUS_MCP_TRANSPORT="${NEXUS_MCP_TRANSPORT:-streamable-http}"
export ASTRA_REDIS_URL="${ASTRA_REDIS_URL:-redis://127.0.0.1:6379/1}"
export ASTRA_OUTBOX_INTERVAL="${ASTRA_OUTBOX_INTERVAL:-2}"
export ASTRA_OUTBOX_RETRY_SECONDS="${ASTRA_OUTBOX_RETRY_SECONDS:-5}"
export ASTRA_OPENCODE_MODEL="${ASTRA_OPENCODE_MODEL:-deepseek/deepseek-flash}"
export ASTRA_NEXUS_API_BASE="${ASTRA_NEXUS_API_BASE:-http://127.0.0.1:18120}"
export ASTRA_AGENTSCOPE_BASE_URL="${ASTRA_AGENTSCOPE_BASE_URL:-http://127.0.0.1:18000}"
export ASTRA_AGENTSCOPE_USER_ID="${ASTRA_AGENTSCOPE_USER_ID:-nexus}"
export ASTRA_LLM_WIKI_API_BASE_URL="${ASTRA_LLM_WIKI_API_BASE_URL:-http://127.0.0.1:19828/api/v1}"
PY="$ROOT/services/task-platform/.venv/bin/python"
cd "$ROOT/services/task-platform"
case "$ACTION" in
  start|restart|stop|status)
    exec "$PY" "$ROOT/ops/dev/astra_service.py" dev "$ACTION"
    ;;
  *)
    echo "usage: $0 [start|restart|stop|status]" >&2
    exit 2
    ;;
esac
