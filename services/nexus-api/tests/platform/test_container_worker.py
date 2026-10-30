"""No-systemd process deployment and identity-protection contracts."""

import json
from unittest.mock import AsyncMock

import pytest

from app.worker_agent import channel_switch, process_manager
from app.worker_agent.process_env import child_environ


def test_config_environment_is_preserved_without_worker_credentials():
    result = child_environ(
        {
            "PATH": "/bin",
            "OPENCODE_CONFIG": "/etc/nexus/opencode.jsonc",
            "OPENCODE_CONFIG_DIR": "/etc/nexus",
            "NEXUS_WORKER_TOKEN": "canary",
            "NEXUS_WORKER_GATEWAY_URL": "https://worker.test",
            "SSL_CERT_FILE": "/etc/ca.pem",
        }
    )
    assert result["OPENCODE_CONFIG"] == "/etc/nexus/opencode.jsonc"
    assert result["OPENCODE_CONFIG_DIR"] == "/etc/nexus"
    assert result["SSL_CERT_FILE"] == "/etc/ca.pem"
    assert not any(key.startswith("NEXUS_") for key in result)


def test_stale_process_record_never_signals(tmp_path, monkeypatch):
    record = tmp_path / "process.json"
    identity = {"pid": 123, "starttime": "old", "cwd": "/work", "argv": ["worker"], "pgid": 123}
    record.write_text(json.dumps({"identity": identity, "command": ["worker"]}))
    monkeypatch.setattr(process_manager, "process_identity", lambda _pid: {**identity, "starttime": "new"})
    monkeypatch.setattr(process_manager.os, "killpg", lambda *_a: pytest.fail("不能向复用的 PID 发信号"))
    with pytest.raises(RuntimeError, match="身份已变化"):
        process_manager._stop(record)
    assert record.exists()


def _provisioner(monkeypatch):
    import hashlib
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.runtime import remote_provisioning
    from app.runtime.remote_provisioning import RemoteProvisioner

    environment = SimpleNamespace(
        id=71,
        employee_id="worker-container",
        ssh_password="ssh-canary",
        opencode_server_port=14096,
        workspace_root="/srv/container workspace",
        model_name="nexus-gateway/test",
        opencode_path="/managed/bin/opencode",
    )
    worker = RemoteProvisioner(environment, {"id": "maintenance", "action": "deploy"})
    worker.home = "/root"
    worker.config_path = "/etc/nexus/opencode/71/opencode.jsonc"
    commands, files = [], {}
    wheel = b"worker-package"
    digest = hashlib.sha256(wheel).hexdigest()
    monkeypatch.setattr(remote_provisioning, "agent_wheel", lambda: ("worker.whl", wheel))
    monkeypatch.setattr(worker.settings, "remote_gateway_local_url", "http://gateway.invalid:8110")
    monkeypatch.setattr(worker.settings, "mcp_resource_server_url", "http://mcp.invalid:8201/mcp")
    monkeypatch.setattr(worker.settings, "worker_agent_gateway_url", "ws://gateway.invalid/connect")

    async def command(text, timeout=30):
        commands.append(text)
        if "/proc/1/comm" in text:
            return "process"
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
    monkeypatch.setattr(
        worker,
        "_existing_worker_credentials",
        AsyncMock(return_value=("worker-token-canary", "worker-instance")),
    )
    worker._manage_process = AsyncMock()
    worker._wait_opencode_ready = AsyncMock()
    return worker, commands, files


def test_container_managed_listener_uses_record_and_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(channel_switch, "assert_native_idle", lambda *a, **kw: True)
    monkeypatch.setattr(channel_switch, "managed_running", lambda *_a: True)
    manager = []
    monkeypatch.setattr(channel_switch, "manage_process", lambda *a, **kw: manager.append((a, kw)))
    monkeypatch.setattr(channel_switch, "_wait_until_healthy", lambda *a, **kw: None)

    def gate():
        return None

    env = {
        "NEXUS_WORKER_OPENCODE_UNIT": "legacy.service",
        "NEXUS_WORKER_OPENCODE_PROCESS_RECORD": str(tmp_path / "process.json"),
        "NEXUS_WORKER_OPENCODE_BIN": "/managed/opencode",
    }
    channel_switch.restart_local_opencode(str(tmp_path), 14096, environ=env, gate=gate)
    assert len(manager) == 1
    assert manager[0][1]["restart"] is True
    assert manager[0][1]["gate"] is gate
    assert manager[0][1]["command"][0] == "/managed/opencode"


def test_container_conflict_port_never_signals_or_starts(tmp_path, monkeypatch):
    record = tmp_path / "process.json"
    identity = {"pid": 123, "pgid": 123, "cwd": str(tmp_path), "argv": ["worker"], "starttime": "1"}
    record.write_text(json.dumps({"identity": identity, "command": ["worker"]}))
    monkeypatch.setattr(process_manager, "process_identity", lambda _pid: identity)
    monkeypatch.setattr(channel_switch, "_probe_listener", lambda _port: True)
    monkeypatch.setattr(channel_switch, "_listener_pid", lambda _port: 456)
    monkeypatch.setattr(process_manager.os, "getpgid", lambda _pid: 456)
    monkeypatch.setattr(process_manager.os, "killpg", lambda *_a: pytest.fail("冲突端口不能杀进程"))
    monkeypatch.setattr(process_manager.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("不能重复启动"))
    with pytest.raises(RuntimeError, match="端口不属于受管进程"):
        process_manager.manage_process(
            record,
            command=["opencode", "serve", "--port", "14096"],
            workspace=str(tmp_path),
            log_path=tmp_path / "log",
            environ={},
            restart=True,
            opencode=True,
        )
    assert json.loads(record.read_text())["identity"] == identity


def test_managed_process_rechecks_native_idle_before_signaling(tmp_path, monkeypatch):
    record = tmp_path / "process.json"
    identity = {"pid": 123, "pgid": 123, "cwd": str(tmp_path), "argv": ["worker"], "starttime": "1"}
    command = ["opencode", "serve", "--port", "14096"]
    record.write_text(json.dumps({"identity": identity, "command": command}))
    monkeypatch.setattr(process_manager, "process_identity", lambda _pid: identity)
    monkeypatch.setattr(channel_switch, "_probe_listener", lambda _port: True)
    monkeypatch.setattr(channel_switch, "_listener_pid", lambda _port: 123)
    monkeypatch.setattr(process_manager.os, "getpgid", lambda _pid: 123)

    def busy(*_args, **_kwargs):
        raise RuntimeError("原生 Session 忙碌")

    monkeypatch.setattr(channel_switch, "assert_native_idle", busy)
    monkeypatch.setattr(process_manager.os, "killpg", lambda *_a: pytest.fail("忙碌不能杀进程"))
    with pytest.raises(RuntimeError, match="原生 Session 忙碌"):
        process_manager.manage_process(
            record,
            command=command,
            workspace=str(tmp_path),
            log_path=tmp_path / "log",
            environ={},
            restart=True,
            opencode=True,
        )


async def test_canceled_maintenance_cannot_start_a_container_process(monkeypatch):
    from unittest.mock import AsyncMock

    worker, commands, _files = _provisioner(monkeypatch)
    from app.runtime.remote_processes import manage

    monkeypatch.setattr(worker, "_ensure_safe", AsyncMock(side_effect=RuntimeError("维护租约已失效")))
    with pytest.raises(RuntimeError, match="租约已失效"):
        await manage(worker, "opencode", restart=True)
    assert commands == []


async def test_install_and_repeat_use_only_managed_processes_and_reuse_token(monkeypatch):
    worker, commands, files = _provisioner(monkeypatch)
    await worker._install_worker(AsyncMock())
    first = worker._manage_process.await_args_list
    assert [call.args[0] for call in first] == ["opencode", "worker"]
    assert all(call.kwargs["restart"] for call in first)
    assert not any("systemctl" in command for command in commands)
    assert not any("systemd" in path for path in files)
    assert b"NEXUS_WORKER_TOKEN=worker-token-canary" in files["/etc/nexus/worker-71.env"][0]
    assert b"worker-token-canary" not in files["/etc/nexus/opencode-71.env"][0]
    worker._manage_process.reset_mock()
    await worker._install_worker(AsyncMock())
    assert all(not call.kwargs["restart"] for call in worker._manage_process.await_args_list)
    assert worker._existing_worker_credentials.await_count == 2


async def test_restart_is_ssh_process_control_and_checks_health_before_worker(monkeypatch):
    worker, commands, _files = _provisioner(monkeypatch)
    order = []

    async def manage(name, **kwargs):
        order.append(name)

    async def health():
        order.append("health")

    worker._manage_process, worker._wait_opencode_ready = manage, health
    worker._wait_until_online = AsyncMock()
    await worker._restart_processes(AsyncMock())
    assert order == ["opencode", "health", "worker"]
    worker._wait_until_online.assert_awaited_once()
    assert not any("systemctl" in command for command in commands)


async def test_busy_or_unknown_native_state_blocks_restart(monkeypatch):
    worker, _commands, _files = _provisioner(monkeypatch)
    worker._native_idle = AsyncMock(side_effect=RuntimeError("原生 Session 忙碌或不可读"))
    with pytest.raises(RuntimeError, match="忙碌或不可读"):
        await worker._restart_processes(AsyncMock())
    worker._manage_process.assert_not_awaited()


def test_health_reports_nexus_process_record(monkeypatch):
    from app.worker_agent.inspection import _managed_by

    monkeypatch.delenv("NEXUS_WORKER_OPENCODE_PROCESS_RECORD", raising=False)
    assert _managed_by() == "external"
    monkeypatch.setenv("NEXUS_WORKER_OPENCODE_PROCESS_RECORD", "/managed/process.json")
    assert _managed_by() == "process"
