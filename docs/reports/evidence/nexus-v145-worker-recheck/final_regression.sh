#!/usr/bin/env bash
# Final Linux regression runner for Nexus v1.4.5 Worker maintenance.
#
# Intended for a future round after the source freeze is confirmed. It refreshes
# only /opt/nexus-v145-regression/src, reuses the existing venv, runs the same
# 21 files as the final5 acceptance, then runs ruff. It never starts, restarts
# or deploys a product service and never touches the business database.
#
# Redis: leave ASTRA_TEST_REDIS_URL unset unless the existing harness-owned
# Redis DB14 identity is confirmed. The script prints that decision.
set -uo pipefail

REPO_ROOT="${REPO_ROOT:-/Users/niuniu/程序/Astra-worktrees/nexus-v1.4.5-workers}"
QA_ROOT="${QA_ROOT:-/tmp/nexus-v145-qa}"
VM="${VM:-nexus-v145-qa}"
SRC=/opt/nexus-v145-regression/src
VENV=/opt/nexus-v145-regression/venv
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${OUT_DIR:-$REPO_ROOT/docs/reports/evidence/nexus-v145-worker-recheck}"
PREP_LOG="$QA_ROOT/logs/recheck-prepare-$STAMP.log"
PYTEST_LOG="$QA_ROOT/logs/recheck-pytest-$STAMP.log"
RUFF_LOG="$QA_ROOT/logs/recheck-ruff-$STAMP.log"
SUMMARY="$OUT_DIR/final-regression-$STAMP.json"

FILES=(
  tests/test_worker_maintenance.py
  tests/test_worker_settings_v145.py
  tests/test_worker_equipment_v145.py
  tests/test_worker_runtime_inspection_v145.py
  tests/test_worker_local_maintenance_v145.py
  tests/test_remote_install_v145.py
  tests/test_remote_provisioning.py
  tests/test_phase4_channel.py
  tests/test_worker_channel_review_fixes.py
  tests/test_worker_gateway.py
  tests/test_channel_switch.py
  tests/test_worker_occupancy.py
  tests/test_task_responsibility.py
  tests/test_task_routing.py
  tests/test_session_workspace.py
  tests/test_proxy_secret_redaction.py
  tests/test_api.py
  tests/test_mcp_transport_security.py
  tests/test_worker_git_auth_v145.py
  tests/test_repositories.py
  tests/test_task_mcp_auth.py
)
FILES_STR="$(printf '%s ' "${FILES[@]}")"

mkdir -p "$QA_ROOT/logs" "$OUT_DIR"

echo "phase=prepare_snapshot"
"$REPO_ROOT/services/task-platform/.venv/bin/python" \
  "$QA_ROOT/harness/prepare_regression.py" --reuse-venv --skip-install \
  2>&1 | tee "$PREP_LOG"
PREP_RC=${PIPESTATUS[0]}
echo "prepare_exit_code=$PREP_RC"
if [[ "$PREP_RC" -ne 0 ]]; then
  printf '{"status":"failed","phase":"prepare","exit_code":%d,"log":"%s"}\n' \
    "$PREP_RC" "$PREP_LOG" > "$SUMMARY"
  exit "$PREP_RC"
fi

REDIS_DECISION="skipped_unconfirmed_identity"
REDIS_EXPORT=""
if [[ -n "${ASTRA_TEST_REDIS_URL:-}" ]]; then
  REDIS_DECISION="provided_by_operator"
  REDIS_EXPORT="export ASTRA_TEST_REDIS_URL=$(printf '%q' "$ASTRA_TEST_REDIS_URL")"
fi
echo "redis=$REDIS_DECISION"

REMOTE_PYTEST=$(cat <<EOF
set -o pipefail
cd $SRC
export ASTRA_DATA_DIR=/tmp/nexus-v145-qa-guest/data
export ASTRA_LOG_DIR=/tmp/nexus-v145-qa-guest/logs
export ASTRA_RUNTIME_DIR=/tmp/nexus-v145-qa-guest/runtime
export ASTRA_CONFIG_DIR=/tmp/nexus-v145-qa-guest/config
export PYTHONDONTWRITEBYTECODE=1
$REDIS_EXPORT
$VENV/bin/python -m pytest -ra $FILES_STR
EOF
)
echo "phase=pytest"
limactl shell --tty=false "$VM" -- sudo -n bash -c "$REMOTE_PYTEST" 2>&1 | tee "$PYTEST_LOG"
PYTEST_RC=${PIPESTATUS[0]}
echo "pytest_exit_code=$PYTEST_RC"

REMOTE_RUFF="cd $SRC && $VENV/bin/ruff check --no-cache app tests"
echo "phase=ruff"
limactl shell --tty=false "$VM" -- sudo -n bash -c "$REMOTE_RUFF" 2>&1 | tee "$RUFF_LOG"
RUFF_RC=${PIPESTATUS[0]}
echo "ruff_exit_code=$RUFF_RC"

HASH="$(git -C "$REPO_ROOT" rev-parse HEAD)"
printf '{"status":"%s","commit":"%s","prepare_exit_code":%d,"pytest_exit_code":%d,"ruff_exit_code":%d,"redis":"%s","pytest_log":"%s","ruff_log":"%s","prepare_log":"%s"}\n' \
  "$([[ "$PYTEST_RC" -eq 0 && "$RUFF_RC" -eq 0 ]] && echo passed || echo failed)" \
  "$HASH" "$PREP_RC" "$PYTEST_RC" "$RUFF_RC" "$REDIS_DECISION" \
  "$PYTEST_LOG" "$RUFF_LOG" "$PREP_LOG" > "$SUMMARY"
echo "summary=$SUMMARY"

if [[ "$PYTEST_RC" -ne 0 ]]; then exit "$PYTEST_RC"; fi
exit "$RUFF_RC"
