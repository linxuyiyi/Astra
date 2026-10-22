#!/usr/bin/env bash
# Repeatable local acceptance for ASR -> gateway -> Nexus speech input.
#
#   ./scripts/verify-deployment.sh
#
# Requires the ASR service, Nexus gateway (18110) and Nexus API (18120) to be
# running. It uses the local test WAV, never user audio.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$ROOT/../.." && pwd)"
ASR_BASE="${NEXUS_ASR_BASE:-http://127.0.0.1:19899}"
GATEWAY_BASE="${NEXUS_GATEWAY_BASE:-http://127.0.0.1:18110/v1}"
NEXUS_BASE="${NEXUS_BASE:-http://127.0.0.1:18120}"
QA_USERNAME="${NEXUS_QA_USERNAME:-local-qa}"
QA_PASSWORD="${NEXUS_QA_PASSWORD:-local-qa-password}"
SAMPLE="${NEXUS_SAMPLE:-$ROOT/testdata/zh-5s.wav}"
EXPECT="${NEXUS_EXPECT:-会议纪要}"

if [ -z "${NEXUS_GATEWAY_API_KEY:-}" ] && [ -f "$REPO/ops/env/.env" ]; then
  NEXUS_GATEWAY_API_KEY="$(sed -n 's/^NEXUS_GATEWAY_API_KEY=//p' "$REPO/ops/env/.env" | tail -1 | tr -d "'\"")"
fi
[ -n "${NEXUS_GATEWAY_API_KEY:-}" ] || { echo "NEXUS_GATEWAY_API_KEY is not configured" >&2; exit 1; }
[ -f "$SAMPLE" ] || { echo "missing sample: $SAMPLE" >&2; exit 1; }

case "$NEXUS_BASE" in
  http://127.0.0.1:*|http://localhost:*) ;;
  *) echo "QA login is restricted to localhost; do not run it against a shared Nexus" >&2; exit 1 ;;
esac
COOKIE_JAR="$(mktemp)"
trap 'rm -f "$COOKIE_JAR"' EXIT
AUTH_BODY="{\"username\":\"$QA_USERNAME\",\"password\":\"$QA_PASSWORD\",\"display_name\":\"Local QA\"}"
auth_status="$(curl -s -o /dev/null -w '%{http_code}' -c "$COOKIE_JAR" -m 10 \
  -X POST -H 'Content-Type: application/json' -d "$AUTH_BODY" \
  "$NEXUS_BASE/api/v1/auth/register" 2>/dev/null || true)"
if [ "$auth_status" = "409" ]; then
  auth_status="$(curl -s -o /dev/null -w '%{http_code}' -c "$COOKIE_JAR" -m 10 \
    -X POST -H 'Content-Type: application/json' -d "$AUTH_BODY" \
    "$NEXUS_BASE/api/v1/auth/login" 2>/dev/null || true)"
fi

pass=0
fail=0
check() {
  local name="$1" ok="$2" detail="${3:-}"
  if [ "$ok" = "1" ]; then
    printf '  ✓ %s %s\n' "$name" "$detail"
    pass=$((pass + 1))
  else
    printf '  ✗ %s %s\n' "$name" "$detail" >&2
    fail=$((fail + 1))
  fi
}
json_text() { python3 -c 'import json,sys; print(json.load(sys.stdin).get("text",""))'; }

echo "== services =="
health="$(curl -fsS -m 5 "$ASR_BASE/health" 2>/dev/null || true)"
check "ASR /health" "$([ -n "$health" ] && echo 1 || echo 0)" "${health:0:100}"
models="$(curl -fsS -m 5 "$ASR_BASE/v1/models" 2>/dev/null || true)"
check "ASR /v1/models" "$(echo "$models" | grep -q 'sensevoice-small' && echo 1 || echo 0)"
check "gateway /healthz" "$(curl -fsS -m 5 "${GATEWAY_BASE%/v1}/healthz" >/dev/null 2>&1 && echo 1 || echo 0)"
check "Nexus /api/health" "$(curl -fsS -m 5 "$NEXUS_BASE/api/health" >/dev/null 2>&1 && echo 1 || echo 0)"
check "Nexus QA login" "$([ "$auth_status" = "201" ] || [ "$auth_status" = "200" ] && echo 1 || echo 0)" "HTTP $auth_status"

echo "== direct ASR =="
direct="$(curl -fsS -m 60 -X POST -F "file=@$SAMPLE" -F "model=sensevoice" "$ASR_BASE/v1/audio/transcriptions" 2>/dev/null || true)"
check "direct text" "$(echo "$direct" | grep -q "$EXPECT" && echo 1 || echo 0)" "$(echo "$direct" | head -c 120)"

echo "== gateway ASR =="
gateway="$(curl -fsS -m 60 -X POST -H "Authorization: Bearer $NEXUS_GATEWAY_API_KEY" \
  -F "file=@$SAMPLE" -F "model=sensevoice-small" "$GATEWAY_BASE/audio/transcriptions" 2>/dev/null || true)"
check "gateway text" "$(echo "$gateway" | grep -q "$EXPECT" && echo 1 || echo 0)" "$(echo "$gateway" | head -c 120)"

echo "== Nexus speech route =="
speech="$(curl -fsS -m 60 -b "$COOKIE_JAR" -X POST \
  -F "file=@$SAMPLE" "$NEXUS_BASE/api/asr/transcribe" 2>/dev/null || true)"
check "Nexus speech text" "$(echo "$speech" | grep -q "$EXPECT" && echo 1 || echo 0)" "$(echo "$speech" | head -c 120)"
config="$(curl -fsS -m 5 -b "$COOKIE_JAR" "$NEXUS_BASE/api/gateway/speech-input" 2>/dev/null || true)"
route_id="$(echo "$config" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("route_id") or "")' 2>/dev/null || true)"
routes="$(curl -fsS -m 5 -b "$COOKIE_JAR" "$NEXUS_BASE/api/gateway/routes" 2>/dev/null || true)"
route_ok="$(echo "$routes" | ROUTE_ID="$route_id" python3 -c 'import json,os,sys; routes=json.load(sys.stdin); print(1 if any(r["id"]==os.environ["ROUTE_ID"] and r["model_type"]=="ASR" and r["enabled"] for r in routes) else 0)' 2>/dev/null || echo 0)"
check "speech-input points to an enabled ASR route" "$route_ok" "route_id=$route_id"

echo "== deployment invariants =="
for file in model.pt config.yaml tokens.json am.mvn chn_jpn_yue_eng_ko_spectok.bpe.model; do
  check "model file $file" "$([ -f "$ROOT/models/SenseVoiceSmall/$file" ] && echo 1 || echo 0)"
done
check "Nginx has no direct ASR upstream" "$(grep -Eq 'proxy_pass[[:space:]]+http://127.0.0.1:19899' "$REPO/ops/deploy/nginx-nexus.conf" && echo 0 || echo 1)"

echo
echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
