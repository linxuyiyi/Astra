"""Nexus-side process control: signals and starts are driven by verified target facts."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.runtime import remote_processes, remote_provisioning


@pytest.fixture
def controlled_worker(monkeypatch):
    record = {
        "identity": {
            "pid": 321,
            "pgid": 321,
            "cwd": "/srv/project",
            "argv": ["opencode"],
            "starttime": "123",
            "boot_id": "boot",
        },
        "command": ["/managed/opencode", "serve", "--hostname", "127.0.0.1", "--port", "14096"],
    }
    worker = SimpleNamespace(
        environment=SimpleNamespace(id=71, workspace_root="/srv/project", opencode_server_port=14096),
        target_binary="/managed/opencode",
        ssh=SimpleNamespace(read_file=AsyncMock(return_value=json.dumps(record))),
        _ensure_safe=AsyncMock(),
        _native_idle=AsyncMock(return_value=True),
        _command=AsyncMock(return_value="/srv/project"),
    )
    events = []

    async def primitive(_worker, source, *args):
        if "active=_probe_listener" in source:
            return json.dumps({"active": True, "pgid": 321})
        if "print('active' if" in source:
            return "stopped"
        if "os.killpg" in source:
            events.append("stop")
        if "subprocess.Popen" in source:
            events.append("start")
            assert all("token-canary" not in value for value in args)
        return ""

    monkeypatch.setattr(remote_processes, "_python", primitive)
    monkeypatch.setattr(remote_processes, "identity", AsyncMock(return_value=record["identity"]))
    monkeypatch.setattr(remote_provisioning, "worker_install_dir", lambda _id: "/private/managed")
    return worker, record, events


async def test_repeat_ensure_reuses_the_exact_process_identity(controlled_worker):
    worker, _record, events = controlled_worker
    await remote_processes.manage(worker, "opencode", restart=False)
    assert events == []


async def test_restart_stops_then_starts_under_the_existing_native_gate(controlled_worker, monkeypatch):
    worker, record, events = controlled_worker
    monkeypatch.setattr(remote_processes, "identity", AsyncMock(side_effect=[record["identity"], {}]))
    await remote_processes.manage(worker, "opencode", restart=True)
    assert events == ["stop", "start"]
    assert worker._native_idle.await_count >= 2


async def test_pid_reuse_cannot_receive_a_signal(controlled_worker, monkeypatch):
    worker, record, events = controlled_worker
    monkeypatch.setattr(
        remote_processes, "identity", AsyncMock(return_value={**record["identity"], "starttime": "new"})
    )
    with pytest.raises(RuntimeError, match="身份已变化"):
        await remote_processes.manage(worker, "opencode", restart=True)
    assert events == []


async def test_busy_native_state_prevents_any_signal(controlled_worker):
    worker, _record, events = controlled_worker
    worker._native_idle.side_effect = RuntimeError("native busy")
    with pytest.raises(RuntimeError, match="native busy"):
        await remote_processes.manage(worker, "opencode", restart=True)
    assert events == []


async def test_unowned_listener_cannot_be_restarted(controlled_worker, monkeypatch):
    worker, _record, events = controlled_worker

    async def unowned(*_args):
        return json.dumps({"active": True, "pgid": 999})

    monkeypatch.setattr(remote_processes, "_python", unowned)
    with pytest.raises(RuntimeError, match="不属于受管进程"):
        await remote_processes.manage(worker, "opencode", restart=True)
    assert events == []


async def test_lost_lease_prevents_even_starting_a_remote_operation(controlled_worker):
    worker, _record, events = controlled_worker
    worker._ensure_safe.side_effect = RuntimeError("lease lost")
    with pytest.raises(RuntimeError, match="lease lost"):
        await remote_processes.manage(worker, "opencode", restart=True)
    worker.ssh.read_file.assert_not_awaited()
    assert events == []
