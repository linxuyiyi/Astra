#!/usr/bin/env bash
# 可选模块：LLM Wiki Web Edition。独立入口，不属于核心安装路径。
# 依赖：LLM_WIKI_BUNDLE；本机部署需要 docker，远端部署需要 ssh（密码方式还需 sshpass）。
# ingest pump 需要 Chrome/Chromium。失败只影响本模块。
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
log() { printf '\033[1;34m[llm-wiki]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[llm-wiki]\033[0m %s\n' "$*" >&2; exit 1; }

: "${NEXUS_CURRENT_LINK:=/opt/nexus/current}"
: "${NEXUS_CONFIG_DIR:=/etc/nexus}"
: "${NEXUS_SYSTEMD_UNIT_DIR:=/etc/systemd/system}"
: "${NEXUS_LOG_DIR:=/var/log/nexus}"
: "${NEXUS_DATA_ROOT:=/var/lib/nexus/data}"
: "${LLM_WIKI_LOCAL_PORT:=19828}"
: "${LLM_WIKI_REMOTE_PORT:=18080}"
SERVICE_ENV=("EnvironmentFile=$NEXUS_CONFIG_DIR/nexus.env" "EnvironmentFile=$NEXUS_CONFIG_DIR/secrets.env" "EnvironmentFile=$NEXUS_CONFIG_DIR/runtime.env")
TASK_PY="$NEXUS_CURRENT_LINK/services/task-platform/.venv/bin/python"
[ -x "$TASK_PY" ] || die "找不到当前发布的 Python：$TASK_PY"

write_unit() { # unit description exec
  local unit="$1" description="$2" exec="$3" path="$NEXUS_SYSTEMD_UNIT_DIR/$1.service"
  {
    printf '[Unit]\nDescription=Nexus %s\nAfter=network-online.target\nWants=network-online.target\n\n' "$description"
    printf '[Service]\nType=simple\nUser=%s\nWorkingDirectory=%s\n' "${NEXUS_RUN_USER:-root}" "$NEXUS_CURRENT_LINK"
    printf '%s\n' "${SERVICE_ENV[@]}"
    printf 'ExecStart=/bin/bash -lc %q\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=30\n' "$exec"
    printf 'StandardOutput=append:%s/%s.log\nStandardError=append:%s/%s.log\n\n' "$NEXUS_LOG_DIR" "$unit" "$NEXUS_LOG_DIR" "$unit"
    printf '[Install]\nWantedBy=multi-user.target\n'
  } > "$path"
  systemctl daemon-reload
  systemctl enable --now "$unit.service"
}

wiki_ssh() {
  if [ -n "${LLM_WIKI_SSH_PASSWORD:-}" ]; then
    SSHPASS="$LLM_WIKI_SSH_PASSWORD" sshpass -e ssh -o StrictHostKeyChecking=accept-new "root@$LLM_WIKI_HOST" "$@"
  else
    ssh -o StrictHostKeyChecking=accept-new "root@$LLM_WIKI_HOST" "$@"
  fi
}
wiki_scp() {
  if [ -n "${LLM_WIKI_SSH_PASSWORD:-}" ]; then SSHPASS="$LLM_WIKI_SSH_PASSWORD" sshpass -e scp -q "$1" "root@$LLM_WIKI_HOST:$2"
  else scp -q "$1" "root@$LLM_WIKI_HOST:$2"; fi
}

bundle="${LLM_WIKI_BUNDLE:?需要 LLM_WIKI_BUNDLE}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
if [ -d "$bundle" ]; then
  tar czf "$tmp/app.tgz" -C "$(dirname "$bundle")" "$(basename "$bundle")"
else
  (cd "$tmp" && unzip -q "$bundle" && tar xzf ./*.tar.gz && rm -f ./*.tar.gz* && tar czf app.tgz llm-wiki-web-*)
fi
arch_b="$(basename "$(tar tzf "$tmp/app.tgz" | head -1)" | grep -oE 'aarch64|x86_64' || echo aarch64)"

remote_script=$(cat <<RS
set -e
mkdir -p /opt/llm-wiki-web-build /var/lib/llm-wiki && chmod 700 /var/lib/llm-wiki
cd /opt/llm-wiki-web-build && rm -rf app && mkdir app && tar xzf /tmp/llm-wiki-app.tgz -C app && rm -f /tmp/llm-wiki-app.tgz
APPDIR=\$(ls app | head -1)
command -v docker >/dev/null || { echo "docker missing" >&2; exit 1; }
PLAT=linux/arm64; NODEARCH=arm64; [ "$arch_b" = x86_64 ] && { PLAT=linux/amd64; NODEARCH=x64; }
QEMU=""
if [ "$arch_b" = aarch64 ] && [ "\$(uname -m)" = x86_64 ]; then
  [ -x /usr/bin/qemu-aarch64-static ] || curl -fsSL -o /usr/bin/qemu-aarch64-static https://github.com/multiarch/qemu-user-static/releases/download/v7.2.0-1/qemu-aarch64-static
  chmod 755 /usr/bin/qemu-aarch64-static; cp /usr/bin/qemu-aarch64-static .
  mountpoint -q /proc/sys/fs/binfmt_misc || mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc
  [ -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ] || echo ':qemu-aarch64:M::\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\xb7\x00:\xff\xff\xff\xff\xff\xff\xff\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff:/usr/bin/qemu-aarch64-static:' > /proc/sys/fs/binfmt_misc/register
  QEMU="COPY qemu-aarch64-static /usr/bin/qemu-aarch64-static"
fi
[ -f node.tar.xz ] || curl -fsSL -o node.tar.xz https://nodejs.org/dist/v20.18.1/node-v20.18.1-linux-\$NODEARCH.tar.xz
printf 'FROM --platform=%s ubuntu:22.04\n%s\nADD node.tar.xz /opt/\nENV PATH=/opt/node-v20.18.1-linux-%s/bin:\$PATH\nRUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*\nCOPY app/%s/ /opt/llm-wiki-web/\nWORKDIR /opt/llm-wiki-web\nCMD ["./start.sh"]\n' "\$PLAT" "\$QEMU" "\$NODEARCH" "\$APPDIR" > Dockerfile
docker build -q --platform \$PLAT -t llm-wiki-web:latest . >/dev/null
docker rm -f llm-wiki-web >/dev/null 2>&1 || true
docker run -d --name llm-wiki-web --restart=always --network host -e LLM_WIKI_DATA_DIR=/var/lib/llm-wiki -e LLM_WIKI_WEB_HOST=127.0.0.1 -e LLM_WIKI_WEB_PORT=$LLM_WIKI_REMOTE_PORT -e LLM_WIKI_BACKEND_READY_TIMEOUT=180 -v /var/lib/llm-wiki:/var/lib/llm-wiki llm-wiki-web:latest >/dev/null
for i in \$(seq 1 90); do curl -fsS -m 3 127.0.0.1:$LLM_WIKI_REMOTE_PORT/api/v1/health >/dev/null 2>&1 && exit 0; sleep 2; done; exit 1
RS
)

if [ -n "${LLM_WIKI_HOST:-}" ]; then
  wiki_scp "$tmp/app.tgz" /tmp/llm-wiki-app.tgz
  wiki_ssh "bash -s" <<<"$remote_script" || die "远端 llm_wiki 容器未就绪"
  key="$HOME/.ssh/nexus_llmwiki_tunnel"
  [ -f "$key" ] || ssh-keygen -q -t ed25519 -N "" -C nexus-llmwiki-tunnel -f "$key"
  wiki_ssh "grep -q nexus-llmwiki-tunnel ~/.ssh/authorized_keys 2>/dev/null || { mkdir -p ~/.ssh; chmod 700 ~/.ssh; echo 'no-pty,no-agent-forwarding,no-X11-forwarding,command=\"/bin/false\",permitopen=\"127.0.0.1:$LLM_WIKI_REMOTE_PORT\" $(cat "$key.pub")' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys; }"
  write_unit nexus-llm-wiki-tunnel "LLM Wiki tunnel" "exec ssh -N -i '$key' -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o StrictHostKeyChecking=accept-new -L 127.0.0.1:$LLM_WIKI_LOCAL_PORT:127.0.0.1:$LLM_WIKI_REMOTE_PORT root@$LLM_WIKI_HOST"
else
  command -v docker >/dev/null || die "本机部署 llm_wiki 需要 docker"
  cp "$tmp/app.tgz" /tmp/llm-wiki-app.tgz
  bash -s <<<"${remote_script//$LLM_WIKI_REMOTE_PORT/$LLM_WIKI_LOCAL_PORT}" || die "本机 llm_wiki 容器未就绪"
fi

for i in $(seq 1 60); do curl -fsS -m 3 "http://127.0.0.1:$LLM_WIKI_LOCAL_PORT/api/v1/health" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS -m 3 "http://127.0.0.1:$LLM_WIKI_LOCAL_PORT/api/v1/health" >/dev/null || die "llm_wiki 健康检查失败"

log "配置项目与 API token"
LLM_WIKI_LOCAL_PORT="$LLM_WIKI_LOCAL_PORT" python3 - <<'PY'
import json, os, urllib.request
U = f"http://127.0.0.1:{os.environ['LLM_WIKI_LOCAL_PORT']}"
def post(path, body):
    request = urllib.request.Request(U + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(request, timeout=120))
inv = lambda c, a: post("/api/web/invoke", {"command": c, "args": a})
pid = os.environ["ASTRA_LLM_WIKI_PROJECT_ID"]; root = "/var/lib/llm-wiki/projects"; path = f"{root}/nexus"
try: inv("create_project", {"name": "nexus", "path": root})
except Exception: pass
inv("write_file", {"path": f"{path}/.llm-wiki/project.json", "contents": json.dumps({"id": pid, "createdAt": int(__import__('time').time()*1000)})})
proj = {"id": pid, "name": "nexus", "path": path}
state = {"lastProject": proj, "recentProjects": [proj],
  "projectRegistry": {pid: {"id": pid, "path": path, "name": "nexus", "lastOpened": 0}},
  "apiConfig": {"enabled": True, "allowUnauthenticated": False, "allowLanAccess": False, "mcpEnabled": True, "token": os.environ["ASTRA_LLM_WIKI_API_TOKEN"]},
  "llmConfig": {"provider": "custom", "apiKey": os.environ.get("DEEPSEEK_API_KEY", ""), "model": os.environ.get("OPENAI_MODEL", "deepseek-flash"),
                "ollamaUrl": "", "customEndpoint": os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com").rstrip("/") + "/v1",
                "maxContextSize": 200000, "apiMode": "chat_completions"},
  "embeddingConfig": {"enabled": False, "endpoint": "", "apiKey": "", "model": ""},
  "sourceWatchConfig": {"enabled": True, "autoIngest": True, "persistExtractedMarkdown": False, "parsingConcurrency": 1, "ingestConcurrency": 1,
                        "includeExtensions": ["md", "txt"], "excludeExtensions": [], "excludeDirs": [], "excludeGlobs": [], "maxFileSizeMb": 20},
  "sourceWatchAllProjects": True, "projectFileSyncEnabled": True}
for k, v in state.items(): post("/api/web/store", {"name": "app-state.json", "op": "set", "key": k, "value": v})
post("/api/web/store", {"name": "app-state.json", "op": "save"})
inv("open_project", {"path": path})
cur = json.load(urllib.request.urlopen(U + "/api/v1/projects", timeout=30))["currentProject"]
assert cur and cur["id"] == pid
PY

PUMP_VENV="$NEXUS_DATA_ROOT/optional/llm-wiki-pump"
[ -x "$PUMP_VENV/bin/python" ] || python3 -m venv "$PUMP_VENV"
"$PUMP_VENV/bin/python" -c 'import websocket' 2>/dev/null || "$PUMP_VENV/bin/pip" install -q websocket-client
[ -f "$NEXUS_CURRENT_LINK/tools/llm_wiki/llm_wiki_ingest_pump.py" ] || die "仓库缺少 tools/llm_wiki/llm_wiki_ingest_pump.py"
write_unit nexus-llm-wiki-pump "LLM Wiki ingest pump" "LLM_WIKI_UI=http://127.0.0.1:$LLM_WIKI_LOCAL_PORT/ exec '$PUMP_VENV/bin/python' '$NEXUS_CURRENT_LINK/tools/llm_wiki/llm_wiki_ingest_pump.py'"

log "写入可选模块配置并重启核心服务"
python3 - <<PY
from pathlib import Path
path = Path("$NEXUS_CONFIG_DIR/optional.env")
path.write_text(
    "ASTRA_LLM_WIKI_ENABLED=true\n"
    "ASTRA_LLM_WIKI_API_BASE_URL=http://127.0.0.1:$LLM_WIKI_LOCAL_PORT/api/v1\n"
    "ASTRA_LLM_WIKI_TIMEOUT=30\n"
)
path.chmod(0o600)
PY
if [ "$(id -u)" = 0 ] && [ -n "${NEXUS_RUN_USER:-}" ] && [ "$NEXUS_RUN_USER" != root ]; then
  chown -R "$NEXUS_RUN_USER:${NEXUS_RUN_GROUP:-$NEXUS_RUN_USER}" "$NEXUS_DATA_ROOT/optional" "$NEXUS_CONFIG_DIR/optional.env" 2>/dev/null || true
fi
PYTHONPATH="$DEPLOY_DIR" python3 -m nexus_deploy restart || die "核心服务重启失败"
curl -fsS -m 30 -X POST "http://127.0.0.1:${NEXUS_TASK_API_PORT:-18100}/api/library/service/resync" >/dev/null || true
log "LLM Wiki 可选模块完成"
