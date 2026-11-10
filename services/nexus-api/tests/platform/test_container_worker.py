"""无 systemd 容器与 Linux 节点部署契约：ctl.sh / start-all.sh / flock 守护与凭据隔离。"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.runtime import remote_provisioning
from app.runtime.remote_provisioning import (
    START_ALL_PATH,
    RemoteProvisioner,
    worker_ctl_path,
    worker_state_dir,
)
from app.worker_tools.inspection import _managed_by
from app.worker_tools.process_env import child_environ


def test_config_environment_is_preserved_without_worker_credentials():
    result = child_environ(
        {
            "PATH": "/bin",
            "OPENCODE_CONFIG": "/etc/nexus/opencode.jsonc",
            "OPENCODE_CONFIG_DIR": "/etc/nexus",
            "NEXUS_WORKER_TOKEN": "canary",
            "NEXUS_WORKER_GATEWAY_URL": "https://worker.test",
            "SSL_CERT_FILE": "/etc/ca.pem",
        },
        env_id="71",
    )
    assert result["OPENCODE_CONFIG"] == "/etc/nexus/opencode.jsonc"
    assert result["OPENCODE_CONFIG_DIR"] == "/etc/nexus"
    assert result["SSL_CERT_FILE"] == "/etc/ca.pem"
    assert result["NEXUS_AGENT_WORKER_ID"] == "71"
    assert "NEXUS_WORKER_TOKEN" not in result
    assert "NEXUS_WORKER_GATEWAY_URL" not in result


def _provisioner(monkeypatch):
    environment = SimpleNamespace(
        id="71",
        mode="LINUX_REMOTE",
        host="10.0.0.8",
        port=22,
        username="root",
        ssh_password="ssh-canary",
        workspace_root="/srv/container workspace",
        model_name="nexus-gateway/test",
        repository_url=None,
        skill_refs=[],
        mcp_refs=[],
    )
    worker = RemoteProvisioner(environment, {"id": "maintenance", "action": "deploy"})
    worker.opencode_port = 14096
    worker.home = "/root"
    worker.config_path = "/etc/nexus/opencode/71/opencode.jsonc"
    worker.target_binary = "/managed/bin/opencode"
    worker.git_credential_revision = "cred-rev-71"
    commands, files = [], {}
    wheel = b"worker-package"
    digest = hashlib.sha256(wheel).hexdigest()
    monkeypatch.setattr(remote_provisioning, "worker_tools_wheel", lambda: ("worker.whl", wheel))
    monkeypatch.setattr(worker.settings, "gateway_openai_base", "http://gateway.invalid:8110")
    monkeypatch.setattr(worker.settings, "mcp_resource_server_url", "http://mcp.invalid:8201/mcp")

    async def command(text, timeout=30):
        commands.append(text)
        if "wheel.sha256" in text:
            return digest
        if "test -x" in text:
            return "yes"
        return ""

    async def write(path, data, mode, *, backup=False):
        changed = files.get(path) != (data, mode)
        files[path] = (data, mode)
        return changed

    monkeypatch.setattr(worker, "_command", command)
    monkeypatch.setattr(worker, "_write_atomic_if_changed", write)
    monkeypatch.setattr(worker, "_ensure_safe", AsyncMock())
    monkeypatch.setattr(worker, "_native_idle", AsyncMock(return_value=True))
    return worker, commands, files


@pytest.mark.asyncio
async def test_install_writes_ctl_and_start_all_without_network_agent(monkeypatch):
    worker, commands, files = _provisioner(monkeypatch)
    await worker._install_worker(AsyncMock())

    ctl_file = worker_ctl_path(71)
    assert ctl_file in files
    assert START_ALL_PATH in files
    assert "/etc/nexus/worker-71.env" in files
    assert not any("systemctl" in cmd for cmd in commands)
    assert not any("systemd" in path for path in files)
    assert not any("opencode-71.env" in path for path in files)

    env_bytes = files["/etc/nexus/worker-71.env"][0]
    assert b"NEXUS_WORKER_TOKEN" not in env_bytes
    assert b"NEXUS_WORKER_GATEWAY_URL" not in env_bytes
    assert b"NEXUS_WORKER_OPENCODE_URL=http://127.0.0.1:14096" in env_bytes
    assert f"NEXUS_WORKER_STATE_DIR={worker_state_dir(71)}".encode() in env_bytes
    assert b"NEXUS_GIT_CREDENTIAL_REVISION=cred-rev-71" in env_bytes

    ctl_script = files[ctl_file][0].decode("utf-8")
    assert "fcntl.flock" in ctl_script
    assert "app.worker_tools" in ctl_script
    assert any(f"{ctl_file}" in cmd and "restart" in cmd for cmd in commands)

    commands.clear()
    await worker._install_worker(AsyncMock())
    assert any(f"{ctl_file}" in cmd and "start" in cmd for cmd in commands)


def test_health_reports_supervisor_managed_by(monkeypatch):
    monkeypatch.delenv("NEXUS_WORKER_STATE_DIR", raising=False)
    assert _managed_by() == "external"
    monkeypatch.setenv("NEXUS_WORKER_STATE_DIR", "/opt/nexus/workers/71/state")
    assert _managed_by() == "supervisor"
