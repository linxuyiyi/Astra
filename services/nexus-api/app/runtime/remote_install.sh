#!/usr/bin/env bash
# Nexus Worker installer/updater. Paths and versions only; credentials arrive in a private SFTP file.
set -euo pipefail
umask 077
workspace=$1
binary=$2
node_version=$3
opencode_version=$4
npm_registry=$5
payload=$6
operation=$7
python_bin=${8:-python3}
jsonc_py=${9:-}
token_file=${10:-}
cache="$HOME/.cache/nexus-provision"
mkdir -p "$cache"
config_info=""
repo_info=""
askpass=""
step() { printf 'NEXUS_STEP\t%s\t%s\n' "$1" "$2"; }
fail() { printf '%s\n' "$*" >&2; exit 1; }
download() { curl --fail --location --silent --show-error --connect-timeout 15 --max-time 180 --retry 2 "$1" -o "$2"; }
cleanup() {
  set +e
  if test -n "${token_file:-}"; then rm -f "$token_file"; fi
  if test -n "${askpass:-}"; then rm -f "$askpass"; fi
  rm -f "${payload:-}" "${config_info:-}" "${repo_info:-}" 2>/dev/null
}
trap cleanup EXIT

# >>> nexus-installer-functions
json_field() {
  "$python_bin" - "$payload" "$1" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
except Exception:
    sys.stderr.write("payload 不是有效 JSON\n")
    raise SystemExit(2)
value = data
for part in sys.argv[2].split("."):
    if not isinstance(value, dict):
        value = None
        break
    value = value.get(part)
if value is None:
    value = ""
if isinstance(value, bool):
    value = "true" if value else "false"
sys.stdout.write(str(value))
PY
}

assert_native_idle() {
  local port=$1
  if test -z "$port"; then
    printf '缺少 opencode_server_port，拒绝执行维护\n' >&2
    return 1
  fi
  "$python_bin" - "$port" "$workspace" <<'PY'
import json
import socket
import sys
import urllib.parse
import urllib.request

port = int(sys.argv[1])
workspace = sys.argv[2]
probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
probe.settimeout(1.0)
try:
    probe.connect(("127.0.0.1", port))
except ConnectionRefusedError:
    # Only an explicit refusal proves that no OpenCode listener is running.
    raise SystemExit(0)
except OSError:
    sys.stderr.write("无法确认 OpenCode 监听状态，拒绝执行维护\n")
    raise SystemExit(3)
finally:
    probe.close()

url = "http://127.0.0.1:%d/session/status?%s" % (
    port,
    urllib.parse.urlencode({"directory": workspace}),
)
try:
    body = urllib.request.urlopen(url, timeout=3).read()
except Exception:
    sys.stderr.write("无法确认 OpenCode 原生 Session 状态，拒绝执行维护\n")
    raise SystemExit(3)
try:
    states = json.loads(body)
except Exception:
    sys.stderr.write("无法确认 OpenCode 原生 Session 状态，拒绝执行维护\n")
    raise SystemExit(3)
if not isinstance(states, dict):
    sys.stderr.write("无法确认 OpenCode 原生 Session 状态，拒绝执行维护\n")
    raise SystemExit(3)
blocked = False
malformed = False
for item in states.values():
    if not isinstance(item, dict):
        malformed = True
        continue
    kind = item.get("type")
    if kind in {"busy", "retry"}:
        blocked = True
    elif kind != "idle":
        malformed = True
if blocked:
    sys.stderr.write("OpenCode 存在正在执行的 Session，请先停止执行再维护环境\n")
    raise SystemExit(4)
if malformed:
    sys.stderr.write("无法确认 OpenCode 原生 Session 状态，拒绝执行维护\n")
    raise SystemExit(3)
PY
}

validate_port() {
  case "$1" in ''|*[!0-9]*) return 1 ;; esac
  test "$1" -ge 1 && test "$1" -le 65535
}

validate_semver() {
  printf '%s' "$1" | grep -Eq '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$'
}

prepare_git_token() {
  if test -z "${token_file:-}" || ! test -f "$token_file"; then
    return 0
  fi
  chmod 600 "$token_file"
  askpass=$(mktemp)
  local token_ref
  token_ref=$("$python_bin" -c 'import shlex,sys; print(shlex.quote(sys.argv[1]))' "$token_file")
  printf '%s\n' \
    '#!/bin/sh' \
    'case "$1" in' \
    "  *Username*) printf '%s\\n' git ;;" \
    "  *) cat $token_ref ;;" \
    'esac' >"$askpass"
  chmod 700 "$askpass"
}

cleanup_git_token() {
  if test -n "${token_file:-}"; then rm -f "$token_file"; fi
  token_file=""
  if test -n "${askpass:-}"; then rm -f "$askpass"; fi
  askpass=""
}

git_run() {
  if test -n "${askpass:-}"; then
    GIT_TERMINAL_PROMPT=0 GIT_ASKPASS="$askpass" git -c credential.helper= "$@"
  else
    GIT_TERMINAL_PROMPT=0 GIT_ASKPASS='' git "$@"
  fi
}

git_fail() {
  printf 'Git 操作失败：%s\n' "$1" >&2
  exit 1
}

workspace_dirty() {
  test -n "$(git -C "$workspace" status --porcelain --untracked-files=normal 2>/dev/null)"
}

repository_stage() {
  local git_err repo_top repo_top_real origin current_head target
  mkdir -p "$(dirname "$workspace")"
  if test -n "$repo_url"; then
    if git -C "$workspace" rev-parse --git-dir >/dev/null 2>&1; then
      repo_top=$(git -C "$workspace" rev-parse --show-toplevel 2>/dev/null || true)
      repo_top_real=$("$python_bin" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$repo_top" 2>/dev/null || true)
      if test -z "$repo_top" || test "$repo_top_real" != "$workspace_real"; then
        cleanup_git_token
        git_fail '工程目录不是 Git 工作树顶层，已停止并保留现场'
      fi
      origin=$(git -C "$workspace" config --get remote.origin.url 2>/dev/null || true)
      if test "$origin" != "$repo_url"; then
        cleanup_git_token
        git_fail '工程目录的 origin 与绑定仓库不一致，已停止并保留现场'
      fi
      current_head=$(git -C "$workspace" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
      if test "$current_head" != "$repo_branch"; then
        cleanup_git_token
        git_fail '工程目录当前分支与绑定分支不一致，已停止并保留现场'
      fi
      if ! git -C "$workspace" rev-parse --verify HEAD >/dev/null 2>&1; then
        cleanup_git_token
        git_fail '工程仓库没有可用提交，请先创建初始提交'
      fi
      if test "$action" = "update"; then
        if workspace_dirty; then
          cleanup_git_token
          git_fail '工程仓库存在未提交或未跟踪变更，已停止更新并保留现场'
        fi
        prepare_git_token
        git_err=$(mktemp)
        if ! git_run -C "$workspace" fetch --no-tags -- origin "$repo_branch" >/dev/null 2>"$git_err"; then
          rm -f "$git_err"
          cleanup_git_token
          git_fail '无法获取目标分支，请人工检查仓库访问与凭据后重试'
        fi
        rm -f "$git_err"
        cleanup_git_token
        if workspace_dirty; then
          git_fail '获取远端后工作区出现变更，已停止更新并保留现场'
        fi
        current_head=$(git -C "$workspace" rev-parse --verify HEAD 2>/dev/null || true)
        target=$(git -C "$workspace" rev-parse --verify 'FETCH_HEAD^{commit}' 2>/dev/null || true)
        if test -z "$target"; then
          git_fail '无法确定远端目标提交，已停止更新并保留现场'
        fi
        remote_commit=$target
        if test "$current_head" != "$target"; then
          if git -C "$workspace" merge-base --is-ancestor "$target" "$current_head" 2>/dev/null; then
            git_fail '本地分支包含未推送提交，已拒绝更新并保留现场'
          fi
          if ! git -C "$workspace" merge-base --is-ancestor "$current_head" "$target" 2>/dev/null; then
            git_fail '本地分支与目标分支已分叉，已拒绝更新并保留现场'
          fi
          if ! git -C "$workspace" merge --ff-only --no-edit "$target" >/dev/null 2>&1; then
            git_fail '快进合并失败，已停止更新并保留现场'
          fi
          repo_changed=true
        fi
        if test "$(git -C "$workspace" rev-parse HEAD 2>/dev/null || true)" != "$target"; then
          git_fail '更新后的提交与远端目标提交不一致，请人工核对'
        fi
      else
        cleanup_git_token
      fi
    else
      if test "$action" = "update"; then
        cleanup_git_token
        git_fail '工程目录不是 Git 仓库，无法执行更新'
      fi
      if test -e "$workspace" && test -n "$(ls -A "$workspace" 2>/dev/null)"; then
        cleanup_git_token
        git_fail '工程目录不是 Git 仓库且不为空，请先初始化或配置正确的仓库根目录'
      fi
      prepare_git_token
      git_err=$(mktemp)
      if ! git_run clone --branch "$repo_branch" -- "$repo_url" "$workspace" >/dev/null 2>"$git_err"; then
        rm -f "$git_err"
        cleanup_git_token
        git_fail '无法克隆绑定仓库，请人工检查仓库访问与凭据后重试'
      fi
      rm -f "$git_err"
      cleanup_git_token
      origin=$(git -C "$workspace" config --get remote.origin.url 2>/dev/null || true)
      if test "$origin" != "$repo_url"; then
        git_fail '克隆后的 origin 与绑定仓库不一致，请人工核对'
      fi
      current_head=$(git -C "$workspace" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
      if test "$current_head" != "$repo_branch"; then
        git_fail '克隆后的分支与绑定分支不一致，请人工核对'
      fi
      if ! git -C "$workspace" rev-parse --verify HEAD >/dev/null 2>&1; then
        git_fail '工程仓库没有可用提交，请先创建初始提交'
      fi
      repo_changed=true
    fi
  else
    mkdir -p "$workspace"
    if ! git -C "$workspace" rev-parse --git-dir >/dev/null 2>&1; then
      if test -n "$(ls -A "$workspace" 2>/dev/null)"; then
        git_fail '工程目录不是 Git 仓库且不为空，请先初始化或配置正确的仓库根目录'
      fi
      git -C "$workspace" init -q
      git -C "$workspace" -c user.name='Nexus' -c user.email='nexus@localhost' commit -q --allow-empty -m 'chore: initialize employee workspace'
      repo_changed=true
    fi
  fi
  if ! git -C "$workspace" rev-parse --verify HEAD >/dev/null 2>&1; then
    git_fail '工程仓库没有提交，请先创建初始提交'
  fi
}
# <<< nexus-installer-functions

test "$(uname -s)" = Linux || fail '只支持 Linux'
case "$(uname -m)" in x86_64) arch=x64 ;; aarch64) arch=arm64 ;; *) fail '只支持 x86_64 / aarch64' ;; esac
. /etc/os-release
old_glibc=false
glibc=$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}' || true)
if test -n "$glibc" && test "$(printf '2.28\n%s\n' "$glibc" | sort -V | head -1)" != 2.28; then old_glibc=true; fi
if test "$old_glibc" = true && test "$arch" != x64; then fail '旧 glibc 仅支持 x86_64，请升级系统'; fi
if ! "$python_bin" -c 'import sys; sys.exit(sys.version_info<(3,11))' >/dev/null 2>&1; then
  fail '需要 Python ≥3.11'
fi
command -v flock >/dev/null || fail '缺少 flock，请管理员安装系统依赖'

config_path=$(json_field config_path)
test -n "$config_path" || fail 'payload 缺少 config_path'
case "$config_path" in /*) ;; *) fail 'config_path 必须是绝对路径' ;; esac
action=$(json_field action)
case "$action" in deploy|update) ;; *) fail 'payload action 必须是 deploy 或 update' ;; esac
test -n "$workspace" || fail '缺少工程工作区路径'
case "$workspace" in /*) ;; *) fail 'workspace 必须是绝对路径' ;; esac
case "$binary" in /*) ;; *) fail 'binary 必须是每环境的绝对路径' ;; esac
validate_semver "$node_version" || fail 'Node.js 目标版本必须是明确的语义版本（不能使用 latest 或 URL）'
validate_semver "$opencode_version" || fail 'OpenCode 目标版本必须是明确的语义版本（不能使用 latest 或 URL）'
environment_id=$(json_field environment_id)
if test -z "$environment_id"; then
  environment_id=$("$python_bin" - "$config_path" <<'PY'
import os
import sys

name = os.path.basename(os.path.dirname(sys.argv[1]))
sys.stdout.write(name if name.isdigit() else "")
PY
)
fi
case "$environment_id" in ''|*[!0-9]*) fail 'payload environment_id 必须是正整数' ;; esac
test "$environment_id" -gt 0 || fail 'payload environment_id 必须是正整数'
opencode_port=$(json_field opencode_server_port)
validate_port "$opencode_port" || fail 'payload opencode_server_port 必须是 1..65535 的整数'
repo_url=$(json_field repository.url)
repo_branch=$(json_field repository.branch)
repo_branch=${repo_branch:-main}
if test "$action" = "update" && test -z "$repo_url"; then fail 'update 需要 payload.repository.url'; fi
runtime_changed=false
repo_changed=false
remote_commit=""

workspace_real=$("$python_bin" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$workspace")
workspace_hash=$(printf '%s' "$workspace_real" | "$python_bin" -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:24])')
env_lock="$cache/environment-$environment_id.lock"
workspace_lock="$cache/workspace-$workspace_hash.lock"
if [[ "$env_lock" < "$workspace_lock" ]]; then
  first_lock=$env_lock
  second_lock=$workspace_lock
else
  first_lock=$workspace_lock
  second_lock=$env_lock
fi
exec 8>"$first_lock"
flock -n 8 || fail '同一 Worker 或工作区正在执行部署/更新，请等待远端操作结束后重试'
exec 9>"$second_lock"
flock -n 9 || fail '同一 Worker 或工作区正在执行部署/更新，请等待远端操作结束后重试'

step '运行状态检查' running
assert_native_idle "$opencode_port"
step '运行状态检查' passed

step '系统依赖' running
py_mm=$("$python_bin" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
venv_pkg="python${py_mm}-venv"
install_venv=false
if ! "$python_bin" -c 'import ensurepip' >/dev/null 2>&1; then install_venv=true; fi
if test "$(id -u)" = 0; then
  if command -v apt-get >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl tar xz-utils unzip git build-essential libcurl4-openssl-dev libssl-dev libexpat1-dev zlib1g-dev gettext
    if test "$install_venv" = true; then apt-get install -y -qq "$venv_pkg" || apt-get install -y -qq python3-venv; fi
  elif command -v yum >/dev/null; then
    yum_args=()
    if test "${ID:-}" = centos && test "${VERSION_ID%%.*}" = 7; then
      mkdir -p "$cache/repos"
      cat >"$cache/repos/centos-vault.repo" <<'REPO'
[nexus-vault-base]
name=CentOS 7 archive base
baseurl=https://vault.centos.org/7.9.2009/os/$basearch/
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-CentOS-7
enabled=1
[nexus-vault-updates]
name=CentOS 7 archive updates
baseurl=https://vault.centos.org/7.9.2009/updates/$basearch/
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-CentOS-7
enabled=1
REPO
      yum_args=(--setopt="reposdir=$cache/repos")
    fi
    yum "${yum_args[@]}" -y -q install ca-certificates curl tar xz unzip git gcc gcc-c++ make libcurl-devel openssl-devel expat-devel zlib-devel gettext-devel perl-ExtUtils-MakeMaker
  else fail '不支持的系统包管理器，需要 Debian/Ubuntu 或 RHEL 系列'; fi
else
  for tool in curl tar xz git flock sha256sum; do command -v "$tool" >/dev/null || fail "缺少 $tool，请管理员安装系统依赖"; done
fi
if ! "$python_bin" -c 'import ensurepip' >/dev/null 2>&1; then fail "需要安装 $venv_pkg"; fi
step '系统依赖' passed

step 'Node.js 与 npm' running
node_dist=linux-$arch
node_source=https://nodejs.org/dist
if test "$old_glibc" = true; then node_dist=linux-x64-glibc-217; node_source=https://unofficial-builds.nodejs.org/download/release; fi
env_root="$HOME/.local/share/nexus/workers/$environment_id"
node_root="$env_root/node-v$node_version"
npm_prefix="$env_root/npm"
npm_cache="$env_root/npm-cache"
npm_binary="$npm_prefix/bin/opencode"
npm_package="$npm_prefix/lib/node_modules/opencode-ai/package.json"
downloads="$env_root/downloads"
mkdir -p "$env_root" "$downloads"
node_ready=false
if test -x "$node_root/bin/node"; then
  actual_node=$("$node_root/bin/node" --version 2>/dev/null || true)
  if test "$actual_node" = "v$node_version"; then node_ready=true; fi
fi
if test "$node_ready" != true; then
  archive="node-v$node_version-$node_dist.tar.xz"
  download "$node_source/v$node_version/$archive" "$downloads/$archive"
  download "$node_source/v$node_version/SHASUMS256.txt" "$downloads/node-sha256.txt"
  (cd "$downloads"; awk -v file="$archive" '$2 == file { print; found=1 } END { if (!found) exit 1 }' node-sha256.txt | sha256sum -c -)
  tmp_node="$env_root/.node-v$node_version.tmp.$$"
  rm -rf "$tmp_node"
  mkdir -p "$tmp_node"
  tar -xJf "$downloads/$archive" -C "$tmp_node" --strip-components=1
  test -x "$tmp_node/bin/node" || fail 'Node.js 安装包内容不完整'
  actual_node=$("$tmp_node/bin/node" --version 2>/dev/null || true)
  test "$actual_node" = "v$node_version" || fail '下载的 Node.js 实际版本与目标版本不符'
  rm -rf "$node_root"
  mv "$tmp_node" "$node_root"
  rm -f "$downloads/$archive" "$downloads/node-sha256.txt"
  runtime_changed=true
fi
export PATH="$node_root/bin:$PATH"
hash -r
test "$(node --version)" = "v$node_version" || fail '选中的 Node.js 版本与目标版本不符'
step 'Node.js 与 npm' passed

step 'Git 工作树' running
git_version=$(git version | awk '{print $3}')
if test "$(printf '2.20.0\n%s\n' "$git_version" | sort -V | head -1)" != 2.20.0; then
  test "$(id -u)" = 0 || fail 'Git 版本低于 2.20，请管理员升级'
  # The maintained 2.43 line supports CentOS 7's libcurl/OpenSSL headers.
  git_release=2.43.7
  archive="git-$git_release.tar.xz"
  download "https://www.kernel.org/pub/software/scm/git/$archive" "$downloads/$archive"
  download 'https://www.kernel.org/pub/software/scm/git/sha256sums.asc' "$downloads/git-sha256.txt"
  (cd "$downloads"; awk -v file="$archive" '$2 == file { print; found=1 } END { if (!found) exit 1 }' git-sha256.txt | sha256sum -c -)
  tar -xJf "$downloads/$archive" -C "$downloads"
  git_flags=()
  if test "$old_glibc" = true; then git_flags=(CSPRNG_METHOD=openssl); fi
  make -s -C "$downloads/git-$git_release" -j2 prefix=/usr/local NO_TCLTK=YesPlease NO_GETTEXT=YesPlease "${git_flags[@]}" all install
  rm -f "$downloads/$archive" "$downloads/git-sha256.txt"
fi
git version
step 'Git 工作树' passed

step 'OpenCode 安装' running
if test "$old_glibc" = true; then
  test "$(id -u)" = 0 || fail '旧 glibc 需要管理员安装 musl 运行库'
  exec 7>"$cache/musl-runtime.lock"
  flock -n 7 || fail '另一 Worker 正在维护共享 musl 运行库，请等待后重试'
  musl_root="$HOME/.local/share/nexus/musl-runtime"
  if ! test -x "$musl_root/sbin/apk"; then
    alpine_archive=alpine-minirootfs-3.22.6-x86_64.tar.gz
    alpine_url=https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/x86_64
    download "$alpine_url/$alpine_archive" "$downloads/$alpine_archive"
    download "$alpine_url/$alpine_archive.sha256" "$downloads/alpine-sha256.txt"
    (cd "$downloads"; sha256sum -c alpine-sha256.txt)
    mkdir -p "$musl_root"
    tar -xzf "$downloads/$alpine_archive" -C "$musl_root"
    rm -f "$downloads/$alpine_archive" "$downloads/alpine-sha256.txt"
  fi
  # Only populate the shared runtime when its libraries are missing; an active
  # worker must not be mutated by another worker's maintenance.
  if ! test -e "$musl_root/usr/lib/libstdc++.so.6"; then
    SSL_CERT_FILE=/etc/pki/tls/certs/ca-bundle.crt "$musl_root/lib/ld-musl-x86_64.so.1" \
      --library-path "$musl_root/lib:$musl_root/usr/lib" "$musl_root/sbin/apk" \
      --root "$musl_root" --keys-dir "$musl_root/etc/apk/keys" \
      --repositories-file "$musl_root/etc/apk/repositories" --no-scripts add libgcc libstdc++
  fi
  if ! test -e /lib/ld-musl-x86_64.so.1; then ln -s "$musl_root/lib/ld-musl-x86_64.so.1" /lib/ld-musl-x86_64.so.1; fi
  # Only musl's loader reads this file; glibc/Node/Git paths stay untouched.
  musl_paths=/etc/ld-musl-x86_64.path
  if ! grep -Fq "$musl_root/usr/lib" "$musl_paths" 2>/dev/null; then
    test -e "$musl_paths" || printf '%s\n' /lib:/usr/local/lib:/usr/lib >"$musl_paths"
    printf '\n%s\n' "$musl_root/lib:$musl_root/usr/lib" >>"$musl_paths"
  fi
fi
# npm is installed under the Worker-specific prefix, never into a shared global path.
installed_version=$(node -e 'try { const p=require(process.argv[1]); process.stdout.write(p.version || "") } catch {}' "$npm_package" 2>/dev/null || true)
if test "$installed_version" != "$opencode_version" || ! test -x "$npm_binary" || test "$("$npm_binary" --version 2>/dev/null || true)" != "$opencode_version"; then
  npm install --global --prefix "$npm_prefix" --registry "$npm_registry" --cache "$npm_cache" \
    --include=optional --no-audit --no-fund --fetch-retries=2 --fetch-timeout=120000 \
    "opencode-ai@$opencode_version"
  runtime_changed=true
fi
test "$("$npm_binary" --version 2>/dev/null || true)" = "$opencode_version" || fail 'npm 安装的 OpenCode 实际版本与目标版本不符'
mkdir -p "$(dirname "$binary")"
if test "$binary" != "$npm_binary"; then
  current_link=$(readlink "$binary" 2>/dev/null || true)
  if ! test -L "$binary" || test "$current_link" != "$npm_binary"; then
    if test -e "$binary" && ! test -L "$binary"; then rm -f "$binary"; fi
    ln -sfn "$npm_binary" "$binary"
    runtime_changed=true
  fi
fi
actual_opencode_version=$("$binary" --version 2>/dev/null || true)
test "$actual_opencode_version" = "$opencode_version" || fail 'OpenCode 执行路径未指向目标 npm 版本'
step 'OpenCode 安装' passed

step '写入前状态检查' running
assert_native_idle "$opencode_port"
step '写入前状态检查' passed

step '工程目录' running
repository_stage
step '工程目录' passed

step '全局 OpenCode 配置' running
test -n "$jsonc_py" && test -f "$jsonc_py" || fail '缺少 JSONC 解析器'
config_info="$cache/$operation.config.json"
node - "$payload" "$operation" "$python_bin" "$jsonc_py" "$config_info" <<'NODE'
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const {execFileSync} = require('child_process');
const [payloadPath, operation, pythonBin, jsoncPy, resultPath] = process.argv.slice(2);
const payload = JSON.parse(fs.readFileSync(payloadPath, 'utf8'));
const target = payload.config_path;
if (!target) throw Error('缺少 config_path');
if (typeof payload.model !== 'string' || !payload.model) throw Error('缺少 model');
const configDir = path.dirname(target);
fs.mkdirSync(configDir, {recursive: true, mode: 0o700});
const manifestPath = path.join(configDir, 'perenv.manifest.json');
const skillRoot = path.join(configDir, 'skill');
function loadConfig(file) {
  let out;
  try { out = execFileSync(pythonBin, [jsoncPy, file], {encoding: 'utf8'}); }
  catch { throw Error('现有 OpenCode 配置不是有效 JSON，请人工处理，未覆盖配置'); }
  let config;
  try { config = JSON.parse(out); } catch { throw Error('现有 OpenCode 配置不是有效 JSON，请人工处理，未覆盖配置'); }
  if (!config || Array.isArray(config) || typeof config !== 'object') throw Error('全局配置必须是 JSON 对象');
  return config;
}
function loadManifest() {
  try {
    const value = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  } catch {
    return {};
  }
}
function writeAtomic(file, text, mode) {
  const tmp = file + '.tmp-' + operation;
  const fd = fs.openSync(tmp, 'w', mode);
  try {
    fs.writeFileSync(fd, text);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  fs.chmodSync(tmp, mode);
  fs.renameSync(tmp, file);
}
function sameJson(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}
function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
function safeSkillName(name) {
  return typeof name === 'string' && name.length > 0 && /^[A-Za-z0-9._-]+$/.test(name) && name !== '.' && name !== '..';
}
const oldManifest = loadManifest();
const oldProvider = oldManifest.provider;
const oldMcp = new Set(Array.isArray(oldManifest.mcp) ? oldManifest.mcp : []);
const oldSkills = isObject(oldManifest.skills) ? oldManifest.skills : {};
const exists = fs.existsSync(target);
let config = exists ? loadConfig(target) : {};
if (config.provider !== undefined && !isObject(config.provider)) throw Error('现有 provider 配置必须是对象');
if (config.mcp !== undefined && !isObject(config.mcp)) throw Error('现有 mcp 配置必须是对象');
const before = JSON.parse(JSON.stringify(config));
const mcpRefs = Array.isArray(payload.mcp_refs) ? payload.mcp_refs : [];
const skills = Array.isArray(payload.skills) ? payload.skills : [];
const desiredMcp = new Set();
for (const item of mcpRefs) {
  if (item && typeof item.name === 'string' && item.name) desiredMcp.add(item.name);
}
if (payload.mcp) desiredMcp.add('nexus-library');
config.mcp = isObject(config.mcp) ? config.mcp : {};
for (const name of oldMcp) {
  if (!desiredMcp.has(name) && Object.prototype.hasOwnProperty.call(config.mcp, name)) {
    delete config.mcp[name];
  }
}
config.$schema = 'https://opencode.ai/config.json';
if (payload.provider) {
  config.provider = config.provider && typeof config.provider === 'object' ? config.provider : {};
  config.provider['nexus-gateway'] = payload.provider;
} else if (oldProvider === 'nexus-gateway' && config.provider) {
  delete config.provider['nexus-gateway'];
}
function assignManagedMcp(name, value) {
  const current = config.mcp[name];
  if (current !== undefined && !oldMcp.has(name) && name !== 'nexus-library' && !sameJson(current, value)) {
    throw Error('MCP 条目 ' + name + ' 已由其他来源管理，拒绝覆盖');
  }
  config.mcp[name] = value;
}
if (payload.mcp) {
  assignManagedMcp('nexus-library', payload.mcp);
}
for (const item of mcpRefs) {
  if (item && item.name && item.url) {
    assignManagedMcp(item.name, {type: 'remote', url: item.url, enabled: true, oauth: false});
  }
}
config.model = payload.model;
const desiredSkills = {};
let skillsChanged = false;
if (!fs.existsSync(skillRoot)) fs.mkdirSync(skillRoot, {recursive: true, mode: 0o700});
for (const skill of skills) {
  if (!skill || !skill.name || skill.body == null) continue;
  if (!safeSkillName(skill.name)) throw Error('技能名称非法，拒绝写入');
  const body = String(skill.body);
  const hash = crypto.createHash('sha256').update(body, 'utf8').digest('hex');
  desiredSkills[skill.name] = hash;
  const dir = path.join(skillRoot, skill.name);
  const file = path.join(dir, 'SKILL.md');
  let current = null;
  try { current = fs.readFileSync(file, 'utf8'); } catch {}
  if (current === body) continue;
  if (current !== null && !Object.prototype.hasOwnProperty.call(oldSkills, skill.name)) {
    throw Error('技能 ' + skill.name + ' 已由其他来源管理，拒绝覆盖');
  }
  fs.mkdirSync(dir, {recursive: true, mode: 0o700});
  writeAtomic(file, body, 0o600);
  skillsChanged = true;
}
for (const name of Object.keys(oldSkills)) {
  if (Object.prototype.hasOwnProperty.call(desiredSkills, name)) continue;
  if (!safeSkillName(name)) continue;
  fs.rmSync(path.join(skillRoot, name), {recursive: true, force: true});
  skillsChanged = true;
}
const configurationChanged = !sameJson(before, config);
if (configurationChanged) {
  if (exists) {
    const backup = target + '.nexus-backup-' + operation;
    fs.copyFileSync(target, backup);
    fs.chmodSync(backup, 0o600);
  }
  const text = JSON.stringify(config, null, 2) + '\n';
  JSON.parse(text);
  writeAtomic(target, text, 0o600);
} else if (exists) {
  fs.chmodSync(target, 0o600);
}
const manifest = {
  provider: payload.provider ? 'nexus-gateway' : null,
  mcp: Array.from(desiredMcp),
  skills: desiredSkills,
};
if (!sameJson(oldManifest, manifest)) {
  writeAtomic(manifestPath, JSON.stringify(manifest, null, 2) + '\n', 0o600);
} else if (fs.existsSync(manifestPath)) {
  fs.chmodSync(manifestPath, 0o600);
}
writeAtomic(resultPath, JSON.stringify({configuration: configurationChanged, skills: skillsChanged}), 0o600);
NODE
step '全局 OpenCode 配置' passed

repo_commit=$(git -C "$workspace" rev-parse HEAD 2>/dev/null || true)
repo_dirty=false
if workspace_dirty; then repo_dirty=true; fi
if test -n "$repo_url"; then
  meta_branch="$repo_branch"
else
  meta_branch=$(git -C "$workspace" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
fi
repo_info="$cache/$operation.repo.json"
"$python_bin" - "$repo_info" "$repo_url" "$meta_branch" "$repo_commit" "$repo_dirty" "$remote_commit" <<'PY'
import json
import sys
from urllib.parse import urlsplit, urlunsplit

info_path, raw_url, branch, commit, dirty, remote_commit = sys.argv[1:7]

def scrub_url(value):
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme and parsed.netloc:
        host = parsed.hostname or ""
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port:
            host = "%s:%d" % (host, port)
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    if "@" in value:
        userinfo, rest = value.split("@", 1)
        if ":" in userinfo:
            return userinfo.split(":", 1)[0] + "@" + rest
    return value

with open(info_path, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "url": scrub_url(raw_url),
            "branch": branch,
            "commit": commit,
            "dirty": dirty == "true",
            "remote_commit": remote_commit or None,
        },
        handle,
        ensure_ascii=False,
    )
PY
"$python_bin" - "$repo_info" "$config_info" "$runtime_changed" "$repo_changed" "$binary" "$actual_opencode_version" <<'PY'
import json
import sys

repo_path, config_path, runtime_changed, repository_changed, binary, version = sys.argv[1:7]
with open(repo_path, encoding="utf-8") as handle:
    repository = json.load(handle)
with open(config_path, encoding="utf-8") as handle:
    configuration = json.load(handle)
meta = {
    "repository": repository,
    "opencode": {"version": version, "binary": binary},
    "changed": {
        "runtime": runtime_changed == "true",
        "configuration": bool(configuration.get("configuration")),
        "skills": bool(configuration.get("skills")),
        "repository": repository_changed == "true",
    },
}
print("NEXUS_META\t" + json.dumps(meta, ensure_ascii=False, separators=(",", ":")))
PY
