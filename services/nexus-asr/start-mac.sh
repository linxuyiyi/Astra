#!/usr/bin/env bash
# Nexus ASR Server — macOS local start (background service manager).
set -euo pipefail
cd "$(dirname "$0")"
exec ./nexus-asr.sh start
