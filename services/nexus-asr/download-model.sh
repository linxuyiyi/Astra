#!/usr/bin/env bash
# Download and verify the official SenseVoice-Small INT8 model.
set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="python3"
fi

MODEL_DIR="${NEXUS_ASR_MODEL_DIR:-models/sensevoice-small-int8}"
exec "$PY" -m app.model_store --download --model-dir "$MODEL_DIR" "$@"
