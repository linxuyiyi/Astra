"""Nexus-side SSH bootstrap; no target Python is needed to prepare Python."""

from __future__ import annotations

import shlex
from urllib.parse import urlsplit

PYTHON_VERSION = "3.11.17"
PYTHON_SHA256 = "bfb74ad39efae27cda510f134ab408e00f9992c56851cfc0b1cdb5646da11599"


def managed_python(environment_id: int) -> str:
    return f"/opt/nexus/workers/{environment_id}/python-{PYTHON_VERSION}/bin/python3.11"


def download_base(settings) -> str:
    base = settings.remote_download_base_url.rstrip("/")
    parsed = urlsplit(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.geturl() != base
    ):
        raise RuntimeError("远端下载镜像必须是无凭据的 HTTPS 根地址")
    return base


def python_probe(interpreter: str) -> str:
    source = (
        "import sys,ssl,venv,ensurepip,sqlite3,ctypes,lzma; "
        "sys.exit(0 if sys.version_info >= (3,11) else 1)"
    )
    return f"{shlex.quote(interpreter)} -I -c {shlex.quote(source)}"


def dependency_script(base: str, *, build_python: bool = True) -> str:
    """Use the administrator's OS repositories; isolate the EOL CentOS repo."""
    reuse = "" if build_python else r'''
ready=true
for tool in curl tar xz git flock sha256sum; do
  command -v "$tool" >/dev/null || ready=false
done
if test "$ready" = true; then exit 0; fi
'''
    return (r'''
test "$(uname -s)" = Linux || { echo '只支持 Linux' >&2; exit 1; }
case "$(uname -m)" in x86_64|aarch64) ;; *) echo '只支持 x86_64 / aarch64' >&2; exit 1 ;; esac
''' + reuse + r'''
if test "$(id -u)" = 0; then
  if command -v apt-get >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
    apt-get update -qq
    packages=(ca-certificates curl tar xz-utils unzip util-linux build-essential
      libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev libffi-dev liblzma-dev uuid-dev)
    command -v git >/dev/null || packages+=(git)
    apt-get install -y -qq --no-upgrade "${packages[@]}"
  elif command -v yum >/dev/null; then
    . /etc/os-release
    yum_args=()
    if test "${ID:-}" = centos && test "${VERSION_ID%%.*}" = 7; then
      repo_dir=$(mktemp -d)
      trap 'rm -rf "$repo_dir"' EXIT
      cat >"$repo_dir/nexus-vault.repo" <<'REPO'
[nexus-vault-base]
name=CentOS 7 archive base
baseurl=DOWNLOAD_BASE/centos-vault/7.9.2009/os/$basearch/
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-CentOS-7
enabled=1
[nexus-vault-updates]
name=CentOS 7 archive updates
baseurl=DOWNLOAD_BASE/centos-vault/7.9.2009/updates/$basearch/
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-CentOS-7
enabled=1
REPO
      yum_args=(--setopt="reposdir=$repo_dir")
    fi
    packages=(ca-certificates curl tar xz unzip util-linux gcc gcc-c++ make
      openssl-devel zlib-devel bzip2-devel readline-devel sqlite-devel libffi-devel xz-devel libuuid-devel)
    command -v git >/dev/null || packages+=(git)
    yum "${yum_args[@]}" -y -q install "${packages[@]}"
  else
    echo '不支持的系统包管理器，需要 Debian/Ubuntu 或 RHEL 系列' >&2; exit 1
  fi
fi
for tool in curl tar xz git flock sha256sum; do
  command -v "$tool" >/dev/null || { echo "缺少 $tool，请管理员安装系统依赖" >&2; exit 1; }
done
''').replace("DOWNLOAD_BASE", base)


def python_build_script(interpreter: str, base: str) -> str:
    prefix = interpreter.rsplit("/bin/", 1)[0]
    return (
        f"prefix={shlex.quote(prefix)}\n"
        f"python_bin={shlex.quote(interpreter)}\n"
        f"python_url={shlex.quote(f'{base}/python/{PYTHON_VERSION}/Python-{PYTHON_VERSION}.tar.xz')}\n"
        f"python_sha256={shlex.quote(PYTHON_SHA256)}\n"
        + r'''
mkdir -p "$(dirname "$prefix")"
exec 9>"$prefix.lock"
flock -n 9 || { echo '同一 Worker 正在准备 Python，请稍后重试' >&2; exit 1; }
'''
        + f"if {python_probe(interpreter)} >/dev/null 2>&1; then exit 0; fi\n"
        + r'''
if test -e "$prefix"; then
  echo '受管 Python 目录已存在但不可用，请管理员检查；保留现场' >&2; exit 1
fi
for tool in cc make; do
  command -v "$tool" >/dev/null || { echo "编译 Python 缺少 $tool，请管理员安装构建依赖" >&2; exit 1; }
done
build=$(mktemp -d "$(dirname "$prefix")/.python-build.XXXXXX")
complete=false
cleanup() {
  if test "$complete" != true; then tail -n 12 "$build"/*.log >&2 2>/dev/null || true; fi
  rm -rf "$build"
  if test "$complete" != true; then rm -rf "$prefix"; fi
}
trap cleanup EXIT
curl --fail --location --silent --show-error --connect-timeout 15 --max-time 300 --retry 2 \
  "$python_url" -o "$build/python.tar.xz"
printf '%s  %s\n' "$python_sha256" "$build/python.tar.xz" | sha256sum -c -
mkdir "$build/source"
tar -xJf "$build/python.tar.xz" -C "$build/source" --strip-components=1
cd "$build/source"
./configure --prefix="$prefix" --with-ensurepip=install >"$build/configure.log" 2>&1
make -s -j2 >"$build/make.log" 2>&1
make -s altinstall >"$build/install.log" 2>&1
'''
        + f"{python_probe(interpreter)} >/dev/null 2>&1 || "
        + "{ echo 'Python 编译后模块验证失败；请检查 OpenSSL >=1.1.1 及开发库，"
        + "或显式配置可用解释器' >&2; exit 1; }\n"
        + "complete=true\n"
    )


async def prepare_python(worker, emit) -> None:
    """Each command is bounded remotely and gated by the existing lease."""
    base = download_base(worker.settings)
    interpreter = worker._agent_interpreter()
    await worker.step(emit, "Python 运行环境", "running")
    await worker._ensure_safe()
    # Probe failure is data; SSH/transport failure still propagates normally.
    ready = await worker._command(
        f"if {python_probe(interpreter)} >/dev/null 2>&1; then printf READY; else printf MISSING; fi"
    )
    explicit = bool(worker.settings.remote_python.strip())
    if ready != "READY" and explicit:
        raise RuntimeError(
            "指定的远端 Python 不可用：需要 Python ≥3.11 及 "
            "SSL、venv、ensurepip、sqlite3、ctypes、lzma"
        )
    await worker._bootstrap_idle(interpreter_ready=ready == "READY")
    # A usable managed interpreter can be reused without compiling again.
    await worker.step(emit, "系统依赖", "running")
    await worker._ensure_safe()
    await worker._command(
        "timeout -k 10 300 bash -euc "
        + shlex.quote(dependency_script(base, build_python=ready != "READY")),
        timeout=315,
    )
    await worker.step(emit, "系统依赖", "passed")
    if ready != "READY":
        await worker._ensure_safe()
        seconds = worker.settings.remote_deploy_timeout_seconds
        await worker._command(
            f"timeout -k 10 {seconds} bash -euc "
            + shlex.quote(python_build_script(interpreter, base)),
            timeout=seconds + 15,
        )
    await worker._ensure_safe()
    await worker.step(emit, "Python 运行环境", "passed", interpreter)
