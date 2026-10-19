#!/usr/bin/env bash
# Nexus v1.4.4 真实 Linux systemd smoke 测试。必须由审查者在目标主机执行。
# 本脚本会安装/升级、重启服务、杀死 Celery worker 并验证 systemd 自恢复。
# 安全门：必须显式设置 NEXUS_SMOKE_EXECUTE=1，且 /etc/nexus/nexus.env 已填写。
set -Eeuo pipefail

if [ "${NEXUS_SMOKE_EXECUTE:-0}" != 1 ]; then
  echo "未执行：设置 NEXUS_SMOKE_EXECUTE=1 后再在目标 Linux 主机运行。" >&2
  exit 2
fi
[ "$(uname -s)" = Linux ] || { echo "仅支持 Linux" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "缺少 systemctl" >&2; exit 1; }
[ -f /etc/nexus/nexus.env ] || { echo "缺少 /etc/nexus/nexus.env" >&2; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
CLI="$ROOT/ops/deploy/install_nexus.sh"

echo "== preflight =="
"$CLI" preflight

echo "== install/upgrade =="
"$CLI" install

echo "== status =="
"$CLI" status

echo "== restart required services =="
"$CLI" restart
"$CLI" health

echo "== kill one Celery worker; systemd must restart it =="
UNIT=nexus-runtime-control
PID="$(systemctl show -p MainPID --value "$UNIT.service")"
[ -n "$PID" ] && [ "$PID" != 0 ] || { echo "$UNIT 没有 MainPID" >&2; exit 1; }
kill -9 "$PID"
for _ in $(seq 1 60); do
  NEW_PID="$(systemctl show -p MainPID --value "$UNIT.service")"
  if [ -n "$NEW_PID" ] && [ "$NEW_PID" != 0 ] && [ "$NEW_PID" != "$PID" ]; then
    break
  fi
  sleep 1
done
NEW_PID="$(systemctl show -p MainPID --value "$UNIT.service")"
[ "$NEW_PID" != "$PID" ] || { echo "$UNIT 未被 systemd 重启" >&2; exit 1; }
"$CLI" health

echo "== failure injection: health must fail when a required unit is stopped =="
systemctl stop nexus-api.service
if "$CLI" health >/dev/null 2>&1; then
  echo "健康检查在 nexus-api 停止后仍返回成功" >&2
  systemctl start nexus-api.service || true
  exit 1
fi
systemctl start nexus-api.service
"$CLI" health

echo "== rollback drill (manual confirmation required) =="
if [ "${NEXUS_SMOKE_ROLLBACK:-0}" = 1 ]; then
  "$CLI" rollback
  "$CLI" health
fi

echo "Linux smoke 通过。"
