#!/usr/bin/env bash
# Nexus 生产部署唯一 CLI 的薄封装。
# 用法：ops/deploy/install_nexus.sh [install|upgrade|restart|status|health|rollback] [选项]
# 环境变量、仓库与完整 commit 由 /etc/nexus/nexus.env 或 --env-file 提供。
# 详细参数：ops/deploy/install_nexus.sh --help
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${NEXUS_PYTHON:-python3}"
export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON" -m nexus_deploy "$@"
