"""Nexus-side environment probes, dependency decisions and Python installation."""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath

from app.config.worker_deployment import PYTHON_SHA256, PYTHON_VERSION, deployment_manifest
from app.runtime.remote_artifacts import download


def managed_python(environment_id: int) -> str:
    return f"/opt/nexus/workers/{environment_id}/python-{PYTHON_VERSION}/bin/python3.11"


def python_probe(interpreter: str) -> str:
    source = (
        "import sys,ssl,venv,ensurepip,sqlite3,ctypes,lzma,tarfile; "
        "sys.exit(0 if sys.version_info >= (3,11) and hasattr(tarfile,'data_filter') else 1)"
    )
    return f"{shlex.quote(interpreter)} -I -c {shlex.quote(source)}"


async def probe_environment(worker) -> dict:
    probes = {
        "os": "uname -s",
        "arch": "uname -m",
        "uid": "id -u",
        "home": 'printf "%s" "$HOME"',
        "glibc": "getconf GNU_LIBC_VERSION 2>/dev/null || true",
        "ldd": "ldd --version 2>&1 | head -1 || true",
    }
    facts = {}
    for name, command in probes.items():
        facts[name] = await worker._command(command)
    if facts["os"] != "Linux" or facts["arch"] not in {"x86_64", "aarch64"}:
        raise RuntimeError("SSH 一键部署只支持 Linux x86_64/aarch64")
    if facts["uid"] != "0":
        raise RuntimeError("SSH 一键部署需要 root，无自动 sudo 升权")
    if not facts["home"].startswith("/") or "\n" in facts["home"]:
        raise RuntimeError("无法确定远端用户 HOME")
    tools = (
        "timeout",
        "curl",
        "tar",
        "xz",
        "git",
        "flock",
        "sha256sum",
        "sha512sum",
        "node",
        "npm",
        "apt-get",
        "dnf",
        "yum",
        "python3",
        "python3.13",
        "python3.12",
        "python3.11",
    )
    facts["tools"] = {}
    for name in tools:
        facts["tools"][name] = await worker._command("command -v " + shlex.quote(name) + " || true")
    if not facts["tools"]["timeout"]:
        raise RuntimeError("缺少 timeout，请管理员安装 coreutils")
    libc = facts["glibc"].split()
    facts["libc"] = "glibc" if libc[:1] == ["glibc"] else ("musl" if "musl" in facts["ldd"] else "unknown")
    if not worker.settings.remote_node_binary:
        if facts["libc"] != "glibc" or tuple(map(int, libc[-1].split(".")[:2])) < (2, 28):
            raise RuntimeError(
                "目标 libc 不支持受管 Node；请配置可运行的 NEXUS_REMOTE_NODE_BINARY，不改造系统库"
            )
    worker.remote_facts, worker.home = facts, facts["home"]
    return facts


def dependency_packages(manager: str, *, build_python: bool, git_missing: bool) -> list[str]:
    packages = ["ca-certificates", "curl", "tar", "util-linux", "coreutils"]
    packages.append("xz-utils" if manager == "apt-get" else "xz")
    if git_missing:
        packages.append("git")
    if build_python:
        packages += (
            "build-essential libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev "
            "libffi-dev liblzma-dev uuid-dev"
            if manager == "apt-get"
            else "gcc gcc-c++ make openssl-devel zlib-devel bzip2-devel readline-devel sqlite-devel "
            "libffi-devel xz-devel libuuid-devel"
        ).split()
    return packages


async def prepare_dependencies(worker, facts: dict, *, build_python: bool) -> None:
    required = ("curl", "tar", "xz", "git", "flock", "sha256sum", "sha512sum")
    if not build_python and all(facts["tools"][name] for name in required):
        return
    manager = next((name for name in ("apt-get", "dnf", "yum") if facts["tools"][name]), None)
    if manager is None:
        raise RuntimeError("系统依赖不完整且无 apt/dnf/yum；请管理员准备依赖和可用 Python")
    await worker._ensure_safe()
    if manager == "apt-get":
        await worker._command("timeout -k 10 120 apt-get update -qq", timeout=135)
    packages = dependency_packages(manager, build_python=build_python, git_missing=not facts["tools"]["git"])
    args = (
        ["apt-get", "install", "-y", "-qq", "--no-upgrade"]
        if manager == "apt-get"
        else [manager, "-y", "-q", "install"]
    )
    await worker._command(
        "DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l timeout -k 10 300 " + shlex.join(args + packages),
        timeout=315,
    )


async def prepare_python(worker, emit) -> None:
    manifest = deployment_manifest(worker.settings)
    await worker._ensure_safe()
    await worker.step(emit, "系统环境检查", "running")
    facts = await probe_environment(worker)
    await worker.step(emit, "系统环境检查", "passed", facts["os"] + " " + facts["arch"])
    await worker.step(emit, "Python 运行环境", "running")
    explicit = worker.settings.remote_python.strip()
    candidates = (
        [explicit]
        if explicit
        else [managed_python(worker.environment.id)]
        + [
            facts["tools"][name]
            for name in ("python3", "python3.13", "python3.12", "python3.11")
            if facts["tools"][name]
        ]
    )
    selected = None
    for candidate in dict.fromkeys(candidates):
        await worker._ensure_safe()
        ready = await worker._command(
            f"if {python_probe(candidate)} >/dev/null 2>&1; then printf READY; else printf MISSING; fi"
        )
        if ready == "READY":
            selected = candidate
            break
    if explicit and selected is None:
        raise RuntimeError("指定的远端 Python 不可用：需要 Python ≥3.11 及完整 SSL/venv/ensurepip/标准库")
    if selected:
        worker.python_interpreter = selected
    await worker._bootstrap_idle(interpreter_ready=selected is not None)
    await worker.step(emit, "系统依赖", "running")
    await prepare_dependencies(worker, facts, build_python=selected is None)
    await worker.step(emit, "系统依赖", "passed")
    await worker._acquire_install_lock()
    if selected is None:
        interpreter = managed_python(worker.environment.id)
        prefix = str(PurePosixPath(interpreter).parent.parent)
        exists = await worker._command(f"test -e {shlex.quote(prefix)} && printf yes || true")
        if exists:
            raise RuntimeError("受管 Python 目录已存在但不可用，保留现场，请管理员核对")
        stage = prefix + ".build-" + worker.operation_id
        await worker._command(shlex.join(["mkdir", "-p", stage]))
        complete = False
        try:
            archive = stage + "/python.tar.xz"
            await download(worker, manifest["python_source_url"], archive)
            digest = (await worker._command(shlex.join(["sha256sum", archive]))).split()[0]
            if digest != PYTHON_SHA256:
                raise RuntimeError("Python 源码 SHA256 校验失败")
            await worker._command(shlex.join(["tar", "-xJf", archive, "-C", stage, "--strip-components=1"]))
            for name, args in [
                ("configure", ["./configure", "--prefix=" + prefix, "--with-ensurepip=install"]),
                ("make", ["make", "-s", "-j2"]),
                ("install", ["make", "-s", "altinstall"]),
            ]:
                await worker._ensure_safe()
                log = stage + "/" + name + ".log"
                command = (
                    "cd "
                    + shlex.quote(stage)
                    + " && "
                    + shlex.join(args)
                    + " >"
                    + shlex.quote(log)
                    + " 2>&1 || { tail -n 20 "
                    + shlex.quote(log)
                    + "; exit 1; }"
                )
                seconds = worker.settings.remote_deploy_timeout_seconds
                await worker._command(
                    "timeout -k 10 " + str(seconds) + " bash -euc " + shlex.quote(command),
                    timeout=seconds + 15,
                )
            await worker._command(python_probe(interpreter))
            worker.python_interpreter = selected = interpreter
            complete = True
        finally:
            await worker._command(shlex.join(["rm", "-rf", "--", stage]))
            if not complete:
                await worker._command(shlex.join(["rm", "-rf", "--", prefix]))
    await worker._ensure_safe()
    await worker.step(emit, "Python 运行环境", "passed", selected)
