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
  local f="$1" line key
  [ -f "$f" ] || return 0
  [ "$(stat -c %a "$f")" -le 600 ] || warn "$f 权限过宽，建议 chmod 600"
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"; line="${line%"${line##*[![:space:]]}"}"
    [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || continue
    key="${line%%=*}"
    [ -n "${!key:-}" ] || export "$key=${line#*=}"
  done < "$f"
}
[ -n "$ENV_FILE" ] && { [ -f "$ENV_FILE" ] || die "找不到 --env-file $ENV_FILE"; load_env_file "$ENV_FILE"; }
load_env_file "$SCRIPT_DIR/../env/.env"

: "${NEXUS_REPO_URL:=https://github.com/linxuyiyi/Astra}"
: "${NEXUS_BRANCH:=v1.4.8}"
: "${NEXUS_ROOT:=/opt/nexus}"
: "${NEXUS_STATE_DIR:=/var/lib/nexus}"
: "${NEXUS_BIND_HOST:=127.0.0.1}"
: "${OPENAI_BASE_URL:=https://api.deepseek.com}"
: "${OPENAI_MODEL:=deepseek-flash}"
: "${REMOTE_WORKER_ID:=p2-remote}"; : "${REMOTE_WORKER_USER:=root}"; : "${REMOTE_WORKER_PORT:=22}"
: "${REMOTE_WORKER_OPENCODE_PORT:=14096}"; : "${REMOTE_WORKER_WORKSPACE:=/root/nexus-remote}"
PORTS=(6379 18000 18110 18120 18201 18202 14096)

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
  have docker && ok "docker $(docker --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)" || note "未安装 docker（仅 llm_wiki 本机部署需要）"
  x="$( (opencode --version || "$HOME/.local/bin/opencode" --version) 2>/dev/null | tail -1 || true)"
  if [ "$(oc_ver "$x")" = "$OPENCODE_VERSION" ]; then ok "OpenCode $OPENCODE_VERSION"; else note "OpenCode ${x:-未安装}，将安装 opencode-ai@$OPENCODE_VERSION"; fi
  for x in "${PORTS[@]}"; do
    if ss -ltnH "( sport = :$x )" 2>/dev/null | grep -q .; then
      if systemctl is-active --quiet "nexus-*" 2>/dev/null || [ -f "$NEXUS_STATE_DIR/installed" ]; then note "端口 $x 已占用（视为已部署的 Nexus 服务）"; else bad "端口 $x 已被占用"; fi
    else ok "端口 $x 空闲"; fi
  done
  [ -n "${DEEPSEEK_API_KEY:-}" ] && ok "DEEPSEEK_API_KEY 已提供" || bad "缺少 DEEPSEEK_API_KEY"
  if [ -n "${DEEPSEEK_API_KEY:-}" ]; then
    x="$(curl -s -m 20 -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $DEEPSEEK_API_KEY" "${OPENAI_BASE_URL%/}/models" || true)"
    [ "$x" = 200 ] && ok "DeepSeek /models 200" || note "DeepSeek /models 返回 $x（上游繁忙时可稍后重试）"
  fi
  if [ "$WITH_WIKI" = 1 ]; then
    [ -n "${ASTRA_LLM_WIKI_API_BASE_URL:-}" ] && ok "llm_wiki 地址已配置" || bad "--with-llm-wiki 需要 ASTRA_LLM_WIKI_API_BASE_URL（例如 http://10.0.0.8:19828/api/v1）"
  fi
  if [ -n "$REMOTE_WORKER" ]; then
    [ -n "${REMOTE_WORKER_SSH_PASSWORD:-}" ] && ok "远程 Worker 密码已提供" || bad "--remote-worker 需要 REMOTE_WORKER_SSH_PASSWORD"
    timeout 10 bash -c "exec 3<>/dev/tcp/$REMOTE_WORKER/$REMOTE_WORKER_PORT" 2>/dev/null && ok "$REMOTE_WORKER:$REMOTE_WORKER_PORT 可达" || bad "$REMOTE_WORKER:$REMOTE_WORKER_PORT 不可达"
  fi
  [ "$errs" -eq 0 ] || die "预检失败 $errs 项，已停止（未做任何修改）"
  log "预检通过"
}

# ---------- helpers ----------
upsert_kv() { # file key value  (value never printed)
  local f="$1" k="$2" v="$3" tmp; touch "$f"; chmod 600 "$f"; tmp="$(mktemp)"
  grep -v "^$k=" "$f" > "$tmp" || true; printf '%s=%s\n' "$k" "$v" >> "$tmp"; mv "$tmp" "$f"; chmod 600 "$f"
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
  log "密钥（不存在才生成，保存在 $SECRETS，0600）"
  local v
  # shell-quote URL query parameters and encoded credentials for later sourcing.
  printf -v v '%q' "$ASTRA_DATABASE_URL"
  upsert_kv "$SECRETS" ASTRA_DATABASE_URL "$v"
  printf -v v '%q' "$AGENTSCOPE_SQL_URL"
  upsert_kv "$SECRETS" AGENTSCOPE_SQL_URL "$v"
  v="${ASTRA_GATEWAY_MASTER_KEY:-$(get_kv "$SECRETS" ASTRA_GATEWAY_MASTER_KEY)}"
  [ -n "$v" ] || v="$("$NEXUS_ROOT/services/nexus-api/.venv/bin/python" -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"
  upsert_kv "$SECRETS" ASTRA_GATEWAY_MASTER_KEY "$v"
  v="${NEXUS_MCP_API_KEY:-$(get_kv "$SECRETS" NEXUS_MCP_API_KEY)}"
  [ -n "$v" ] || v="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
  upsert_kv "$SECRETS" NEXUS_MCP_API_KEY "$v"
  upsert_kv "$SECRETS" DEEPSEEK_API_KEY "$DEEPSEEK_API_KEY"
  upsert_kv "$SECRETS" OPENAI_API_KEY "$DEEPSEEK_API_KEY"
  if [ "$WITH_WIKI" = 1 ]; then
    v="${ASTRA_LLM_WIKI_API_TOKEN:-$(get_kv "$SECRETS" ASTRA_LLM_WIKI_API_TOKEN)}"; [ -n "$v" ] || v="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
    upsert_kv "$SECRETS" ASTRA_LLM_WIKI_API_TOKEN "$v"
    v="${ASTRA_LLM_WIKI_PROJECT_ID:-$(get_kv "$SECRETS" ASTRA_LLM_WIKI_PROJECT_ID)}"; [ -n "$v" ] || v="$(python3 -c 'import uuid;print(uuid.uuid4())')"
    upsert_kv "$SECRETS" ASTRA_LLM_WIKI_PROJECT_ID "$v"
  fi
}

step_runenv() {
  log "运行时环境 $RUNENV"
  local R="$NEXUS_ROOT"
  { cat <<ENV
PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
ASTRA_GATEWAY_PROBE_TIMEOUT_SECONDS=30
NEXUS_MCP_TRANSPORT=streamable-http
NEXUS_MCP_HOST=127.0.0.1
NEXUS_MCP_PORT=18201
NEXUS_TASK_MCP_PORT=18202
ASTRA_REDIS_URL=redis://127.0.0.1:6379/1
ASTRA_OUTBOX_INTERVAL=2
ASTRA_OUTBOX_RETRY_SECONDS=5
ASTRA_OPENCODE_MODEL=deepseek/deepseek-flash
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
NEXUS_CHAT_PROVIDER=openai
OPENAI_BASE_URL=$OPENAI_BASE_URL
OPENAI_MODEL=$OPENAI_MODEL
# 经验复盘默认关闭；开启后人工确认完成会创建复盘 Job。
NEXUS_EXPERIENCE_REVIEW_ENABLED=false
NEXUS_EXPERIENCE_REVIEW_GATEWAY_URL=http://127.0.0.1:18110/v1/chat/completions
NEXUS_EXPERIENCE_REVIEW_MODEL=deepseek-flash
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
  if [ "${ASTRA_EXECUTOR_TYPE:-worker}" = "opencode_server" ]; then
    # 本机 OpenCode 调试专用；正式部署只使用远程 Worker。
    local oc="$HOME/.config/opencode/opencode.json"
    if [ ! -f "$oc" ] || ! grep -q '"deepseek"' "$oc"; then
      mkdir -p "$(dirname "$oc")"; [ -f "$oc" ] && cp "$oc" "$oc.bak.$(date +%s)"
      cat > "$oc" <<'JSON'
{"$schema":"https://opencode.ai/config.json","provider":{"deepseek":{"npm":"@ai-sdk/openai-compatible","name":"DeepSeek","options":{"baseURL":"https://api.deepseek.com/v1","apiKey":"{env:DEEPSEEK_API_KEY}"},"models":{"deepseek-flash":{"name":"deepseek-flash"}}}}}
JSON
    fi
  fi
}

step_migrate() {
  log "Nexus 全新数据库初始化（0001_init）"
  (set -a; . "$RUNENV"; . "$SECRETS"; set +a; cd "$NEXUS_ROOT/services/nexus-api" && .venv/bin/python -m alembic upgrade head >/dev/null)
  log "AgentScope 官方 SQL 初始化（独立数据库）"
  (set -a; . "$RUNENV"; . "$SECRETS"; set +a; cd "$NEXUS_ROOT/services/agent-service" && .venv-agentscope/bin/python agentscope_sql.py init)
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
EnvironmentFile=$RUNENV
EnvironmentFile=$SECRETS
ExecStart=/bin/bash -c '$exec'
$( [ -n "$stop" ] && echo "ExecStop=/bin/bash -c '$stop'" )
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
  log "网关路由 deepseek-flash（已存在则跳过）"
  (set -a; . "$SECRETS"; set +a; "$NEXUS_ROOT/services/nexus-api/.venv/bin/python" - <<'PY'
import os, httpx
B = "http://127.0.0.1:18120/api/gateway"; c = httpx.Client(timeout=60)
items = lambda r: r.json() if isinstance(r.json(), list) else r.json().get("items", [])
pools = {p["name"]: p for p in items(c.get(B + "/pools"))}
pool = pools.get("deepseek-pool") or c.post(B + "/pools", json={"name": "deepseek-pool"}).raise_for_status().json()
if not any(n["name"] == "deepseek" for n in items(c.get(B + "/nodes"))):
    c.post(B + "/nodes", json={"name": "deepseek", "base_url": os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
        "pool_id": pool["id"], "api_key": os.environ["DEEPSEEK_API_KEY"], "models": ["deepseek-flash"],
        "request_timeout_seconds": 90}).raise_for_status()
if not any(r["model"] == "deepseek-flash" for r in items(c.get(B + "/routes"))):
    c.post(B + "/routes", json={"model": "deepseek-flash", "pool_id": pool["id"]}).raise_for_status()
print("gateway route ok")
PY
  ) || warn "网关路由配置失败，请在管理台「API 中转」手工配置（见指南 §5.8）"
}

# ---------- llm_wiki（已有服务器版本，按局域网地址接入） ----------
step_llm_wiki() {
  [ "$WITH_WIKI" = 1 ] || return 0
  local base="${ASTRA_LLM_WIKI_API_BASE_URL:-}"
  [ -n "$base" ] || die "--with-llm-wiki 需要 ASTRA_LLM_WIKI_API_BASE_URL（例如 http://10.0.0.8:19828/api/v1）"
  base="${base%/}"
  log "llm_wiki（接入已有服务器 $base）"
  wait_http "$base/health" "llm_wiki" 90
  # 模型端点由 llm_wiki 服务端配置为 Nexus 网关（NEXUS_GATEWAY_OPENAI_BASE + NEXUS_GATEWAY_API_KEY）。
  log "触发图书馆重新同步"
  curl -fsS -m 30 -X POST http://127.0.0.1:18120/api/library/service/resync >/dev/null || warn "图书馆重新同步请求失败"
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
       "model_name": "astra-gateway/deepseek-flash"}
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
  ss -ltn | grep -q ':18110 ' && echo "  ✓ Gateway 18110" || { echo "  ✗ Gateway 18110"; fails=$((fails+1)); }
  ss -ltn | grep -q ':18201 ' && echo "  ✓ Library MCP 18201" || { echo "  ✗ Library MCP 18201"; fails=$((fails+1)); }
  ss -ltn | grep -q ':18202 ' && echo "  ✓ Task MCP 18202" || { echo "  ✗ Task MCP 18202"; fails=$((fails+1)); }
  chk http://127.0.0.1:18120/ "Nexus Web(18120)"
  if [ "$WITH_WIKI" = 1 ]; then
    chk "${ASTRA_LLM_WIKI_API_BASE_URL%/}/health" "llm_wiki"
    u="$(curl -fsS -m 30 http://127.0.0.1:18120/api/library/service/status || true)"
    case "$u" in *'"healthy":true'*) echo "  ✓ 图书馆知识服务 healthy";; *) echo "  ✗ 图书馆知识服务未 healthy"; fails=$((fails+1));; esac
  fi
  touch "$NEXUS_STATE_DIR/installed"
  [ "$fails" -eq 0 ] && log "部署完成。日志：$LOGS 与 $NEXUS_ROOT/.astra/logs" || die "$fails 项健康检查失败，见 DEPLOYMENT_GUIDE.md §7 排障"
}

preflight
[ "$PREFLIGHT_ONLY" = 1 ] && exit 0
step_checkout; step_deps; step_secrets; step_runenv; step_migrate
step_services; step_gateway_route; step_llm_wiki; step_remote_worker; step_health
