#!/usr/bin/env bash
# Download and verify the configured SenseVoice model (FunASR by default).
set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="python3"
fi

PROVIDER="${NEXUS_ASR_PROVIDER:-funasr}"
ARGS=(--download --provider "$PROVIDER")
if [ -n "${NEXUS_ASR_MODEL_DIR:-}" ]; then
  ARGS+=(--model-dir "$NEXUS_ASR_MODEL_DIR")
fi
if [ -n "${NEXUS_ASR_CACHE_DIR:-}" ]; then
  ARGS+=(--cache-dir "$NEXUS_ASR_CACHE_DIR")
fi
exec "$PY" -m app.model_store "${ARGS[@]}" "$@"
