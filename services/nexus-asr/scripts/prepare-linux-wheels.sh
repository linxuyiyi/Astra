#!/usr/bin/env bash
# Run on macOS (or any machine with network) to collect Linux ARM64 wheels.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  echo ".venv missing; run ./setup-mac.sh first" >&2
  exit 1
fi

rm -rf wheels
mkdir -p wheels
.venv/bin/python -m pip download \
  -r requirements.txt \
  -d wheels \
  --platform manylinux_2_28_aarch64 \
  --platform manylinux2014_aarch64 \
  --python-version 312 \
  --implementation cp \
  --abi cp312 \
  --only-binary=:all:

echo "Verifying that the wheel set is self-contained..."
.venv/bin/python -m pip install --dry-run --no-index --find-links=wheels \
  -r requirements.txt \
  --platform manylinux_2_28_aarch64 \
  --platform manylinux2014_aarch64 \
  --python-version 312 \
  --implementation cp \
  --abi cp312 \
  --only-binary=:all: \
  --target /tmp/nexus-asr-wheel-check >/dev/null

rm -rf /tmp/nexus-asr-wheel-check
echo "wheels: $(ls wheels | wc -l | tr -d ' ') files, $(du -sh wheels | cut -f1)"
echo "Package wheels/ together with models/ and the source tree for offline deployment."
