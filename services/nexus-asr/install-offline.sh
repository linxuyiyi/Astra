#!/usr/bin/env bash
# Offline install for Linux ARM64. Installs into the current directory only.
# Required layout:
#   app/ requirements.txt config.yaml models/... wheels/ start-linux.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "== target check =="
ARCH="$(uname -m)"
if [ "$ARCH" != "aarch64" ] && [ "$ARCH" != "arm64" ]; then
  echo "This package is built for ARM64; current arch is $ARCH." >&2
  exit 1
fi

PYTHON="${PYTHON:-python3}"
"$PYTHON" - <<'PY'
import ctypes, platform, sys
if sys.version_info[:2] != (3, 12):
    raise SystemExit(f"Python 3.12 is required (found {sys.version.split()[0]}); "
                     "the bundled wheels are cp312.")
try:
    libc = ctypes.CDLL("libc.so.6")
    version = libc.gnu_get_libc_version().decode()
    print("glibc", version)
    if tuple(int(x) for x in version.split(".")[:2]) < (2, 28):
        raise SystemExit("glibc >= 2.28 is required by the PyAV wheel (found %s)" % version)
except OSError as exc:
    raise SystemExit(f"cannot detect glibc: {exc}")
print("Python", platform.python_version(), "arch", platform.machine())
PY

if [ ! -d wheels ] || [ -z "$(ls -A wheels 2>/dev/null)" ]; then
  echo "wheels/ is empty. Prepare it on macOS with scripts/prepare-linux-wheels.sh." >&2
  exit 1
fi

echo "== virtualenv =="
if [ ! -x .venv/bin/python ]; then
  "$PYTHON" -m venv .venv
fi
.venv/bin/python -m pip install --no-index --find-links wheels --only-binary=:all: -r requirements.txt

echo "== model check =="
.venv/bin/python -m app.model_store --check --provider "${NEXUS_ASR_PROVIDER:-funasr}"

echo
echo "Install OK. Next:"
echo "  1. cp .env.example .env  (optional; edit host/port/offline)"
echo "  2. ./start-linux.sh"
echo "  3. install deploy/nexus-asr.service and register the ASR node in Nexus"
echo "     (see docs/deployment-linux-arm64.md; do not expose 19899 publicly)"
