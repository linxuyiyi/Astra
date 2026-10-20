#!/usr/bin/env bash
# 检查 Redis 与 AgentScope /health。Agent Service 的健康检查需要 X-User-ID。
set -euo pipefail
BASE="${NEXUS_AGENTSCOPE_BASE_URL:-http://127.0.0.1:18000}"
USER_ID="${NEXUS_AGENTSCOPE_USER_ID:-nexus}"
PORT="${AGENTSCOPE_REDIS_PORT:-6379}"

if ! command -v redis-cli >/dev/null 2>&1; then
  echo "redis-cli 不在 PATH 中" >&2
  exit 1
fi
redis-cli -p "$PORT" ping
curl -fsS -H "X-User-ID: ${USER_ID}" "${BASE%/}/health"
echo
