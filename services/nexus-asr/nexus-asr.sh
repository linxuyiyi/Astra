#!/usr/bin/env bash
# Nexus ASR service manager (macOS/Linux, background process).
#
#   ./nexus-asr.sh start|stop|restart|status|health|logs
#
# The model is verified before the process starts; a missing model is
# downloaded only when auto_download is enabled (default).
set -euo pipefail
cd "$(dirname "$0")"

ACTION="${1:-start}"
RUN_DIR="${NEXUS_ASR_RUN_DIR:-.run}"
PID_FILE="$RUN_DIR/nexus-asr.pid"
LOG_FILE="${NEXUS_ASR_LOG:-$RUN_DIR/nexus-asr.log}"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

HOST="${NEXUS_ASR_HOST:-127.0.0.1}"
PORT="${NEXUS_ASR_PORT:-19899}"
BASE="http://${HOST}:${PORT}"

mkdir -p "$RUN_DIR"

is_running() {
  [ -f "$PID_FILE" ] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  ps -p "$pid" -o command= 2>/dev/null | grep -q 'app.main:app' || return 1
}

wait_health() {
  local deadline=$((SECONDS + ${NEXUS_ASR_START_TIMEOUT:-60}))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if curl -fsS -m 2 "$BASE/health" >/dev/null 2>&1; then
      return 0
    fi
    if ! is_running; then
      return 1
    fi
    sleep 1
  done
  return 1
}

start() {
  if is_running; then
    echo "nexus-asr already running (pid $(cat "$PID_FILE")) at $BASE"
    return 0
  fi
  if [ ! -x .venv/bin/python ]; then
    echo "No virtualenv found; running ./setup-mac.sh ..."
    ./setup-mac.sh
  fi
  echo "Checking model files..."
  .venv/bin/python -m app.preflight
  echo "Starting nexus-asr on $BASE ..."
  nohup .venv/bin/uvicorn app.main:app \
    --host "$HOST" \
    --port "$PORT" \
    --workers 1 \
    --proxy-headers \
    --forwarded-allow-ips=127.0.0.1 \
    >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  if ! wait_health; then
    echo "nexus-asr failed to become healthy; see $LOG_FILE" >&2
    tail -20 "$LOG_FILE" >&2 || true
    stop || true
    return 1
  fi
  echo "ready: $BASE (pid $(cat "$PID_FILE"), log $LOG_FILE)"
}

stop() {
  if ! is_running; then
    rm -f "$PID_FILE"
    echo "nexus-asr is not running."
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  echo "Stopping nexus-asr (pid $pid) ..."
  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 30); do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PID_FILE"
      echo "nexus-asr stopped."
      return 0
    fi
    sleep 0.5
  done
  echo "Forcing nexus-asr to stop ..." >&2
  kill -KILL "$pid" 2>/dev/null || true
  rm -f "$PID_FILE"
}

status() {
  if is_running; then
    echo "nexus-asr running (pid $(cat "$PID_FILE")) at $BASE"
    curl -fsS -m 3 "$BASE/health" || true
    echo
    return 0
  fi
  echo "nexus-asr is not running."
  return 1
}

health() {
  curl -fsS -m 5 "$BASE/health"
  echo
  curl -fsS -m 5 "$BASE/v1/models"
  echo
}

case "$ACTION" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  health) health ;;
  logs) tail -n "${2:-80}" -f "$LOG_FILE" ;;
  *) echo "usage: $0 [start|stop|restart|status|health|logs]" >&2; exit 2 ;;
esac
