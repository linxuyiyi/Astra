#!/usr/bin/env bash
# Nexus v1.4.8 fresh-database installer. Idempotent; safe to re-run.
# shellcheck disable=SC1090
# Usage: ops/deploy/install_nexus.sh [--env-file F] [--preflight-only] [--skip-web-build]
#        [--with-llm-wiki] [--remote-worker HOST] [--no-systemd] [--yes]
# Secrets come from the environment or ops/env/.env and are never echoed.
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE=""; PREFLIGHT_ONLY=0; SKIP_WEB=0; WITH_WIKI=0; REMOTE_WORKER=""; USE_SYSTEMD=auto
MIN_MEM_MB=3800; HARD_MIN_MEM_MB=1900; MIN_DISK_GB=10; MIN_CPUS=2
OPENCODE_VERSION=1.18.35

log()  { printf '\033[1;34m[nexus]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }
trap 'die "第 $LINENO 行命令失败：$BASH_COMMAND"' ERR

usage() { sed -n 2,5p "$0"; exit 0; }
while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) ENV_FILE="$2"; shift 2;;
    --preflight-only) PREFLIGHT_ONLY=1; shift;;
    --skip-web-build) SKIP_WEB=1; shift;;
    --with-llm-wiki) WITH_WIKI=1; shift;;
    --remote-worker) REMOTE_WORKER="$2"; shift 2;;
    --no-systemd) USE_SYSTEMD=no; shift;;
    -h|--help) usage;;
    *) die "未知参数：$1（--help 查看用法）";;
  esac
done

# ---------- config (env wins over file; values never printed) ----------
load_env_file() {
  local f="$1" line key value
  [ -f "$f" ] || return 0
  [ "$(stat -c %a "$f")" -le 600 ] || warn "$f 权限过宽，建议 chmod 600"
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%"${line##*[![:space:]]}"}"
    [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    # 同时支持 shell/dotenv 的外层引号，不执行配置中的命令或变量替换。
    case "$value" in
      \"*\") value="${value:1:${#value}-2}" ;;
      \'*\') value="${value:1:${#value}-2}" ;;
      *) value="${value%%[[:space:]]#*}" ;;
    esac
    [ -n "${!key:-}" ] || export "$key=$value"
  done < "$f"
}
[ -n "$ENV_FILE" ] && { [ -f "$ENV_FILE" ] || die "找不到 --env-file $ENV_FILE"; load_env_file "$ENV_FILE"; }
load_env_file "$SCRIPT_DIR/../env/.env"

: "${NEXUS_REPO_URL:=https://github.com/linxuyiyi/Astra}"
: "${NEXUS_BRANCH:=v1.4.8}"
: "${NEXUS_ROOT:=/opt/nexus}"
: "${NEXUS_STATE_DIR:=/var/lib/nexus}"
: "${NEXUS_BIND_HOST:=127.0.0.1}"
: "${ASTRA_EXECUTOR_TYPE:=worker}"
: "${NEXUS_GATEWAY_OPENAI_BASE:=http://127.0.0.1:${ASTRA_SERVICE_GATEWAY_PORT:-18110}/v1}"
: "${REMOTE_WORKER_ID:=p2-remote}"; : "${REMOTE_WORKER_USER:=root}"; : "${REMOTE_WORKER_PORT:=22}"
: "${REMOTE_WORKER_OPENCODE_PORT:=14096}"; : "${REMOTE_WORKER_WORKSPACE:=/root/nexus-remote}"
PORTS=(6379 18000 "${ASTRA_SERVICE_GATEWAY_PORT:-18110}" 18120 18201 18202 18203)
[ "$ASTRA_EXECUTOR_TYPE" != opencode_server ] || PORTS+=(14096)

ver_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }
oc_ver() { printf '%s' "$1" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1; }
have() { command -v "$1" >/dev/null 2>&1; }
SUDO=""; [ "$(id -u)" = 0 ] || { have sudo && SUDO="sudo"; }

# ---------- preflight ----------
preflight() {
  local errs=0 x
  bad() { printf '  \033[31m✗\033[0m %s\n' "$*"; errs=$((errs+1)); }
  ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
  note(){ printf '  \033[33m!\033[0m %s\n' "$*"; }
  log "预检"
  [ -n "${ASTRA_DATABASE_URL:-}" ] && ok "Nexus SQL 地址已配置" || bad "缺少 ASTRA_DATABASE_URL（公司 MySQL 8.0.36 的空库）"
  [ -n "${AGENTSCOPE_SQL_URL:-}" ] && ok "AgentScope SQL 地址已配置" || bad "缺少独立数据库 AGENTSCOPE_SQL_URL"
  [ "$(uname -s)" = Linux ] && ok "OS Linux $(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-}")" || bad "仅支持 Linux"
  x="$(uname -m)"; case "$x" in x86_64|aarch64) ok "arch $x";; *) bad "不支持的架构 $x";; esac
  x="$(ldd --version 2>&1 | awk 'NR==1{print $NF}')"; [[ "$x" =~ ^[0-9]+\.[0-9]+$ ]] || x=0
  ver_ge "$x" 2.28 && ok "glibc $x" || bad "glibc $x < 2.28（CentOS 7 不行；请用容器或新系统）"
  x="$(nproc)"; [ "$x" -ge "$MIN_CPUS" ] && ok "CPU $x 核" || bad "CPU $x 核 < $MIN_CPUS"
  x="$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)"
  if [ "$x" -lt "$HARD_MIN_MEM_MB" ]; then bad "内存 ${x}MB 过小（1.7GB 主机实测会卡死）"
  elif [ "$x" -lt "$MIN_MEM_MB" ]; then note "内存 ${x}MB < 建议 4GB"; else ok "内存 ${x}MB"; fi
  local pdir="$NEXUS_ROOT"; while [ ! -d "$pdir" ]; do pdir="$(dirname "$pdir")"; done
  x="$(df -Pk "$pdir" | awk 'NR==2{print int($4/1048576)}')"
  [ "$x" -ge "$MIN_DISK_GB" ] && ok "磁盘可用 ${x}GB ($pdir)" || bad "磁盘可用 ${x}GB < ${MIN_DISK_GB}GB"
  for x in git curl python3 node npm; do have "$x" && ok "$x 已安装" || bad "缺少 $x"; done
  if have python3; then x="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
    ver_ge "$x" 3.11 && ok "Python $x" || bad "Python $x < 3.11"; fi
  if have node; then x="$(node -p 'process.versions.node')"; ver_ge "$x" 20 && ok "Node $x" || bad "Node $x < 20"; fi
  if have redis-server || have docker; then ok "Redis 来源：$(have redis-server && echo redis-server || echo docker)"; else bad "需要 redis-server 或 docker"; fi
  if [ "$ASTRA_EXECUTOR_TYPE" = opencode_server ]; then
    x="$( (opencode --version || "$HOME/.local/bin/opencode" --version) 2>/dev/null | tail -1 || true)"
    if [ "$(oc_ver "$x")" = "$OPENCODE_VERSION" ]; then ok "OpenCode $OPENCODE_VERSION"; else note "本机执行模式将安装 OpenCode $OPENCODE_VERSION"; fi
  elif [ "$ASTRA_EXECUTOR_TYPE" = worker ]; then
    ok "远程 Worker 模式，不检查控制机 OpenCode"
  else
    bad "安装器 ASTRA_EXECUTOR_TYPE 只支持 worker 或 opencode_server"
  fi
  for x in "${PORTS[@]}"; do
    if ss -ltnH "( sport = :$x )" 2>/dev/null | grep -q .; then
      if systemctl is-active --quiet "nexus-*" 2>/dev/null || [ -f "$NEXUS_STATE_DIR/installed" ]; then note "端口 $x 已占用（视为已部署的 Nexus 服务）"; else bad "端口 $x 已被占用"; fi
    else ok "端口 $x 空闲"; fi
  done
  if [ "$WITH_WIKI" = 1 ]; then
    [ -n "${ASTRA_LLM_WIKI_API_TOKEN:-}" ] || bad "已有 llm_wiki 需要 ASTRA_LLM_WIKI_API_TOKEN"
    [ -n "${ASTRA_LLM_WIKI_PROJECT_ID:-}" ] || bad "已有 llm_wiki 需要 ASTRA_LLM_WIKI_PROJECT_ID"
    [ -n "${ASTRA_LLM_WIKI_API_BASE_URL:-}" ] && ok "llm_wiki 地址已配置" || bad "--with-llm-wiki 需要 ASTRA_LLM_WIKI_API_BASE_URL（例如 http://10.0.0.8:19828/api/v1）"
  fi
  if [ -n "$REMOTE_WORKER" ]; then
    [ -n "${NEXUS_DEFAULT_MODEL:-}" ] || bad "--remote-worker 需要已配置的网关模型别名 NEXUS_DEFAULT_MODEL"
    [ -n "${REMOTE_WORKER_SSH_PASSWORD:-}" ] && ok "远程 Worker 密码已提供" || bad "--remote-worker 需要 REMOTE_WORKER_SSH_PASSWORD"
    timeout 10 bash -c "exec 3<>/dev/tcp/$REMOTE_WORKER/$REMOTE_WORKER_PORT" 2>/dev/null && ok "$REMOTE_WORKER:$REMOTE_WORKER_PORT 可达" || bad "$REMOTE_WORKER:$REMOTE_WORKER_PORT 不可达"
  fi
  [ "$errs" -eq 0 ] || die "预检失败 $errs 项，已停止（未做任何修改）"
  log "预检通过"
}

# ---------- helpers ----------
upsert_kv() { # file key value  (value never printed)
  local f="$1" k="$2" v="$3" tmp; touch "$f"; chmod 600 "$f"; tmp="$(mktemp)"
  grep -v "^$k=" "$f" > "$tmp" || true; printf '%s=%q\n' "$k" "$v" >> "$tmp"; mv "$tmp" "$f"; chmod 600 "$f"
}
get_kv() { [ -f "$1" ] && grep "^$2=" "$1" | tail -1 | cut -d= -f2- || true; }
wait_http() { # url name timeout_seconds
  local py="$NEXUS_ROOT/services/nexus-api/.venv/bin/python"
  if "$py" "$NEXUS_ROOT/ops/dev/service_ctl.py" wait-http "$1" --timeout "${3:-60}"; then
    log "$2 健康"
  else
    die "$2 未就绪：$1"
  fi
}

SECRETS="$NEXUS_STATE_DIR/secrets.env"; RUNENV="$NEXUS_STATE_DIR/nexus.env"; LOGS="$NEXUS_STATE_DIR/logs"

step_checkout() {
  log "代码：$NEXUS_REPO_URL @ $NEXUS_BRANCH -> $NEXUS_ROOT"
  $SUDO mkdir -p "$NEXUS_ROOT" "$NEXUS_STATE_DIR" "$LOGS"; $SUDO chown "$(id -u):$(id -g)" "$NEXUS_ROOT" "$NEXUS_STATE_DIR" "$LOGS"; chmod 700 "$NEXUS_STATE_DIR"
  if [ -d "$NEXUS_ROOT/.git" ]; then
    git -C "$NEXUS_ROOT" fetch -q origin "$NEXUS_BRANCH"
    git -C "$NEXUS_ROOT" diff --quiet || warn "工作区有本地修改，仅尝试快进"
    git -C "$NEXUS_ROOT" checkout -q "$NEXUS_BRANCH" 2>/dev/null || git -C "$NEXUS_ROOT" checkout -q -b "$NEXUS_BRANCH" "origin/$NEXUS_BRANCH"
    git -C "$NEXUS_ROOT" merge -q --ff-only "origin/$NEXUS_BRANCH" || warn "无法快进，保留当前提交"
  else
    git clone -q -b "$NEXUS_BRANCH" "$NEXUS_REPO_URL" "$NEXUS_ROOT.tmp.$$"
    rmdir "$NEXUS_ROOT" 2>/dev/null || true; mv "$NEXUS_ROOT.tmp.$$" "$NEXUS_ROOT"
  fi
}

step_deps() {
  log "Redis"
  if ! (have redis-cli && redis-cli -p 6379 ping >/dev/null 2>&1); then
    if have redis-server; then
      if have systemctl && systemctl list-unit-files 2>/dev/null | grep -q '^redis'; then $SUDO systemctl enable --now redis-server 2>/dev/null || $SUDO systemctl enable --now redis
      else redis-server --bind 127.0.0.1 --port 6379 --daemonize yes >/dev/null; fi
    else docker compose -f "$NEXUS_ROOT/services/agent-service/docker-compose.yml" up -d; fi
  fi
  if [ "${ASTRA_EXECUTOR_TYPE:-worker}" = "opencode_server" ]; then
    log "OpenCode CLI（本机执行模式）"
    local ocv; ocv="$( (opencode --version || "$HOME/.local/bin/opencode" --version) 2>/dev/null | tail -1 || true)"
    if [ "$(oc_ver "$ocv")" != "$OPENCODE_VERSION" ]; then npm i -g --prefix "$HOME/.local" "opencode-ai@$OPENCODE_VERSION" >/dev/null; fi
    export PATH="$HOME/.local/bin:$PATH"
  else
    log "跳过本机 OpenCode（ASTRA_EXECUTOR_TYPE=${ASTRA_EXECUTOR_TYPE:-worker}，只使用远程 Worker）"
  fi
  log "Python 虚拟环境（nexus api / agent service）"
  local py=python3
  [ -x "$NEXUS_ROOT/services/nexus-api/.venv/bin/python" ] || $py -m venv "$NEXUS_ROOT/services/nexus-api/.venv"
  "$NEXUS_ROOT/services/nexus-api/.venv/bin/pip" install -q -U pip
  "$NEXUS_ROOT/services/nexus-api/.venv/bin/pip" install -q -e "$NEXUS_ROOT/services/nexus-api[dev]"
  [ -x "$NEXUS_ROOT/services/agent-service/.venv-agentscope/bin/python" ] || $py -m venv "$NEXUS_ROOT/services/agent-service/.venv-agentscope"
  "$NEXUS_ROOT/services/agent-service/.venv-agentscope/bin/pip" install -q -r "$NEXUS_ROOT/services/agent-service/requirements.txt"
  if [ "$SKIP_WEB" = 0 ]; then
    # 聊天岛产物落在 services/nexus-api/web/public/nexus-chat/，必须先于 web 构建，
    # 否则 Vite 拷 public 时拿不到它。
    log "构建 chat-island（聊天岛）"
    (cd "$NEXUS_ROOT/services/nexus-api/chat-island" && npm ci --no-audit --no-fund >/dev/null && npm run build >/dev/null)
    log "构建 Nexus Web"
    (cd "$NEXUS_ROOT/services/nexus-api/web" && npm ci --no-audit --no-fund >/dev/null && npm run build >/dev/null)
  else log "跳过 Web 构建（--skip-web-build）"; fi
}

step_secrets() {
  log "密钥（不存在才生成，保存在 ${SECRETS}，0600）"
  local v
  # shell-quote URL query parameters and encoded credentials for later sourcing.
  v="$ASTRA_DATABASE_URL"
  upsert_kv "$SECRETS" ASTRA_DATABASE_URL "$v"
  v="$AGENTSCOPE_SQL_URL"
  upsert_kv "$SECRETS" AGENTSCOPE_SQL_URL "$v"
  v="${ASTRA_GATEWAY_MASTER_KEY:-$(get_kv "$SECRETS" ASTRA_GATEWAY_MASTER_KEY)}"
  [ -n "$v" ] || v="$("$NEXUS_ROOT/services/nexus-api/.venv/bin/python" -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"
  upsert_kv "$SECRETS" ASTRA_GATEWAY_MASTER_KEY "$v"
  v="${NEXUS_MCP_API_KEY:-$(get_kv "$SECRETS" NEXUS_MCP_API_KEY)}"
  [ -n "$v" ] || v="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
  upsert_kv "$SECRETS" NEXUS_MCP_API_KEY "$v"
  v="${NEXUS_WORKER_GATEWAY_ADMIN_KEY:-$(get_kv "$SECRETS" NEXUS_WORKER_GATEWAY_ADMIN_KEY)}"
  [ -n "$v" ] || v="$(python3 -c 'import secrets;print("wgk_"+secrets.token_urlsafe(32))')"
  upsert_kv "$SECRETS" NEXUS_WORKER_GATEWAY_ADMIN_KEY "$v"
  v="${NEXUS_SCHEDULE_MCP_KEY:-$(get_kv "$SECRETS" NEXUS_SCHEDULE_MCP_KEY)}"
  [ -n "$v" ] || v="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
  upsert_kv "$SECRETS" NEXUS_SCHEDULE_MCP_KEY "$v"
  v="${NEXUS_INTERNAL_KEY:-$(get_kv "$SECRETS" NEXUS_INTERNAL_KEY)}"
  [ -n "$v" ] || v="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
  upsert_kv "$SECRETS" NEXUS_INTERNAL_KEY "$v"
  v="${NEXUS_GATEWAY_API_KEY:-$(get_kv "$SECRETS" NEXUS_GATEWAY_API_KEY)}"
  [ -n "$v" ] || v="$(python3 -c 'import secrets;print("agw_"+secrets.token_urlsafe(32))')"
  [[ "$v" =~ ^agw_[A-Za-z0-9_-]+$ ]] || die "NEXUS_GATEWAY_API_KEY 需要网关签发的 agw_ 密钥"
  upsert_kv "$SECRETS" NEXUS_GATEWAY_API_KEY "$v"
  if [ -n "${NEXUS_EXPERIENCE_REVIEW_GATEWAY_KEY:-}" ]; then
    upsert_kv "$SECRETS" NEXUS_EXPERIENCE_REVIEW_GATEWAY_KEY "$NEXUS_EXPERIENCE_REVIEW_GATEWAY_KEY"
  fi
  if [ "$WITH_WIKI" = 1 ]; then
    [ -n "${ASTRA_LLM_WIKI_API_TOKEN:-}" ] || die "缺少已有 llm_wiki token"
    [ -n "${ASTRA_LLM_WIKI_PROJECT_ID:-}" ] || die "缺少已有 llm_wiki project_id"
    v="$ASTRA_LLM_WIKI_API_TOKEN"
    upsert_kv "$SECRETS" ASTRA_LLM_WIKI_API_TOKEN "$v"
    v="$ASTRA_LLM_WIKI_PROJECT_ID"
    upsert_kv "$SECRETS" ASTRA_LLM_WIKI_PROJECT_ID "$v"
  fi
}

step_runenv() {
  log "运行时环境 $RUNENV"
  local R="$NEXUS_ROOT" gateway="${NEXUS_GATEWAY_OPENAI_BASE%/}"
  [[ "$gateway" == */v1 ]] || gateway="$gateway/v1"
  { cat <<ENV
PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
ASTRA_OPENCODE_MODEL=nexus-gateway/${NEXUS_DEFAULT_MODEL:-default}
ASTRA_GATEWAY_PROBE_TIMEOUT_SECONDS=30
NEXUS_MCP_TRANSPORT=streamable-http
NEXUS_MCP_HOST=127.0.0.1
NEXUS_MCP_PORT=18201
NEXUS_TASK_MCP_PORT=18202
NEXUS_SCHEDULE_MCP_PORT=18203
ASTRA_REDIS_URL=redis://127.0.0.1:6379/1
ASTRA_OUTBOX_INTERVAL=2
ASTRA_OUTBOX_RETRY_SECONDS=5
ASTRA_NEXUS_API_BASE=http://127.0.0.1:18120
ASTRA_AGENTSCOPE_BASE_URL=http://127.0.0.1:18000
ASTRA_AGENTSCOPE_USER_ID=nexus
NEXUS_API_BASE=http://127.0.0.1:18120
AGENTSCOPE_HOST=$NEXUS_BIND_HOST
AGENTSCOPE_PORT=18000
AGENTSCOPE_REDIS_HOST=127.0.0.1
AGENTSCOPE_REDIS_PORT=6379
AGENTSCOPE_REDIS_DB=1
AGENTSCOPE_WORKSPACE_DIR=$R/services/agent-service/data/workspaces
NEXUS_DATA_DIR=$R/services/nexus-api/data
NEXUS_AGENTSCOPE_BASE_URL=http://127.0.0.1:18000
NEXUS_AGENTSCOPE_USER_ID=nexus
NEXUS_TASK_MCP_URL=http://127.0.0.1:18202/mcp
NEXUS_SCHEDULE_MCP_URL=http://127.0.0.1:18203/mcp
NEXUS_CHAT_PROVIDER=openai
# 经验复盘默认关闭；开启后人工确认完成会创建复盘 Job。
NEXUS_EXPERIENCE_REVIEW_ENABLED=false
NEXUS_EXPERIENCE_REVIEW_GATEWAY_URL=$gateway/chat/completions
NEXUS_EXPERIENCE_REVIEW_MODEL=${NEXUS_DEFAULT_MODEL:-default}
NEXUS_EXPERIENCE_REVIEW_SCAN_INTERVAL=30
NEXUS_EXPERIENCE_REVIEW_MAX_CONCURRENCY=2
ENV
  if [ "$WITH_WIKI" = 1 ]; then cat <<ENV
ASTRA_LLM_WIKI_ENABLED=true
ASTRA_LLM_WIKI_API_BASE_URL=$ASTRA_LLM_WIKI_API_BASE_URL
ASTRA_LLM_WIKI_TIMEOUT=30
ENV
  fi; } > "$RUNENV.new"
  mv "$RUNENV.new" "$RUNENV"; chmod 600 "$RUNENV"
  # systemd 和手动重启经 shell 读取同一份受保护环境。
  local key v
  for key in ASTRA_EXECUTOR_TYPE ASTRA_OPENCODE_MODEL NEXUS_GATEWAY_OPENAI_BASE NEXUS_DEFAULT_MODEL \
    NEXUS_REMOTE_GATEWAY_LOCAL_URL NEXUS_MCP_RESOURCE_SERVER_URL NEXUS_WORKER_AGENT_GATEWAY_URL \
    NEXUS_REMOTE_DOWNLOAD_BASE_URL NEXUS_REMOTE_NPM_REGISTRY NEXUS_REMOTE_PIP_INDEX_URL \
    NEXUS_REMOTE_PYTHON NEXUS_REMOTE_NODE_VERSION NEXUS_REMOTE_OPENCODE_VERSION \
    NEXUS_REMOTE_DEPLOY_TIMEOUT_SECONDS \
    NEXUS_KB_EMBEDDING_JSON ASTRA_DATABASE_BACKEND ASTRA_DATA_DIR ASTRA_CONFIG_DIR \
    ASTRA_WORKSPACE_ROOT ASTRA_LOG_DIR ASTRA_RUNTIME_DIR ASTRA_SEED_DEMO \
    ASTRA_REDIS_URL ASTRA_GATEWAY_ENABLED \
    ASTRA_LLM_WIKI_API_BASE_URL ASTRA_LLM_WIKI_TIMEOUT \
    ASTRA_SERVICE_GATEWAY_HOST ASTRA_SERVICE_GATEWAY_PORT \
    NEXUS_EXPERIENCE_REVIEW_ENABLED NEXUS_EXPERIENCE_REVIEW_MODEL \
    NEXUS_EXPERIENCE_REVIEW_GATEWAY_URL; do
    if [ "${!key+x}" = x ]; then
      v="${!key}"
      upsert_kv "$RUNENV" "$key" "$v"
    fi
  done
  v="$RUNENV"
  upsert_kv "$RUNENV" NEXUS_RUNTIME_ENV_FILE "$v"

}

step_migrate() {
  log "Nexus 全新数据库初始化（0001_init）"
  (set -a; . "$RUNENV"; . "$SECRETS"; set +a; cd "$NEXUS_ROOT/services/nexus-api" && .venv/bin/python -m alembic upgrade head >/dev/null)
  log "AgentScope 官方 SQL 初始化（独立数据库）"
  (set -a; . "$RUNENV"; . "$SECRETS"; set +a; cd "$NEXUS_ROOT/services/agent-service" && .venv-agentscope/bin/python agentscope_sql.py init)
  (set -a; . "$RUNENV"; . "$SECRETS"; set +a; cd "$NEXUS_ROOT/services/nexus-api" && .venv/bin/python -m app.deployment gateway-key)
}

# ---------- services ----------
SYSTEMD=0
detect_systemd() { [ "$USE_SYSTEMD" != no ] && have systemctl && [ -d /run/systemd/system ] && SYSTEMD=1 || true; }

svc_unit() { # name type exec [stop]
  local name="$1" type="$2" exec="$3" stop="${4:-}" f="/etc/systemd/system/$1.service"
  $SUDO tee "$f" >/dev/null <<UNIT
[Unit]
Description=Nexus $name
After=network-online.target redis-server.service
[Service]
Type=$type
$( [ "$type" = oneshot ] && echo RemainAfterExit=yes )
User=$(id -un)
WorkingDirectory=$NEXUS_ROOT
ExecStart=/bin/bash -c 'set -a; . "$RUNENV"; . "$SECRETS"; set +a; $exec'
$( [ -n "$stop" ] && echo "ExecStop=/bin/bash -c 'set -a; . \"$RUNENV\"; . \"$SECRETS\"; set +a; $stop'" )
$( [ "$type" = simple ] && echo "Restart=always" )
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
}

svc_detach() { # name cmd
  local py="$NEXUS_ROOT/services/nexus-api/.venv/bin/python"
  (set -a; . "$RUNENV"; . "$SECRETS"; set +a
   SERVICE_CTL_STATE_DIR="$NEXUS_STATE_DIR" "$py" "$NEXUS_ROOT/ops/dev/service_ctl.py" detach "$1" --log "$LOGS/$1.log" -- bash -c "$2")
}

py_start() { # service_ctl name: agent, nexus-api
  local py="$NEXUS_ROOT/services/nexus-api/.venv/bin/python"
  (set -a; . "$RUNENV"; . "$SECRETS"; set +a
   SERVICE_CTL_STATE_DIR="$NEXUS_STATE_DIR" "$py" "$NEXUS_ROOT/ops/dev/service_ctl.py" "$1" start)
}

start_service() { # name type cmd [stop]
  if [ "$SYSTEMD" = 1 ]; then svc_unit "$@"; $SUDO systemctl daemon-reload; $SUDO systemctl enable -q "$1"; $SUDO systemctl restart "$1"
  elif [ "$2" = oneshot ]; then (set -a; . "$RUNENV"; . "$SECRETS"; set +a; cd "$NEXUS_ROOT"; bash -c "$3" >>"$LOGS/$1.log" 2>&1)
  else svc_detach "$1" "$3"; fi
}

# Drop standalone units left by older installs. astra_service supervises both.
retire_legacy_units() {
  if [ -z "${RETIRE_UNITS_UNDER_TEST:-}" ]; then
    command -v systemctl >/dev/null 2>&1 || return 0
    [ -d /run/systemd/system ] || return 0
  fi
  local name unit dir removed=0
  dir="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
  for name in nexus-task-mcp nexus-web; do
    unit="$dir/${name}.service"
    if [ -e "$unit" ] || $SUDO systemctl cat "${name}.service" >/dev/null 2>&1; then
      $SUDO systemctl stop "${name}.service" >/dev/null 2>&1 || true
      $SUDO systemctl disable "${name}.service" >/dev/null 2>&1 || true
      if [ -e "$unit" ]; then
        $SUDO rm -f "$unit"
        removed=1
      fi
    fi
  done
  if [ "$removed" = 1 ]; then
    $SUDO systemctl daemon-reload
  fi
} # end retire_legacy_units

step_services() {
  detect_systemd
  retire_legacy_units
  log "启动服务（$( [ "$SYSTEMD" = 1 ] && echo systemd || echo python )）"
  local R="$NEXUS_ROOT"
  start_service nexus-astra oneshot "cd $R/services/nexus-api && .venv/bin/python $NEXUS_ROOT/ops/dev/astra_service.py dev restart" "cd $R/services/nexus-api && .venv/bin/python $NEXUS_ROOT/ops/dev/astra_service.py dev stop"
  # Task MCP、Celery、Gateway 与 OpenCode 由 astra_service dev 拉起。
  if [ "$SYSTEMD" = 1 ]; then
    start_service nexus-agent-service simple "exec $R/services/agent-service/.venv-agentscope/bin/python $R/services/agent-service/agent_service.py"
  else
    py_start agent
  fi
  "$R/services/nexus-api/.venv/bin/python" "$NEXUS_ROOT/ops/dev/service_ctl.py" wait-http http://127.0.0.1:18000/health --timeout 120 --header "X-User-ID: nexus" \
    || die "Agent Service(18000) 未就绪"
  if [ "$SYSTEMD" = 1 ]; then
    start_service nexus-api simple "cd $R/services/nexus-api && exec .venv/bin/uvicorn nexus_api.service:app --host 127.0.0.1 --port 18120"
  else
    py_start nexus-api
  fi
  wait_http http://127.0.0.1:18120/api/v1/knowledge "Nexus API(18120)" 60
}

step_gateway_route() {
  log "上游节点与模型路由由 API 网关控制面配置；安装器不持有厂商密钥"
}

# ---------- llm_wiki（已有服务器版本，按局域网地址接入） ----------
step_llm_wiki() {
  [ "$WITH_WIKI" = 1 ] || return 0
  local base="${ASTRA_LLM_WIKI_API_BASE_URL:-}"
  [ -n "$base" ] || die "--with-llm-wiki 需要 ASTRA_LLM_WIKI_API_BASE_URL（例如 http://10.0.0.8:19828/api/v1）"
  base="${base%/}"
  log "llm_wiki（接入已有服务器 $base）"
  (set -a; . "$RUNENV"; . "$SECRETS"; set +a; cd "$NEXUS_ROOT/services/nexus-api" && .venv/bin/python -m app.deployment wiki)

  # 模型端点由 llm_wiki 服务端配置为 Nexus 网关（NEXUS_GATEWAY_OPENAI_BASE + NEXUS_GATEWAY_API_KEY）。
  log "触发图书馆重新同步"
  curl -fsS -m 30 -X POST http://127.0.0.1:18120/api/library/service/resync >/dev/null || die "图书馆重新同步请求失败"
}

step_remote_worker() {
  [ -n "$REMOTE_WORKER" ] || return 0
  log "远程 Worker $REMOTE_WORKER_ID @ $REMOTE_WORKER"
  (REMOTE_WORKER="$REMOTE_WORKER" REMOTE_WORKER_ID="$REMOTE_WORKER_ID" REMOTE_WORKER_USER="$REMOTE_WORKER_USER" REMOTE_WORKER_PORT="$REMOTE_WORKER_PORT" \
   REMOTE_WORKER_OPENCODE_PORT="$REMOTE_WORKER_OPENCODE_PORT" REMOTE_WORKER_WORKSPACE="$REMOTE_WORKER_WORKSPACE" REMOTE_WORKER_SSH_PASSWORD="$REMOTE_WORKER_SSH_PASSWORD" \
   "$NEXUS_ROOT/services/nexus-api/.venv/bin/python" - <<'PY'
import os, httpx
e = os.environ; B = "http://127.0.0.1:18120/api"; c = httpx.Client(timeout=900)
wid = e["REMOTE_WORKER_ID"]
env = {"mode": "LINUX_REMOTE", "name": wid, "host": e["REMOTE_WORKER"], "port": int(e["REMOTE_WORKER_PORT"]),
       "username": e["REMOTE_WORKER_USER"], "ssh_password": e["REMOTE_WORKER_SSH_PASSWORD"],
       "opencode_server_port": int(e["REMOTE_WORKER_OPENCODE_PORT"]), "workspace_root": e["REMOTE_WORKER_WORKSPACE"],
       "model_name": "nexus-gateway/" + e["NEXUS_DEFAULT_MODEL"]}
if c.get(f"{B}/employees/{wid}").status_code == 404:
    c.post(f"{B}/employees", json={"id": wid, "name": wid, "role": "remote worker", "execution_environment": env}).raise_for_status()
ok = True
with c.stream("POST", f"{B}/employees/{wid}/environment/deploy") as r:
    r.raise_for_status()
    for line in r.iter_lines():
        if '"FAILED"' in line or '"failed"' in line: ok = False
print("remote deploy", "ok" if ok else "FAILED (see evidence stream / 管理台)")
raise SystemExit(0 if ok else 1)
PY
  ) || die "远程 Worker 部署失败"
}

step_health() {
  log "健康检查"
  local fails=0 u
  chk() { if curl -fsS -m 10 ${3:+-H "$3"} -o /dev/null "$1"; then printf '  ✓ %s\n' "$2"; else printf '  ✗ %s (%s)\n' "$2" "$1"; fails=$((fails+1)); fi; }
  redis-cli -p 6379 ping >/dev/null 2>&1 && echo "  ✓ Redis" || { echo "  ✗ Redis"; fails=$((fails+1)); }
  chk http://127.0.0.1:18120/api/health "Nexus API 18120"
  chk http://127.0.0.1:18000/health "Agent Service 18000" "X-User-ID: nexus"
  chk http://127.0.0.1:18120/api/v1/knowledge "Nexus API 18120"
  ss -ltn | grep -q ":${ASTRA_SERVICE_GATEWAY_PORT:-18110} " && echo "  ✓ Gateway 18110" || { echo "  ✗ Gateway 18110"; fails=$((fails+1)); }
  ss -ltn | grep -q ':18201 ' && echo "  ✓ Library MCP 18201" || { echo "  ✗ Library MCP 18201"; fails=$((fails+1)); }
  ss -ltn | grep -q ':18202 ' && echo "  ✓ Task MCP 18202" || { echo "  ✗ Task MCP 18202"; fails=$((fails+1)); }
  ss -ltn | grep -q ':18203 ' && echo "  ✓ Schedule MCP 18203" || { echo "  ✗ Schedule MCP 18203"; fails=$((fails+1)); }
  chk http://127.0.0.1:18120/ "Nexus Web(18120)"
  if [ "$WITH_WIKI" = 1 ]; then
    chk "${ASTRA_LLM_WIKI_API_BASE_URL%/}/health" "llm_wiki"
    u="$(curl -fsS -m 30 http://127.0.0.1:18120/api/library/service/status || true)"
    case "$u" in *'"healthy":true'*) echo "  ✓ 图书馆知识服务 healthy";; *) echo "  ✗ 图书馆知识服务未 healthy"; fails=$((fails+1));; esac
  fi
  [ "$fails" -eq 0 ] || die "$fails 项健康检查失败"
  touch "$NEXUS_STATE_DIR/installed"
  log "部署完成。日志：$LOGS 与 $NEXUS_ROOT/.astra/logs" || die "$fails 项健康检查失败，见 DEPLOYMENT_GUIDE.md §7 排障"
}

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then return 0; fi

preflight
[ "$PREFLIGHT_ONLY" = 1 ] && exit 0
step_checkout; step_deps; step_secrets; step_runenv; step_migrate
step_services; step_gateway_route; step_llm_wiki; step_remote_worker; step_health
