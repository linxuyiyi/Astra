"""Nexus 统一平台部署：显式来源、配置诊断、受管进程和完整验收。"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
import platform
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from configuration import (
    DeploymentError,
    Dialog,
    atomic_write,
    initialize,
    load_config,
    save_env,
)

ROOT = Path(__file__).resolve().parents[2]
SOURCE_FIELDS = ("python_url", "node_base", "pip_index", "npm_registry")


def version(value):
    found = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", value)
    if not found:
        raise DeploymentError("无法识别软件版本")
    return tuple(int(part or 0) for part in found.groups())


def source_manifest(path):
    try:
        data = json.loads((Path(__file__).parent / "sources/versions.json").read_text())
        data.update(path if isinstance(path, dict) else json.loads(path.read_text()))
        for key in SOURCE_FIELDS:
            parsed = urlsplit(data[key])
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or any(c.isspace() for c in data[key])
            ):
                raise ValueError(key)
        for key in ("python_version", "node_version", "git_min", "nginx_min"):
            if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", data[key]):
                raise ValueError(key)
        if version(data["python_version"]) < (3, 11, 0):
            raise ValueError("Python < 3.11")
        if not re.fullmatch(r"[0-9a-f]{64}", data["python_sha256"]):
            raise ValueError("Python checksum")
        if not isinstance(data["npm_lock_hosts"], list) or any(
            not isinstance(host, str) or "://" in host or "/" in host
            for host in data["npm_lock_hosts"]
        ):
            raise ValueError("npm lock hosts")
        if set(data["node_sha256"]) != {"x64", "arm64"} or any(
            not re.fullmatch(r"[0-9a-f]{64}", item)
            for item in data["node_sha256"].values()
        ):
            raise ValueError("Node checksum")
    except (OSError, ValueError, KeyError, TypeError):
        raise DeploymentError(
            "安装源清单无效：需要独立 URL、固定版本和两种架构 SHA256"
        ) from None
    return data


def linux_resources(proc=Path("/proc"), cgroup=Path("/sys/fs/cgroup")):
    """同时读取宿主机与容器配额，不高估容器内存和 CPU。"""
    memory = (
        int(re.search(r"MemTotal:\s+(\d+)", (proc / "meminfo").read_text()).group(1))
        * 1024
    )
    cpus = (
        len(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else (os.cpu_count() or 1)
    )
    directories = [cgroup, cgroup / "memory", cgroup / "cpu"]
    membership = proc / "self/cgroup"
    if membership.is_file():
        for line in membership.read_text().splitlines():
            _number, controllers, relative = line.split(":", 2)
            bases = (
                [cgroup]
                if not controllers
                else [
                    cgroup / item
                    for item in controllers.split(",")
                    if item in ("memory", "cpu")
                ]
            )
            for base in bases:
                current = base / relative.lstrip("/")
                while current == base or base in current.parents:
                    directories.append(current)
                    if current == base:
                        break
                    current = current.parent
    for directory in dict.fromkeys(directories):
        for filename in ("memory.max", "memory.limit_in_bytes"):
            path = directory / filename
            if path.is_file():
                value = path.read_text().strip()
                if value.isdigit() and int(value) > 0:
                    memory = min(memory, int(value))
        quota_path = directory / "cpu.max"
        if quota_path.is_file():
            quota, period = quota_path.read_text().split()
            if quota != "max" and int(period) > 0:
                cpus = min(cpus, int(quota) / int(period))
        elif (directory / "cpu.cfs_quota_us").is_file():
            quota = int((directory / "cpu.cfs_quota_us").read_text())
            period = int((directory / "cpu.cfs_period_us").read_text())
            if quota > 0 and period > 0:
                cpus = min(cpus, quota / period)
    return {"memory_mb": memory // (1024 * 1024), "cpus": cpus}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DeploymentError("下载发生重定向，请配置已确认的最终地址；未切换来源")


def fetch(url, target=None, *, digest=None):
    parsed = urlsplit(url)
    opener = urllib.request.build_opener(NoRedirect())
    label = f"{parsed.hostname}{parsed.path}"
    try:
        with opener.open(
            urllib.request.Request(url, headers={"User-Agent": "Nexus-Deploy/1.4.9"}),
            timeout=30,
        ) as response:
            if response.status != 200:
                raise DeploymentError(f"来源 {label} HTTP {response.status}")
            hasher = hashlib.sha256()
            body = bytearray()
            stream = target.open("wb") if target else contextlib.nullcontext(None)
            deadline, size = time.monotonic() + 300, 0
            with stream as output:
                for chunk in iter(lambda: response.read(131072), b""):
                    size += len(chunk)
                    if time.monotonic() > deadline or size > (
                        512 * 1024 * 1024 if target else 2 * 1024 * 1024
                    ):
                        raise DeploymentError(f"来源 {label} 超时或内容过大")
                    hasher.update(chunk)
                    if output:
                        output.write(chunk)
                    else:
                        body.extend(chunk)
            if digest and hasher.hexdigest() != digest:
                raise DeploymentError(f"来源 {label} SHA256 不符，未安装")
            return bytes(body)
    except (OSError, urllib.error.URLError) as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(exc, urllib.error.HTTPError):
            reason = f"HTTP {exc.code}"
        elif isinstance(reason, ssl.SSLError):
            reason = "TLS/证书验证失败"
        else:
            reason = type(reason or exc).__name__
        raise DeploymentError(f"来源 {label} 访问失败：{reason}；未切换来源") from None


def unpack(archive, target):
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as packed:
        # 不依赖 bootstrap Python 是否已有安全过滤 backport。
        for member in packed.getmembers():
            destination = (target / member.name).resolve()
            if target.resolve() not in destination.parents:
                raise DeploymentError("制品包含不安全路径")
            if member.issym():
                if (
                    member.linkname.startswith("/")
                    or target.resolve()
                    not in (destination.parent / member.linkname).resolve().parents
                ):
                    raise DeploymentError("制品包含不安全链接")
            elif not (member.isfile() or member.isdir()):
                raise DeploymentError("制品包含不安全特殊文件")
        packed.extractall(target)


def quote_nginx(value):
    if any(c in str(value) for c in ("\n", "\r", '"', "$", "\\", "\x00")):
        raise DeploymentError("Nginx 路径或地址含不安全字符")
    return '"' + str(value) + '"'


def nginx_config(root, state, env):
    api, gateway, library = (
        env["NEXUS_API_PORT"],
        env["ASTRA_SERVICE_GATEWAY_PORT"],
        env["NEXUS_MCP_PORT"],
    )
    common = """proxy_http_version 1.1;
      proxy_set_header Host $http_host;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection $nexus_connection;
      proxy_buffering off;
      proxy_request_buffering off;
      proxy_read_timeout 3600s;
      proxy_send_timeout 3600s;
      client_max_body_size 64m;"""
    allowed = []
    for item in env["NEXUS_DEPLOY_WORKER_ALLOW"].split(","):
        network = ipaddress.ip_network(item.strip(), strict=False)
        if network.version != 4 or network.prefixlen == 0:
            raise DeploymentError("禁止全网开放 Worker HTTP 入口")
        allowed.append("allow " + str(network) + ";")
    worker_ip = str(ipaddress.IPv4Address(env["NEXUS_DEPLOY_PUBLIC_IP"]))
    return f"""# Nexus 唯一受管 Nginx 配置；worker_processes 默认跟随可用 CPU。
worker_processes auto;
pid {quote_nginx(state / "nginx/nginx.pid")};
error_log {quote_nginx(state / "logs/nginx-error.log")};
events {{ worker_connections 1024; }}
http {{
  include {quote_nginx(state / "nginx/mime.types")};
  default_type application/octet-stream;
  access_log off;
  add_header X-Nexus-Release {quote_nginx(env.get("NEXUS_DEPLOY_RELEASE", "unreleased"))} always;
  client_body_temp_path {quote_nginx(Path(env.get("NEXUS_DEPLOY_NGINX_CACHE", str(state / "nginx-cache"))) / "body")};
  proxy_temp_path {quote_nginx(Path(env.get("NEXUS_DEPLOY_NGINX_CACHE", str(state / "nginx-cache"))) / "proxy")};
  map $http_upgrade $nexus_connection {{ default upgrade; '' close; }}
  server {{
    listen {int(env["NEXUS_DEPLOY_HTTPS_PORT"])} ssl;
    server_name _;
    ssl_certificate {quote_nginx(env["NEXUS_DEPLOY_TLS_CERT"])};
    ssl_certificate_key {quote_nginx(env["NEXUS_DEPLOY_TLS_KEY"])};
    ssl_protocols TLSv1.2 TLSv1.3;
    location / {{ proxy_pass http://127.0.0.1:{api}; {common} }}
    location /v1/ {{ proxy_pass http://127.0.0.1:{gateway}; {common} }}
  }}
  server {{
    listen {worker_ip}:{int(env["NEXUS_DEPLOY_WORKER_PORT"])};
    server_name _;
    {" ".join(allowed)}
    allow 127.0.0.1;
    allow {worker_ip};
    deny all;
    location /v1/ {{ proxy_pass http://127.0.0.1:{gateway}; {common} }}
    location = /mcp {{ proxy_pass http://127.0.0.1:{library}; {common} }}
    location = /api/worker-gateway/connect {{ proxy_pass http://127.0.0.1:{api}; {common} }}
    location / {{ return 404; }}
  }}
}}
"""


class Deployer:
    def __init__(self, args, dialog=None):
        self.args, self.dialog = args, dialog or Dialog(sys.stdin.isatty())
        self.root, self.state = args.root.resolve(), args.state.resolve()
        self.values = {}
        self.stage = "初始化"
        self.state.mkdir(parents=True, exist_ok=True)
        self.state.chmod(0o700)
        (self.state / "logs").mkdir(exist_ok=True)
        self.log_path = self.state / "logs/deployment.log"
        self.env = dict(os.environ)
        self.python = None
        self.node = None
        self.inferred_remote_keys = set()
        self.release_root = self.root

    def step(self, name):
        self.stage = name
        print("[部署] " + name, flush=True)

    def run(self, command, *, cwd=None, env=None, timeout=300, capture=False):
        with self.log_path.open("ab") as log:
            os.chmod(self.log_path, 0o600)
            process = None
            try:
                process = subprocess.Popen(
                    command,
                    cwd=cwd or self.root,
                    env=env or self.env,
                    stdout=subprocess.PIPE if capture else log,
                    stderr=log,
                    start_new_session=True,
                )
                output, _ = process.communicate(timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                if process and process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                raise DeploymentError(
                    f"{self.stage}：命令超时或被中断，已停止本次命令进程组；详见私有日志"
                ) from None
            except OSError:
                raise DeploymentError(
                    f"{self.stage}：{Path(command[0]).name} 无法执行；详见私有日志"
                ) from None
        if process.returncode:
            tail = self.log_path.read_text(errors="replace").splitlines()[-1:]
            if tail and tail[0].startswith("[失败]"):
                print(tail[0][:250], file=sys.stderr)
            raise DeploymentError(
                f"{self.stage}：{Path(command[0]).name} 退出码 {process.returncode}；详见 {self.log_path}"
            )
        return (output or b"").decode().strip() if capture else ""

    def preflight(self):
        self.step("系统与已有部署探测")
        if platform.system() != "Linux" or platform.machine() not in (
            "x86_64",
            "aarch64",
        ):
            raise DeploymentError(
                "标准平台部署支持 Linux x86_64/aarch64；本地 macOS 可运行隔离组件测试"
            )
        if os.geteuid() != 0:
            raise DeploymentError("自动准备系统依赖及 HTTPS 入口需要 root")
        resources = linux_resources()
        self.available_cpus = resources["cpus"]
        memory = resources["memory_mb"]
        free = shutil.disk_usage(self.state).free // (1024**3)
        print(
            json.dumps(
                {
                    "os": platform.platform(),
                    "arch": platform.machine(),
                    "container": Path("/.dockerenv").exists()
                    or "container" in Path("/proc/1/cgroup").read_text(),
                    "cpus": self.available_cpus,
                    "memory_mb": memory,
                    "disk_free_gb": free,
                },
                ensure_ascii=False,
            )
        )
        if memory < 1900 or free < 10 or self.available_cpus < 2:
            raise DeploymentError(
                "至少需要 2 CPU、1900 MiB 内存及 10 GiB 可用磁盘；建议内存 4 GiB 以上"
            )
        if self.root != ROOT:
            raise DeploymentError(
                "请在目标 Git checkout 中执行唯一入口；--root 必须等于当前仓库，不创建第二份代码"
            )
        # 不将仓库当前开发改动自动提交、stash 或丢弃。
        status = self.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"], capture=True
        )
        if status:
            raise DeploymentError(
                "部署工作区含未提交或未跟踪文件，请先提交或移走；不会覆盖已有代码"
            )

    def initialize_config(self):
        while True:
            try:
                return initialize(self.values, self.root, self.state, self.dialog)
            except DeploymentError as exc:
                print("[配置诊断] " + str(exc))
                choice = self.dialog.ask(
                    "保留并停止(k)、修改配置(m)、删除错误值后重新初始化(r)", "k"
                )
                if choice not in ("m", "r"):
                    raise
                key = self.dialog.ask("要处理的配置键")
                if not re.fullmatch(r"(?:NEXUS|ASTRA|AGENTSCOPE)_[A-Z0-9_]+", key):
                    raise DeploymentError("配置键无效")
                if choice == "r":
                    if key == "ASTRA_GATEWAY_MASTER_KEY":
                        raise DeploymentError(
                            "数据库加密主密钥须恢复原值，不能通过删除再生"
                        )
                    self.values.pop(key, None)
                else:
                    self.values[key] = self.dialog.ask(
                        key + "（敏感值不会回显）", secret=True
                    )

    def configure(self):
        self.step("配置导入与诊断")
        canonical = self.state / "nexus.env"
        paths = (
            [canonical]
            if canonical.is_file()
            and canonical.read_text().startswith("# Nexus 部署器生成")
            else [self.root / "ops/env/.env", self.state / "secrets.env", canonical]
        )
        if self.args.env_file and self.args.env_file.resolve() not in [
            p.resolve() for p in paths
        ]:
            paths.append(self.args.env_file)
        self.values = load_config(paths, os.environ, self.dialog)
        # 只保留本入口的 root/state 值，防止继承环境与实际路径不同。
        for key, value in {
            "NEXUS_ROOT": str(self.root),
            "NEXUS_STATE_DIR": str(self.state),
        }.items():
            if self.values.get(key, value) != value:
                chosen = self.dialog.resolve(
                    key, self.values[key], value, "与执行路径不同"
                )
                if chosen != value:
                    raise DeploymentError(f"{key} 与部署路径不符")
            self.values[key] = value
        self.values = self.initialize_config()
        self.values["NEXUS_DEPLOY_RELEASE"] = self.release_root.parent.name
        saved_sources = self.state / "sources.json"
        source_path = self.args.sources or (
            saved_sources
            if saved_sources.is_file()
            else self.root / "ops/deploy/sources/public.json"
        )
        self.sources = source_manifest(source_path)
        print(
            "[部署] 来源配置："
            + ", ".join(key + "=" + self.sources[key] for key in SOURCE_FIELDS)
        )
        for key, source in {
            "NEXUS_REMOTE_PYTHON_SOURCE_URL": self.sources["python_url"],
            "NEXUS_REMOTE_NODE_BASE_URL": self.sources["node_base"],
            "NEXUS_REMOTE_NPM_REGISTRY": self.sources["npm_registry"],
            "NEXUS_REMOTE_PIP_INDEX_URL": self.sources["pip_index"],
            "NEXUS_REMOTE_NODE_VERSION": self.sources["node_version"],
        }.items():
            if key not in self.values:
                self.inferred_remote_keys.add(key)
                self.values[key] = source

        if (  # noqa: SIM102
            saved_sources.is_file()
            and self.args.sources
            and source_manifest(saved_sources) != self.sources
        ):
            if (
                self.dialog.ask(
                    "安装源配置发生变化，输入 replace 确认替换（旧文件会备份）"
                )
                != "replace"
            ):
                raise DeploymentError("未确认安装源变更")
        self.pending_sources = json.dumps(self.sources, indent=2) + "\n"
        # 参数列表来自当前代码，发现不认识的值必须显式处理。
        known_text = (self.root / "ops/env/.env.example").read_text()
        known = set(
            re.findall(r"\b(?:NEXUS|ASTRA|AGENTSCOPE)_[A-Z0-9_]+", known_text)
        ) | set(
            re.findall(
                r"\b(?:NEXUS|ASTRA|AGENTSCOPE)_[A-Z0-9_]+", Path(__file__).read_text()
            )
        )
        known.update(
            re.findall(
                r"\b(?:NEXUS|ASTRA|AGENTSCOPE)_[A-Z0-9_]+",
                (Path(__file__).parent / "configuration.py").read_text(),
            )
        )
        known.update(("PATH", "HOME", "LANG"))
        for key in sorted(set(self.values) - known):
            selected = self.dialog.resolve(
                key,
                self.values[key],
                "",
                "当前部署模板没有此参数；可保留扩展参数或 r 删除",
                regenerable=True,
            )
            if selected:
                self.values[key] = selected
            else:
                self.values.pop(key)
        self.env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(
                (
                    "ASTRA_",
                    "NEXUS_",
                    "AGENTSCOPE_",
                    "PIP_",
                    "NPM_CONFIG_",
                    "npm_config_",
                )
            )
        }
        self.env.update(self.values)

    def packages(self, build_python=False):
        packages = ["git", "curl", "xz-utils", "nginx", "openssl"]
        manager = next(
            (name for name in ("apt-get", "dnf", "yum") if shutil.which(name)), None
        )
        if not manager:
            raise DeploymentError("缺失依赖但没有 apt/dnf/yum；需要管理员准备依赖")
        repository_dir = self.sources.get("os_repository_dir", "")
        repository_args = []
        if repository_dir:
            directory = Path(repository_dir)
            if not directory.is_absolute() or not directory.is_dir():
                raise DeploymentError(
                    "os_repository_dir 必须为已有绝对目录，内含已确认的系统仓库配置"
                )
            if manager == "apt-get":
                repository_args = [
                    "-o",
                    "Dir::Etc::sourcelist=/dev/null",
                    "-o",
                    "Dir::Etc::sourceparts=" + str(directory),
                ]
            else:
                repository_args = ["--setopt=reposdir=" + str(directory)]
        if build_python:
            packages += (
                "build-essential libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev libffi-dev liblzma-dev uuid-dev"
                if manager == "apt-get"
                else "gcc gcc-c++ make openssl-devel zlib-devel bzip2-devel readline-devel sqlite-devel libffi-devel xz-devel libuuid-devel"
            ).split()
        if manager != "apt-get":
            packages = ["xz" if p == "xz-utils" else p for p in packages]
        # 不让 apt 首次安装 Nginx 时抢先以系统服务启动，保留管理员原有策略。
        policy = Path("/usr/sbin/policy-rc.d")
        created = manager == "apt-get" and not policy.exists()
        try:
            if created:
                atomic_write(policy, "#!/bin/sh\nexit 101\n", mode=0o755)
            if manager == "apt-get":
                self.run([manager, *repository_args, "update", "-qq"], timeout=180)
                self.run(
                    [
                        manager,
                        *repository_args,
                        "install",
                        "-y",
                        "--no-upgrade",
                        *packages,
                    ],
                    env=dict(self.env, DEBIAN_FRONTEND="noninteractive"),
                    timeout=600,
                )
            else:
                self.run(
                    [manager, *repository_args, "install", "-y", *packages], timeout=600
                )
        finally:
            if created:
                policy.unlink(missing_ok=True)

    def repair_source(self, key, failure):
        print("[安装源诊断] " + str(failure))
        if self.dialog.ask("保留并停止(k)、明确修改当前来源(m)", "k") != "m":
            raise failure
        previous = self.sources[key]
        candidate = dict(self.sources)
        candidate[key] = self.dialog.ask(key + " 完整 URL（不会自动选择其他来源）")
        self.sources = source_manifest(candidate)
        remote_key = {
            "python_url": "NEXUS_REMOTE_PYTHON_SOURCE_URL",
            "node_base": "NEXUS_REMOTE_NODE_BASE_URL",
            "pip_index": "NEXUS_REMOTE_PIP_INDEX_URL",
            "npm_registry": "NEXUS_REMOTE_NPM_REGISTRY",
        }.get(key)
        if (
            remote_key in self.inferred_remote_keys
            and self.values.get(remote_key) == previous
        ):
            self.values[remote_key] = self.sources[key]
        self.pending_sources = json.dumps(self.sources, indent=2) + "\n"

    def artifact(self, key, suffix, archive, digest):
        while True:
            try:
                return fetch(
                    self.sources[key].rstrip("/") + suffix, archive, digest=digest
                )
            except DeploymentError as exc:
                self.repair_source(key, exc)

    def dependencies(self):
        self.step("依赖与安装源验证")
        for key, suffix in (
            ("pip_index", "/pip/"),
            ("npm_registry", "/typescript/latest"),
        ):
            while True:
                try:
                    content = fetch(self.sources[key].rstrip("/") + suffix)
                    if key == "pip_index" and b"href=" not in content:
                        raise DeploymentError("PyPI 来源不是有效包索引")
                    if key == "npm_registry" and not json.loads(content).get("version"):
                        raise DeploymentError("npm 来源不是有效包 metadata")
                    break
                except (DeploymentError, ValueError) as exc:
                    self.repair_source(key, DeploymentError(str(exc)))
        # Python 需要可用完整模块，显式解释器失败不换源替代。
        python_name = "python" + ".".join(self.sources["python_version"].split(".")[:2])
        python_candidates = (
            [self.values["NEXUS_DEPLOY_PYTHON"]]
            if self.values.get("NEXUS_DEPLOY_PYTHON")
            else [
                str(
                    self.state
                    / f"python-{self.sources['python_version']}/bin/{python_name}"
                ),
                sys.executable,
            ]
        )
        probe = "import sys,ssl,venv,ensurepip,sqlite3,ctypes,lzma;sys.exit(0 if sys.version_info>=(3,11) else 1)"
        for binary in python_candidates:
            try:
                if (
                    subprocess.run(
                        [binary, "-I", "-c", probe],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=15,
                    ).returncode
                    == 0
                ):
                    self.python = binary
                    break
            except OSError:
                pass
        if not self.python and self.values.get("NEXUS_DEPLOY_PYTHON"):
            raise DeploymentError("显式 Python 不可用或模块不完整")
        missing_system = any(
            not shutil.which(binary) for binary in ("git", "nginx", "openssl", "xz")
        )
        if missing_system or not self.python:
            self.packages(build_python=not self.python)
        if not self.python:
            if self.values.get("NEXUS_DEPLOY_PYTHON"):
                raise DeploymentError("显式 Python 不可用或模块不完整")
            stage = self.state / "python-build"
            stage.mkdir(exist_ok=True)
            archive = stage / "python.tar.xz"
            self.artifact("python_url", "", archive, self.sources["python_sha256"])
            unpack(archive, stage)
            source = stage / ("Python-" + self.sources["python_version"])
            prefix = self.state / ("python-" + self.sources["python_version"])
            self.run(
                ["./configure", "--prefix=" + str(prefix), "--with-ensurepip=install"],
                cwd=source,
                timeout=180,
            )
            self.run(
                ["make", "-j", str(min(int(self.available_cpus), 4))],
                cwd=source,
                timeout=1800,
            )
            self.run(["make", "install"], cwd=source, timeout=300)
            self.python = str(prefix / "bin" / python_name)
            self.run([self.python, "-I", "-c", probe])
        node_root = self.state / ("node-v" + self.sources["node_version"])
        candidates = (
            [self.values["NEXUS_DEPLOY_NODE_BINARY"]]
            if self.values.get("NEXUS_DEPLOY_NODE_BINARY")
            else [str(node_root / "bin/node"), shutil.which("node") or ""]
        )
        for binary in candidates:
            if (
                binary
                and Path(binary).is_file()
                and self.run([binary, "--version"], capture=True)
                == "v" + self.sources["node_version"]
            ):
                self.node = binary
                break
        if not self.node:
            if self.values.get("NEXUS_DEPLOY_NODE_BINARY"):
                raise DeploymentError("显式 Node 版本不符或无法执行")
            if platform.libc_ver()[0] != "glibc" or version(platform.libc_ver()[1]) < (
                2,
                28,
                0,
            ):
                raise DeploymentError(
                    "受管 Node 需要 glibc >=2.28；请显式配置可用 Node，不修补运行库"
                )
            arch = {"x86_64": "x64", "aarch64": "arm64"}[platform.machine()]
            name = f"node-v{self.sources['node_version']}-linux-{arch}"
            stage = self.state / "node-build"
            stage.mkdir(exist_ok=True)
            archive = stage / "node.tar.xz"
            self.artifact(
                "node_base",
                f"/v{self.sources['node_version']}/{name}.tar.xz",
                archive,
                self.sources["node_sha256"][arch],
            )
            unpack(archive, stage)
            ready = stage / name
            if (
                self.run([str(ready / "bin/node"), "--version"], capture=True)
                != "v" + self.sources["node_version"]
            ):
                raise DeploymentError("Node 实际版本不符")
            if node_root.exists():
                node_root.rename(
                    node_root.with_name(
                        node_root.name + ".previous-" + str(time.time_ns())
                    )
                )
            ready.rename(node_root)
            self.node = str(node_root / "bin/node")
        self.env["PATH"] = ":".join(
            dict.fromkeys(
                [str(Path(self.node).parent), *self.env.get("PATH", "").split(":")]
            )
        )
        self.values["PATH"] = self.env["PATH"]
        self.npm = str(Path(self.node).parent / "npm")
        self.run([self.npm, "--version"], capture=True)
        for binary, key in (("git", "git_min"), ("nginx", "nginx_min")):
            # nginx -v writes stderr; nginx -V output is captured explicitly.
            if binary == "nginx":
                result = subprocess.run(
                    ["nginx", "-v"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                detected = result.stdout + result.stderr
            else:
                detected = self.run([binary, "--version"], capture=True)
            if version(detected) < version(self.sources[key]):
                raise DeploymentError(
                    binary + " 低于最低版本，请更新系统仓库中的受支持版本"
                )
        self.values["NEXUS_DEPLOY_NGINX_BINARY"] = shutil.which("nginx")
        print(
            "[依赖] "
            + self.run([self.python, "--version"], capture=True)
            + "; Node "
            + self.run([self.node, "--version"], capture=True)
            + "; npm "
            + self.run([self.npm, "--version"], capture=True)
        )

    def checkout(self):
        self.step("代码版本核对与快进更新")
        branch = self.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], capture=True
        )
        if branch != "v1.4.9":
            raise DeploymentError("标准部署需要 v1.4.9 分支，请先选择正确的发布分支")
        previous = self.run(["git", "rev-parse", "HEAD"], capture=True)
        self.run(["git", "fetch", "origin", branch], timeout=180)
        self.run(["git", "merge", "--ff-only", "origin/" + branch])
        if self.run(["git", "rev-parse", "HEAD"], capture=True) != self.run(
            ["git", "rev-parse", "origin/" + branch], capture=True
        ):
            raise DeploymentError("本地提交领先或与远端发布不一致，未执行部署")
        release = self.run(["git", "rev-parse", "HEAD"], capture=True)
        if release != previous:
            print("[部署] 已快进到新版本，重新执行新版本部署程序", flush=True)
            os.execv(
                sys.executable,
                [
                    sys.executable,
                    str(self.root / "ops/deploy/deploy.py"),
                    *sys.argv[1:],
                ],
            )
        self.values["NEXUS_DEPLOY_RELEASE"] = release
        self.env.update(self.values)
        self.release_root = self.state / "releases" / release / "repo"
        if not self.release_root.is_dir():
            archive = self.state / "releases" / release / "source.tar"
            archive.parent.mkdir(parents=True, exist_ok=True)
            self.run(
                ["git", "archive", "--format=tar", "--output", str(archive), release]
            )
            staging = archive.parent / "repo.candidate"
            if staging.exists():
                shutil.rmtree(staging)
            unpack(archive, staging)
            staging.rename(self.release_root)
            archive.unlink()

    def install(self):
        self.step("Python 依赖安装与前端构建")
        # 子进程统一源；--isolated 禁止用户额外 index；隔离构建使用同一 PIP_INDEX_URL。
        package_env = {
            k: v
            for k, v in self.env.items()
            if not k.upper().startswith(("PIP_", "NPM_CONFIG_"))
        }
        package_env.update(
            PIP_CONFIG_FILE=os.devnull,
            PIP_INDEX_URL=self.sources["pip_index"],
            PIP_DISABLE_PIP_VERSION_CHECK="1",
            npm_config_registry=self.sources["npm_registry"],
            npm_config_replace_registry_host="always",
            npm_config_audit="false",
            npm_config_fund="false",
        )
        package_env.update(
            npm_config_userconfig=str(self.state / "npmrc"),
            npm_config_globalconfig=str(self.state / "npmrc.global"),
        )
        atomic_write(
            self.state / "npmrc",
            "registry=" + self.sources["npm_registry"] + "\nstrict-ssl=true\n",
        )
        atomic_write(self.state / "npmrc.global", "")
        self.package_env = package_env
        if (self.release_root.parent / "prepared.json").is_file():
            marker = json.loads(
                (self.release_root.parent / "prepared.json").read_text()
            )
            if (
                marker.get("python_version") != self.sources["python_version"]
                or marker.get("node_version") != self.sources["node_version"]
            ):
                raise DeploymentError(
                    "同一代码发布的工具版本已变化；请在新发布提交中更新版本清单后部署，避免修改在线环境"
                )
            print("[部署] 复用已完成构建的同一代码发布")
            return
        for directory, name, requirements in (
            (self.release_root / "services/nexus-api", ".venv", None),
            (
                self.release_root / "services/agent-service",
                ".venv-agentscope",
                "requirements.txt",
            ),
        ):
            py = directory / name / "bin/python"
            if not py.is_file():
                self.run(
                    [self.python, "-m", "venv", str(directory / name)], env=package_env
                )
            command = [
                str(py),
                "-m",
                "pip",
                "install",
                "--index-url",
                self.sources["pip_index"],
            ]
            command += (
                ["-r", str(directory / requirements)]
                if requirements
                else [str(directory)]
            )
            self.run(command, cwd=directory, env=package_env, timeout=1200)

    def build_web(self):
        self.step("聊天岛与前端构建")
        package_env = self.package_env
        if (self.release_root.parent / "prepared.json").is_file():
            return
        for name in ("chat-island", "web"):
            directory = self.release_root / "services/nexus-api" / name
            if (directory / ".npmrc").is_file():
                for line in (directory / ".npmrc").read_text().splitlines():
                    item = line.strip().split("=", 1)
                    if len(item) == 2 and (
                        (
                            item[0].endswith("registry")
                            and item[1].strip().rstrip("/")
                            != self.sources["npm_registry"].rstrip("/")
                        )
                        or (item[0] == "strict-ssl" and item[1].strip() != "true")
                    ):
                        raise DeploymentError(
                            name + " 项目 npm 配置与指定源/TLS 策略冲突"
                        )
            lock = json.loads((directory / "package-lock.json").read_text())
            registry_host = urlsplit(self.sources["npm_registry"]).netloc
            for package in lock.get("packages", {}).values():
                resolved = package.get("resolved", "")
                if resolved and (
                    urlsplit(resolved).scheme not in ("https", "http")
                    or urlsplit(resolved).netloc
                    not in [registry_host, *self.sources["npm_lock_hosts"]]
                ):
                    raise DeploymentError(
                        f"{name} 锁文件含非配置 registry 制品，拒绝隐式访问"
                    )
            # 禁用任意生命周期下载；本仓库构建脚本显式执行。
            self.run(
                [
                    self.npm,
                    "ci",
                    "--ignore-scripts",
                    "--registry",
                    self.sources["npm_registry"],
                ],
                cwd=directory,
                env=package_env,
                timeout=600,
            )
            self.run(
                [self.npm, "run", "build"], cwd=directory, env=package_env, timeout=600
            )
        if not (self.release_root / "services/nexus-api/web/dist/index.html").is_file():
            raise DeploymentError("前端构建产物缺失")
        atomic_write(
            self.release_root.parent / "prepared.json",
            json.dumps(
                {
                    "release": self.values["NEXUS_DEPLOY_RELEASE"],
                    "python_version": self.sources["python_version"],
                    "node_version": self.sources["node_version"],
                }
            )
            + "\n",
        )

    def validate_connections(self):
        self.step("数据库、Redis 与配置真实连接验证")
        py = str(self.release_root / "services/nexus-api/.venv/bin/python")
        script = """import asyncio,os
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from sqlalchemy.engine import make_url
import redis
async def verify():
 urls=[make_url(os.environ[key]) for key in ('ASTRA_DATABASE_URL','AGENTSCOPE_SQL_URL')]
 if (urls[0].host,urls[0].port,urls[0].database)==(urls[1].host,urls[1].port,urls[1].database):raise RuntimeError('Nexus 与 AgentScope 不能共享数据库')
 for key in ('ASTRA_DATABASE_URL','AGENTSCOPE_SQL_URL'):
  value=os.environ[key];u=make_url(value)
  if u.drivername not in ('mysql+asyncmy','sqlite+aiosqlite'):raise RuntimeError(key+' 驱动不支持')
  if u.drivername.startswith('sqlite') and (not u.database or not u.database.startswith('/')):raise RuntimeError(key+' SQLite 必须绝对路径')
  engine=create_async_engine(value)
  try:
   async with engine.connect() as connection:
    await connection.execute(text('SELECT 1'))
  finally:await engine.dispose()
 redis.Redis.from_url(os.environ['ASTRA_REDIS_URL'],socket_connect_timeout=5,socket_timeout=5).ping()
asyncio.run(asyncio.wait_for(verify(),20))
from app.config.settings import Settings
Settings()
"""
        while True:
            try:
                self.run(
                    [py, "-c", script],
                    cwd=self.release_root / "services/nexus-api",
                    timeout=30,
                    env=self.env,
                )
                return
            except DeploymentError:
                print(
                    "[配置诊断] 数据库、Redis 或 Settings 校验失败；地址和凭据已隐藏，详细错误见私有日志"
                )
                choice = self.dialog.ask(
                    "保留并停止(k)、修改连接(m)、重新输入全部连接(r)", "k"
                )
                if choice == "k":
                    raise
                keys = ("ASTRA_DATABASE_URL", "AGENTSCOPE_SQL_URL", "ASTRA_REDIS_URL")
                if choice == "m":
                    selected = self.dialog.ask("要修改的配置键（数据库或 Redis）")
                    if selected not in keys:
                        raise DeploymentError("连接配置键无效")
                    keys = (selected,)
                elif choice != "r":
                    raise DeploymentError("未选择连接修正方式")
                for key in keys:
                    self.values[key] = self.dialog.ask(key, secret=True)
                self.values = self.initialize_config()
                self.env.update(self.values)

    def nginx(self):
        self.step("HTTPS 证书与 Nginx 配置验证")
        host = self.values["NEXUS_DEPLOY_PUBLIC_HOST"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
            raise DeploymentError("HTTPS 主机名无效，不能含协议、路径或端口")
        try:
            ipaddress.ip_address(host)
            match_option = "-checkip"
        except ValueError:
            match_option = "-checkhost"

        def valid(cert, key):
            if not cert.is_file() or not key.is_file():
                return False
            checks = [
                ["openssl", "x509", "-in", str(cert), "-noout", "-checkend", "86400"],
                ["openssl", "x509", "-in", str(cert), "-noout", match_option, host],
            ]
            if any(
                subprocess.run(
                    command, capture_output=True, timeout=10, check=False
                ).returncode
                for command in checks
            ):
                return False
            pub = subprocess.run(
                ["openssl", "x509", "-in", str(cert), "-pubkey", "-noout"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            private = subprocess.run(
                ["openssl", "pkey", "-in", str(key), "-pubout", "-passin", "pass:"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            return (
                pub.returncode == private.returncode == 0
                and pub.stdout == private.stdout
            )

        cert, key = (
            Path(self.values["NEXUS_DEPLOY_TLS_CERT"]),
            Path(self.values["NEXUS_DEPLOY_TLS_KEY"]),
        )
        if not cert.exists() and not key.exists():
            # 自动发现现有 Nginx 证书，只有地址、有效期和私钥全部匹配才复用。
            detected = subprocess.run(
                [self.values["NEXUS_DEPLOY_NGINX_BINARY"], "-T"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            certificates = re.findall(
                r"(?m)^\s*ssl_certificate\s+([^;]+);", detected.stdout
            )
            keys = re.findall(
                r"(?m)^\s*ssl_certificate_key\s+([^;]+);", detected.stdout
            )
            for found_cert, found_key in zip(certificates, keys):
                candidate_cert, candidate_key = (
                    Path(found_cert.strip(' "')),
                    Path(found_key.strip(' "')),
                )
                if valid(candidate_cert, candidate_key):
                    cert, key = candidate_cert, candidate_key
                    break
        while not valid(cert, key):
            print(
                "[配置诊断] HTTPS 证书/私钥缺失、过期、地址不匹配或密钥不匹配；不会覆盖已有文件"
            )
            choice = self.dialog.ask(
                "保留并停止(k)、提供证书路径(m)、明确生成自签证书(r)", "k"
            )
            if choice == "m":
                cert = Path(self.dialog.ask("证书完整路径"))
                key = Path(self.dialog.ask("私钥完整路径", secret=True))
            elif choice == "r":
                cert, key = self.state / "tls/server.crt", self.state / "tls/server.key"
                cert.parent.mkdir(parents=True, exist_ok=True)
                temporary_cert, temporary_key = (
                    cert.with_suffix(".candidate.crt"),
                    key.with_suffix(".candidate.key"),
                )
                kind = "IP:" if match_option == "-checkip" else "DNS:"
                self.run(
                    [
                        "openssl",
                        "req",
                        "-x509",
                        "-newkey",
                        "rsa:3072",
                        "-nodes",
                        "-days",
                        "365",
                        "-keyout",
                        str(temporary_key),
                        "-out",
                        str(temporary_cert),
                        "-subj",
                        "/CN=" + host,
                        "-addext",
                        "subjectAltName=" + kind + host,
                    ]
                )
                atomic_write(cert, temporary_cert.read_text())
                atomic_write(key, temporary_key.read_text())
                temporary_cert.unlink()
                temporary_key.unlink()
            else:
                raise DeploymentError("HTTPS 配置尚未修复，部署停止")
        self.values.update(
            NEXUS_DEPLOY_TLS_CERT=str(cert), NEXUS_DEPLOY_TLS_KEY=str(key)
        )
        cache = Path(self.values["NEXUS_DEPLOY_NGINX_CACHE"])
        if not cache.is_absolute() or any(
            path.is_symlink() for path in (cache, *cache.parents)
        ):
            raise DeploymentError("Nginx 缓存目录必须为绝对目录且不能经过符号链接")
        cache.mkdir(parents=True, exist_ok=True)
        conf = nginx_config(self.root, self.state, self.values)
        prefix = self.state / "nginx"
        prefix.mkdir(exist_ok=True)
        (prefix / "logs").mkdir(exist_ok=True)
        # 独立前台 Nginx 无需读取系统配置；所有内容由唯一生成器维护。
        atomic_write(
            prefix / "mime.types",
            "types { text/html html; text/css css; application/javascript js mjs; application/json json map; image/svg+xml svg; image/png png; image/jpeg jpg jpeg; image/x-icon ico; font/woff2 woff2; }\n",
        )
        candidate = self.state / "nginx.conf.candidate"
        atomic_write(candidate, conf)
        self.run(
            [
                self.values["NEXUS_DEPLOY_NGINX_BINARY"],
                "-p",
                str(prefix) + "/",
                "-t",
                "-c",
                str(candidate),
            ]
        )
        atomic_write(self.state / "nginx.conf", conf)
        self.env.update(self.values)

    def lifecycle(self, action):
        if self.args.action != "deploy":
            from configuration import parse_env

            saved = parse_env(self.state / "nexus.env", self.dialog)
            release = saved.get("NEXUS_DEPLOY_RELEASE", "")
            if not re.fullmatch(r"[0-9a-f]{40}", release):
                raise DeploymentError("缺少有效发布记录，请先执行部署")
            self.release_root = self.state / "releases" / release / "repo"
        return self.run(
            [
                str(self.release_root / "services/nexus-api/.venv/bin/python"),
                str(self.root / "ops/deploy/lifecycle.py"),
                action,
                "--root",
                str(self.release_root),
                "--state",
                str(self.state),
            ],
            timeout=900,
        )

    def publish(self):
        self.step("发布运行配置并检查端口归属")
        self.env.update(self.values)
        atomic_write(self.state / "sources.json", self.pending_sources)
        save_env(self.state / "nexus.env", self.values)
        while True:
            try:
                self.lifecycle("preflight")
                break
            except DeploymentError:
                choice = self.dialog.ask(
                    "进程/端口检查失败：保留并停止(k)、修改一个端口(m)、重新分配外部入口端口(r)",
                    "k",
                )
                port_keys = (
                    "NEXUS_API_PORT",
                    "AGENTSCOPE_PORT",
                    "ASTRA_SERVICE_GATEWAY_PORT",
                    "NEXUS_MCP_PORT",
                    "NEXUS_TASK_MCP_PORT",
                    "NEXUS_SCHEDULE_MCP_PORT",
                    "NEXUS_DEPLOY_HTTPS_PORT",
                    "NEXUS_DEPLOY_WORKER_PORT",
                )
                if choice == "m":
                    key = self.dialog.ask("要修改的端口键：" + ",".join(port_keys))
                    if key not in port_keys:
                        raise DeploymentError("端口配置键无效")
                    self.values[key] = self.dialog.ask(key)
                elif choice == "r":
                    import socket

                    for key in ("NEXUS_DEPLOY_HTTPS_PORT", "NEXUS_DEPLOY_WORKER_PORT"):
                        with socket.socket() as probe:
                            probe.bind((self.values["NEXUS_DEPLOY_PUBLIC_IP"], 0))
                            self.values[key] = str(probe.getsockname()[1])
                else:
                    raise
                self.values = self.initialize_config()
                self.env.update(self.values)
                self.nginx()
                save_env(self.state / "nexus.env", self.values)
        self.step("停止旧进程并执行正式数据库迁移")
        self.lifecycle("stop")
        py = str(self.release_root / "services/nexus-api/.venv/bin/python")
        self.run(
            [py, "-m", "alembic", "upgrade", "head"],
            cwd=self.release_root / "services/nexus-api",
            env=self.env,
        )
        self.run(
            [
                str(
                    self.release_root
                    / "services/agent-service/.venv-agentscope/bin/python"
                ),
                "agentscope_sql.py",
                "init",
            ],
            cwd=self.release_root / "services/agent-service",
            env=self.env,
        )
        self.run(
            [py, "-m", "app.deployment", "gateway-key"],
            cwd=self.release_root / "services/nexus-api",
            env=self.env,
        )
        if self.values.get("ASTRA_LLM_WIKI_ENABLED", "false").lower() == "true":
            self.run(
                [py, "-m", "app.deployment", "wiki"],
                cwd=self.release_root / "services/nexus-api",
                env=self.env,
            )
        self.step("启动全部服务并验证实际发布版本")
        self.lifecycle("start")
        self.step("通过真实 AgentScope 接口验证内置 MCP 注册与连接")
        self.run(
            [py, "-m", "app.deployment", "mcp"],
            cwd=self.release_root / "services/nexus-api",
            env=self.env,
        )

    def external_health(self):
        self.step("Nginx HTTPS 与 Worker HTTP 入口验收")
        ip = self.values["NEXUS_DEPLOY_PUBLIC_IP"]
        base = f"https://{self.values['NEXUS_DEPLOY_PUBLIC_HOST']}:{self.values['NEXUS_DEPLOY_HTTPS_PORT']}"
        # 以已配置证书建立信任，仍验证证书地址，不禁用 TLS。
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=self.values["NEXUS_DEPLOY_TLS_CERT"])
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
        )
        try:
            for path in ("/", "/api/health"):
                with opener.open(base + path, timeout=10) as response:
                    if response.status != 200:
                        raise DeploymentError("HTTPS 入口健康检查失败")
            worker = f"http://{ip}:{self.values['NEXUS_DEPLOY_WORKER_PORT']}"
            for path, expected in (("/api/agent-workers", 404), ("/v1/models", 401)):
                try:
                    response = opener.open(worker + path, timeout=10)
                    status = response.status
                    response.close()
                except urllib.error.HTTPError as exc:
                    status = exc.code
                if status != expected:
                    raise DeploymentError(
                        f"Worker HTTP 入口 {path} 边界验收失败，状态 {status}"
                    )
            request = urllib.request.Request(
                worker + "/v1/models",
                headers={
                    "Authorization": "Bearer " + self.values["NEXUS_GATEWAY_API_KEY"]
                },
            )
            with opener.open(request, timeout=15) as response:
                if not isinstance(json.load(response).get("data"), list):
                    raise DeploymentError("Worker 模型网关认证验收失败")
        except (OSError, ValueError) as exc:
            raise DeploymentError(
                "入口访问或 TLS 地址验证失败：" + type(exc).__name__
            ) from None
        atomic_write(
            self.state / "installed.json",
            json.dumps(
                {
                    "release": self.values["NEXUS_DEPLOY_RELEASE"],
                    "version": "1.4.9",
                    "verified_at": time.time(),
                }
            )
            + "\n",
        )
        print("[成功] 全部核心服务及入口验收通过；Web: " + base)

    def execute(self):
        self.preflight()
        self.checkout()
        self.configure()
        self.dependencies()
        self.install()
        self.env.update(self.values)
        # 还未发布的运行文件不能被 Settings 从 checkout 开发配置覆盖。
        candidate = self.state / "nexus.env.candidate"
        save_env(candidate, self.values)
        self.env["NEXUS_RUNTIME_ENV_FILE"] = str(candidate)
        self.validate_connections()
        self.build_web()
        self.nginx()
        self.publish()
        self.external_health()


def main():
    parser = argparse.ArgumentParser(
        description="Nexus·数字空间 V1.4.9 首次安装/升级统一入口"
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--state",
        type=Path,
        default=Path(os.environ.get("NEXUS_STATE_DIR", "/var/lib/nexus")),
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument(
        "--sources", type=Path, help="仅来源配置，不选择开发/生产模式；后续自动复用"
    )
    parser.add_argument(
        "--action", choices=("deploy", "health", "stop"), default="deploy"
    )
    args = parser.parse_args()
    try:
        deployer = Deployer(args)
    except OSError:
        print(
            "[失败阶段：初始化] 无法创建部署状态目录，请检查权限并使用 root 执行",
            file=sys.stderr,
        )
        return 1
    try:
        with (deployer.state / "deploy.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DeploymentError("已有部署/维护操作正在运行") from None
            if args.action == "deploy":
                deployer.execute()
            else:
                deployer.lifecycle(args.action)
    except (DeploymentError, OSError, ValueError) as exc:
        print(f"[失败阶段：{deployer.stage}] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
