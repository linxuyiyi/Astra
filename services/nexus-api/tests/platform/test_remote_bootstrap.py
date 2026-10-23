"""Company-network bootstrap boundaries and real shell failure behavior."""

import shlex
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config.settings import Settings
from app.runtime.remote_bootstrap import (
    dependency_script,
    download_base,
    managed_python,
    prepare_python,
    python_build_script,
)
from app.runtime.remote_provisioning import RemoteProvisioner, agent_python


class BootstrapWorker:
    def __init__(self, *, ready=False, explicit="", canceled_at=0, build_error=False, busy=False):
        self.settings = SimpleNamespace(
            remote_python=explicit,
            remote_download_base_url="https://repo.huaweicloud.com",
            remote_deploy_timeout_seconds=1200,
        )
        self.ready = ready
        self.commands = []
        self.steps = []
        self.checks = 0
        self.canceled_at = canceled_at
        self.build_error = build_error
        self.busy = busy

    def _agent_interpreter(self):
        return self.settings.remote_python or managed_python(7)

    async def _ensure_safe(self):
        self.checks += 1
        if self.checks == self.canceled_at:
            raise RuntimeError("lease lost")

    async def _bootstrap_idle(self, *, interpreter_ready):
        if self.busy:
            raise RuntimeError("native busy")

    async def _command(self, command, timeout=30):
        self.commands.append(command)
        if command.startswith("if "):
            return "READY" if self.ready else "MISSING"
        if self.build_error and "./configure" in command:
            raise RuntimeError("checksum failed")
        return ""

    async def step(self, emit, name, state, detail=""):
        self.steps.append((name, state))


@pytest.mark.asyncio
async def test_missing_system_python_uses_private_bootstrap():
    worker = BootstrapWorker()
    await prepare_python(worker, None)
    assert len(worker.commands) == 3
    assert managed_python(7) in worker.commands[0]
    assert "python3 -c" not in "\n".join(worker.commands)
    assert "https://repo.huaweicloud.com/python/" in worker.commands[-1]
    assert worker.steps[-1] == ("Python 运行环境", "passed")


@pytest.mark.asyncio
async def test_redeploy_does_not_build_available_managed_python():
    worker = BootstrapWorker(ready=True)
    await prepare_python(worker, None)
    assert len(worker.commands) == 2
    assert not any("./configure" in c for c in worker.commands)


@pytest.mark.asyncio
async def test_explicit_bad_python_fails_before_dependency_or_configuration_writes():
    worker = BootstrapWorker(explicit="/custom/python3")
    with pytest.raises(RuntimeError, match="指定的远端 Python 不可用"):
        await prepare_python(worker, None)
    assert len(worker.commands) == 1


@pytest.mark.asyncio
async def test_lease_loss_stops_before_build():
    worker = BootstrapWorker(canceled_at=3)
    with pytest.raises(RuntimeError, match="lease lost"):
        await prepare_python(worker, None)
    assert len(worker.commands) == 2
    assert not any("./configure" in c for c in worker.commands)


@pytest.mark.asyncio
async def test_build_failure_is_not_reported_as_success():
    worker = BootstrapWorker(build_error=True)
    with pytest.raises(RuntimeError, match="checksum failed"):
        await prepare_python(worker, None)
    assert ("Python 运行环境", "passed") not in worker.steps


@pytest.mark.asyncio
async def test_native_busy_blocks_before_any_package_install():
    worker = BootstrapWorker(busy=True)
    with pytest.raises(RuntimeError, match="native busy"):
        await prepare_python(worker, None)
    assert len(worker.commands) == 1


@pytest.mark.asyncio
async def test_existing_agent_python_checks_native_state_before_bootstrap():
    worker = RemoteProvisioner(
        SimpleNamespace(id=7, ssh_password="", model_name="", opencode_path=""),
        {"id": "op", "action": "deploy"},
    )
    probes = []

    async def command(*_args):
        return "yes"

    async def native_idle(**kwargs):
        probes.append(kwargs)

    worker._command = command
    worker._native_idle = native_idle
    await worker._bootstrap_idle(interpreter_ready=False)
    assert probes == [{"strict": True, "interpreter": agent_python(7)}]


def test_mirrors_default_to_huawei():
    settings = Settings(_env_file=None)
    assert download_base(settings) == "https://repo.huaweicloud.com"
    assert settings.remote_npm_registry == "https://repo.huaweicloud.com/repository/npm/"
    assert settings.remote_pip_index_url == "https://repo.huaweicloud.com/repository/pypi/simple"
    source = Path("app/runtime/remote_install.sh").read_text()
    assert 'node_source="$download_base/nodejs"' in source
    assert 'node_dist=linux-x64-musl' in source
    for external in ("nodejs.org", "kernel.org", "dl-cdn.alpinelinux.org", "vault.centos.org"):
        assert external not in source
    assert "https://repo.huaweicloud.com/centos-vault" in dependency_script(download_base(settings))


@pytest.mark.parametrize("base", ["http://mirror.test", "https://user:secret@mirror.test", "https://x/?k=v"])
def test_invalid_download_base_is_rejected(base):
    with pytest.raises(RuntimeError, match="HTTPS"):
        download_base(SimpleNamespace(remote_download_base_url=base))


def test_checksum_failure_does_not_extract_or_leave_partial_python(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # Real sha256sum rejects this bogus payload before configure/build can run.
    (bindir / "curl").write_text('#!/bin/sh\nfor arg; do target="$arg"; done\nprintf bad >"$target"\n')
    (bindir / "curl").chmod(0o755)
    interpreter = str(tmp_path / "python" / "bin" / "python3.11")
    source = python_build_script(interpreter, "https://repo.huaweicloud.com")
    # macOS lacks flock: a no-op for this non-concurrent checksum test only.
    result = subprocess.run(
        ["bash", "-euc", f"export PATH={shlex.quote(str(bindir))}:$PATH\nflock() {{ :; }}\n" + source],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert "FAILED" in result.stdout + result.stderr
    assert not (tmp_path / "python").exists()
    assert not list(tmp_path.glob(".python-build.*"))


@pytest.mark.parametrize("version,accepted", [("2.17.9", False), ("1.19.0", False),
                                               ("2.18.0", True), ("2.19.1", True), ("2.43.7", True)])
def test_git_floor_uses_existing_git_without_installing(version, accepted):
    source = Path("app/runtime/remote_install.sh").read_text()
    functions = source.split("# >>> nexus-installer-functions\n")[1].split(
        "# <<< nexus-installer-functions"
    )[0]
    result = subprocess.run(
        ["bash", "-euc", "fail() { echo \"$*\" >&2; exit 1; }\n"
         + f"git() {{ printf 'git version {version}\\n'; }}\n" + functions + "\nassert_git_version"],
        capture_output=True, text=True, timeout=10,
    )
    assert (result.returncode == 0) is accepted
    if not accepted:
        assert "Git 版本低于 2.18" in result.stderr
    assert "kernel.org" not in source
    assert "git_release=" not in source


def test_all_ssh_probes_select_managed_or_installed_agent_python():
    worker = RemoteProvisioner(
        SimpleNamespace(id=7, ssh_password="", model_name="", opencode_path=""),
        {"id": "op", "action": "deploy"},
    )
    worker.settings = SimpleNamespace(remote_python="")
    assert worker._probe_interpreter() == managed_python(7)
    worker.action = "restart"
    assert worker._probe_interpreter() == agent_python(7)


@pytest.mark.skipif(sys.platform != "linux", reason="前置监听检查使用 Linux procfs")
async def test_bootstrap_without_python_blocks_real_listener_before_packages():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        worker = RemoteProvisioner(
            SimpleNamespace(id=987654321, ssh_password="", model_name="", opencode_path="",
                            opencode_server_port=listener.getsockname()[1]),
            {"id": "op", "action": "deploy"},
        )

        async def command(text, timeout=30):
            result = subprocess.run(["bash", "-c", text], capture_output=True, text=True, timeout=timeout)
            assert result.returncode == 0, result.stderr
            return result.stdout.strip()

        worker._command = command
        with pytest.raises(RuntimeError, match="停止环境引导"):
            await worker._bootstrap_idle(interpreter_ready=False)
    await worker._bootstrap_idle(interpreter_ready=False)
