#!/usr/bin/env bash
# 唯一平台部署入口：首次安装和升级使用相同命令。
set -Eeuo pipefail
umask 077
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# bootstrap 仅准备运行控制程序所需的系统 Python；包源沿用管理员仓库。
if ! command -v python3 >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1; then
  [ "$(id -u)" = 0 ] || { echo '缺少 python3 或 git；自动安装需要 root' >&2; exit 1; }
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update && apt-get install -y python3 git
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 git
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 git
  else
    echo '缺少 python3 且未找到受支持系统包管理器' >&2; exit 1
  fi
fi
python3 -c 'import sys;sys.exit(0 if sys.version_info >= (3,8) else "部署引导需要 Python 3.8+，请使用受支持的 Linux 系统仓库")'
exec python3 "$DEPLOY_DIR/deploy.py" "$@"
