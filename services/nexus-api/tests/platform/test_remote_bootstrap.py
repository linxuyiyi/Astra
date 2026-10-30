"""Server-owned target probing, dependency decisions and Python reuse/build."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config.settings import Settings
from app.config.worker_deployment import PYTHON_SHA256, deployment_manifest
from app.runtime.remote_bootstrap import dependency_packages, managed_python, prepare_python


class BootstrapWorker:
    def __init__(self, *, ready="", explicit="", uid="0", busy=False, failure=False):
        self.settings = Settings(_env_file=None, remote_python=explicit)
        self.environment, self.operation_id = SimpleNamespace(id=7), "bootstrap"
        self.ready, self.uid, self.busy, self.failure = ready, uid, busy, failure
        self.commands, self.steps, self.python_interpreter = [], [], None
        self._acquire_install_lock = AsyncMock()

    def _agent_interpreter(self):
        return self.python_interpreter or self.settings.remote_python or managed_python(7)

    async def _ensure_safe(self):
        return None

    async def _bootstrap_idle(self, *, interpreter_ready):
        if self.busy:
            raise RuntimeError("native busy")

    async def _command(self, command, timeout=30):
        self.commands.append(command)
        values = {
            "uname -s": "Linux",
            "uname -m": "x86_64",
            "id -u": self.uid,
            'printf "%s" "$HOME"': "/root",
            "getconf GNU_LIBC_VERSION 2>/dev/null || true": "glibc 2.31",
        }
        if command in values:
            return values[command]
        if command.startswith("command -v"):
            tool = command.split()[2]
            return (
                "" if tool in {"dnf", "yum", "python3.13", "python3.12", "python3.11"} else "/usr/bin/" + tool
            )
        if command.startswith("if "):
            return "READY" if self.ready and self.ready in command else "MISSING"
        if command.startswith("sha256sum"):
            return PYTHON_SHA256 + " archive"
        if self.failure and "./configure" in command:
            raise RuntimeError("build failed")
        return ""

    async def step(self, emit, name, state, detail=""):
        self.steps.append((name, state))


async def test_missing_python_build_is_selected_by_server_from_explicit_source():
    worker = BootstrapWorker()
    await prepare_python(worker, None)
    assert any(
        "https://mirrors.tools.huawei.com/python/3.11.17/Python-3.11.17.tar.xz" in c for c in worker.commands
    )
    assert any("./configure" in c for c in worker.commands)
    assert worker.python_interpreter == managed_python(7)
    assert worker.steps[-1] == ("Python 运行环境", "passed")
    worker._acquire_install_lock.assert_awaited_once()


async def test_preinstalled_python_is_reused_without_compilation_or_dev_packages():
    worker = BootstrapWorker(ready="/usr/bin/python3")
    await prepare_python(worker, None)
    assert worker.python_interpreter == "/usr/bin/python3"
    assert not any("./configure" in c or "apt-get install" in c for c in worker.commands)


async def test_explicit_bad_python_does_not_fallback():
    worker = BootstrapWorker(explicit="/custom/python3")
    with pytest.raises(RuntimeError, match="指定的远端 Python 不可用"):
        await prepare_python(worker, None)
    assert not any("apt-get install" in c or c.startswith("curl ") for c in worker.commands)


async def test_native_busy_blocks_before_package_installation():
    worker = BootstrapWorker(busy=True)
    with pytest.raises(RuntimeError, match="native busy"):
        await prepare_python(worker, None)
    assert not any("apt-get install" in c or "./configure" in c for c in worker.commands)


async def test_non_root_is_rejected_before_dependency_or_workspace_changes():
    worker = BootstrapWorker(uid="1000")
    with pytest.raises(RuntimeError, match="需要 root"):
        await prepare_python(worker, None)
    assert not any("apt-get" in c or "mkdir" in c for c in worker.commands)


async def test_build_failure_never_reports_success_and_cleans_its_own_stage():
    worker = BootstrapWorker(failure=True)
    with pytest.raises(RuntimeError, match="build failed"):
        await prepare_python(worker, None)
    assert ("Python 运行环境", "passed") not in worker.steps
    assert any("rm -rf --" in c and ".build-bootstrap" in c for c in worker.commands)


def test_sources_are_independent_and_legacy_root_is_explicitly_rejected():
    settings = Settings(_env_file=None)
    manifest = deployment_manifest(settings)
    assert manifest["npm_registry"] == "https://mirrors.tools.huawei.com/npm"
    assert manifest["pip_index_url"] == "https://mirrors.tools.huawei.com/pypi/simple"
    with pytest.raises(ValueError, match="已停用"):
        deployment_manifest(Settings(_env_file=None, remote_download_base_url="https://old.invalid"))
    assert "build-essential" not in dependency_packages("apt-get", build_python=False, git_missing=False)
    assert "git" not in dependency_packages("dnf", build_python=True, git_missing=False)
