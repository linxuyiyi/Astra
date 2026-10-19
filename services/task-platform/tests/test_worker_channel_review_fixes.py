"""Fixes from the phase-2 local verification and code review."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.workspace_routes import native_events
from app.config.settings import get_settings
from app.database import SessionLocal, init_database
from app.domain import ExecutionMode
from app.main import app
from app.models import (
    DigitalEmployee,
    OpenCodeExecutionEnvironment,
    Task,
    WorkerCommand,
    WorkerHttpSlot,
    WorkerInstance,
    WorkerMaintenanceOperation,
    utcnow,
)
from app.runtime.lifecycle import MessageHistoryIncomplete, RuntimeService, RuntimeStateError
from app.runtime.recovery import _refresh_with_client
from app.runtime.remote_provisioning import RemoteProvisioner, guard_environment_write
from app.runtime.session_connection import connect
from app.runtime.worker_channel import main as channel_main
from app.runtime.worker_channel import mark_worker_channel, read_worker_channel
from app.runtime.worker_client import WorkerOpenCodeClient
from app.runtime.workspace_files import check_request_path
from app.worker_agent.channel_switch import restart_local_opencode
from app.worker_agent.connection import (
    AgentConfig,
    WorkerConnectionManager,
    _bounded_event_frame,
)
from app.worker_agent.local_opencode import bind_local_opencode
from app.worker_agent.process_env import child_environ
from app.worker_agent.workspace import prepare_workspace_local
from app.worker_gateway.data_channel import DataChannel, response_key, seal_unfetched_request
from app.worker_gateway.events import EventLog
from app.worker_gateway.protocol import WorkerGatewayError, WorkerMessage, envelope
from app.worker_gateway.service import (
    WorkerGateway,
    expire_due_commands_statement,
    held_slot_expire_statement,
)
from tests.test_worker_data_channel import _direct_switch_result
from tests.test_worker_gateway import ADMIN_HEADERS, bring_online, gateway_for


@pytest.fixture
async def environment_id() -> int:
    await init_database()
    async with SessionLocal() as session:
        employee = DigitalEmployee(id=f"emp-{uuid4().hex[:8]}", name="远程员工", role="执行")
        session.add(employee)
        await session.flush()
        environment = OpenCodeExecutionEnvironment(
            employee_id=employee.id,
            mode="LINUX_REMOTE",
            name="云执行机",
            worker_channel="ssh",
            workspace_root="/srv/app",
            opencode_server_port=4096,
        )
        session.add(environment)
        await session.commit()
        return environment.id


async def _mark(environment_id: int) -> None:
    async with SessionLocal() as session:
        environment = await session.get(OpenCodeExecutionEnvironment, environment_id)
        environment.worker_channel = "worker"
        await session.commit()


def _detached_environment(environment_id: int, employee_id: str) -> OpenCodeExecutionEnvironment:
    return OpenCodeExecutionEnvironment(
        id=environment_id,
        employee_id=employee_id,
        mode=ExecutionMode.LINUX_REMOTE,
        name="云执行机",
        workspace_root="/srv/app",
        opencode_server_port=4096,
        opencode_path="opencode",
        worker_channel="worker",
    )


async def _complete(gateway: WorkerGateway, frame: dict, epoch: int, result: dict | None = None) -> None:
    reply = envelope(
        request_id=frame["request_id"],
        worker_instance_id=frame["worker_instance_id"],
        environment_id=frame["environment_id"],
        session_id=frame.get("session_id"),
        message_type="response",
        payload={"stage": "result", "ok": True, "result": result or {"ok": True}, "error_code": None},
    )
    reply.epoch = epoch
    async with SessionLocal() as session:
        await gateway.apply_response(
            session,
            worker_instance_id=frame["worker_instance_id"],
            epoch=epoch,
            message=reply,
        )
    await gateway.bus.publish_response(frame["request_id"], reply.to_dict())


@pytest.mark.asyncio
async def test_connect_start_health_uses_the_worker_channel(environment_id, monkeypatch):
    gateway = gateway_for()
    enrolled, auth = await bring_online(gateway, environment_id)
    await _mark(environment_id)
    monkeypatch.setattr("app.runtime.worker_client.get_gateway", lambda: gateway)
    local_calls: list[str] = []

    class GuardClient(httpx.AsyncClient):
        async def get(self, url, *args, **kwargs):
            local_calls.append(str(url))
            raise AssertionError(url)

    monkeypatch.setattr(httpx, "AsyncClient", GuardClient)
    queues = gateway.bus.bound[enrolled.worker_instance_id]

    async def answer():
        frame = await queues.wait_frame()
        assert frame["payload"]["name"] == "http"
        assert frame["payload"]["data"]["path"] == "/global/health"
        async with SessionLocal() as session:
            await DataChannel(gateway).commit_upload(
                session,
                request_id=frame["request_id"],
                worker_instance_id=enrolled.worker_instance_id,
                epoch=auth.epoch,
                upload_id="up-health",
                http_status=200,
                headers={},
                content_type="application/json",
                chunk=json.dumps({"healthy": True, "version": "1.18.35"}),
                complete=True,
            )

    running = asyncio.create_task(answer())
    async with SessionLocal() as session:
        stored = await session.get(OpenCodeExecutionEnvironment, environment_id)
        environment = _detached_environment(environment_id, stored.employee_id)
    async with connect(environment, start=True) as client:
        assert isinstance(client, WorkerOpenCodeClient)
    await running
    assert local_calls == []


@pytest.mark.asyncio
async def test_open_event_stream_emits_gap_when_the_worker_epoch_changes(environment_id, monkeypatch):
    gateway = gateway_for()
    enrolled, auth = await bring_online(gateway, environment_id)
    await _mark(environment_id)
    monkeypatch.setattr("app.runtime.worker_client.get_gateway", lambda: gateway)
    queues = gateway.bus.bound[enrolled.worker_instance_id]
    names: list[str] = []

    async def answer():
        while True:
            frame = await queues.wait_frame()
            names.append(frame["payload"]["name"])
            await _complete(gateway, frame, auth.epoch, {"subscribed": True})

    answering = asyncio.create_task(answer())
    client = WorkerOpenCodeClient(environment_id=environment_id, gateway=gateway, timeout_seconds=5)
    stream = client.events(Path("/srv/app"), consumer="browser:held").__aiter__()
    pending = asyncio.create_task(anext(stream))
    await asyncio.wait_for(_until(lambda: "subscribe_events" in names), 3)
    await EventLog(gateway.bus, limit=8).append(
        worker_id=enrolled.worker_instance_id,
        epoch=auth.epoch,
        directory="/srv/app",
        payload={"seq": 1, "directory": "/srv/app", "event": {"type": "server.heartbeat"}},
    )
    first = await asyncio.wait_for(pending, 3)
    assert first["type"] == "server.heartbeat"
    async with SessionLocal() as session:
        instance = await session.get(WorkerInstance, enrolled.worker_instance_id)
        instance.epoch += 1
        await session.commit()
    gap = await asyncio.wait_for(anext(stream), 3)
    assert gap["type"] == "astra.event_gap"
    assert gap["reason"] == "worker_reconnected"
    await stream.aclose()
    answering.cancel()
    await asyncio.gather(answering, return_exceptions=True)


@pytest.mark.asyncio
async def test_open_event_stream_resumes_after_pause(environment_id, monkeypatch):
    gateway = gateway_for()
    enrolled, auth = await bring_online(gateway, environment_id)
    await _mark(environment_id)
    monkeypatch.setattr("app.runtime.worker_client.get_gateway", lambda: gateway)
    resumed = {"done": False}
    original = EventLog.control

    async def control(self, **kwargs):
        action = await original(self, **kwargs)
        if action is None and not resumed["done"]:
            resumed["done"] = True
            return "resume_events"
        return action

    monkeypatch.setattr(EventLog, "control", control)
    queues = gateway.bus.bound[enrolled.worker_instance_id]
    names: list[str] = []

    async def answer():
        while "resume_events" not in names:
            frame = await asyncio.wait_for(queues.wait_frame(), 3)
            names.append(frame["payload"]["name"])
            await _complete(gateway, frame, auth.epoch, {"subscribed": True})

    answering = asyncio.create_task(answer())
    client = WorkerOpenCodeClient(environment_id=environment_id, gateway=gateway, timeout_seconds=5)
    stream = client.events(Path("/srv/app"), consumer="browser:pause").__aiter__()
    reading = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(answering, 5)
        assert names[0] == "subscribe_events"
        assert "resume_events" in names
    finally:
        reading.cancel()
        answering.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(reading, answering, return_exceptions=True), 2)
        except TimeoutError:
            pass


def test_resume_wakes_a_paused_event_reader():
    manager = WorkerConnectionManager(
        AgentConfig(
            gateway_url="ws://nexus.internal/api/worker-gateway/connect",
            worker_instance_id="wrk",
            environment_id=1,
            token="wtk_test",
        )
    )
    manager.paused_directories.add("/srv/app")
    manager.resume_waiters["/srv/app"] = asyncio.Event()
    manager._note_event_control("resume_events", {"directory": "/srv/app"})
    assert "/srv/app" not in manager.paused_directories
    assert manager.resume_waiters["/srv/app"].is_set()


def test_child_environ_drops_worker_credentials_and_restart_is_not_pure(tmp_path, monkeypatch):
    source = {
        "PATH": "/usr/bin",
        "HOME": "/home/dev",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NEXUS_WORKER_TOKEN": "wtk_secret",
        "NEXUS_WORKER_GATEWAY_URL": "ws://nexus/api/worker-gateway/connect",
        "NEXUS_GATEWAY_MASTER_KEY": "master",
    }
    cleaned = child_environ(source)
    assert cleaned["PATH"] == "/usr/bin"
    assert "NEXUS_WORKER_TOKEN" not in cleaned
    assert "NEXUS_WORKER_GATEWAY_URL" not in cleaned
    assert "NEXUS_GATEWAY_MASTER_KEY" not in cleaned
    captured: dict[str, object] = {}

    def popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return SimpleNamespace()

    monkeypatch.setattr("app.worker_agent.channel_switch._listener_pid", lambda _port: None)
    monkeypatch.setattr("app.worker_agent.channel_switch._probe_listener", lambda _port: False)
    monkeypatch.setattr("app.worker_agent.channel_switch.probe_url", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.worker_agent.channel_switch._wait_until_healthy", lambda *_a, **_k: None)
    monkeypatch.setattr("app.worker_agent.channel_switch.subprocess.Popen", popen)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    restart_local_opencode(
        str(workspace),
        4096,
        environ={**source, "NEXUS_WORKER_OPENCODE_BIN": "/opt/opencode"},
    )
    assert captured["cmd"] == ["/opt/opencode", "serve", "--hostname", "127.0.0.1", "--port", "4096"]
    assert "--pure" not in captured["cmd"]
    assert "NEXUS_WORKER_TOKEN" not in captured["env"]


def test_restart_refuses_a_port_that_is_not_opencode(tmp_path, monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr("app.worker_agent.channel_switch._probe_listener", lambda _port: True)
    monkeypatch.setattr("app.worker_agent.channel_switch._read_native_status", lambda *_a, **_k: {})
    monkeypatch.setattr("app.worker_agent.channel_switch._listener_pid", lambda _port: 4321)
    monkeypatch.setattr("app.worker_agent.channel_switch._opencode_listener", lambda _pid: False)
    monkeypatch.setattr("app.worker_agent.channel_switch.os.kill", lambda pid, _sig: killed.append(pid))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(RuntimeError, match="不是 OpenCode"):
        restart_local_opencode(str(workspace), 4096, environ={"PATH": "/usr/bin", "HOME": "/home/dev"})
    assert killed == []


def test_systemd_restart_does_not_inherit_the_worker_token(tmp_path, monkeypatch):
    calls: list[tuple] = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("env")))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("app.worker_agent.channel_switch.subprocess.run", run)
    monkeypatch.setattr("app.worker_agent.channel_switch.probe_url", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.worker_agent.channel_switch._wait_until_healthy", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "app.worker_agent.channel_switch.subprocess.Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("systemd 路径不应再拉起进程")),
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    restart_local_opencode(
        str(workspace),
        4096,
        environ={
            "PATH": "/usr/bin",
            "HOME": "/home/dev",
            "NEXUS_WORKER_TOKEN": "wtk_secret",
            "NEXUS_WORKER_OPENCODE_UNIT": "opencode.service",
        },
    )
    assert calls[0][0] == ["systemctl", "restart", "opencode.service"]
    assert "NEXUS_WORKER_TOKEN" not in calls[0][1]


def test_oversized_event_frame_becomes_a_gap_marker():
    incoming = WorkerMessage(
        request_id="evt",
        worker_instance_id="wrk",
        environment_id=1,
        session_id=None,
        type="event",
        payload={},
    )
    config = AgentConfig(
        gateway_url="ws://nexus",
        worker_instance_id="wrk",
        environment_id=1,
        token="wtk_test",
        max_frame_bytes=180,
    )
    frame = _bounded_event_frame(
        incoming,
        config,
        directory="/srv/app",
        seq=3,
        event={"type": "file.edited", "properties": {"text": "x" * 400}},
        epoch=1,
        expires_at=10,
    )
    assert frame["payload"]["dropped"] is True
    assert frame["payload"]["reason"] == "event_too_large"
    assert "event" not in frame["payload"]


def test_prepare_workspace_stays_under_the_configured_root_and_rejects_symlinks(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "opencode.json").write_text("{}\n")
    outside = tmp_path / "elsewhere"
    with pytest.raises(RuntimeError, match="不在允许范围内"):
        prepare_workspace_local(str(root), str(outside), allowed_root=str(root))
    link = root / "opencode.jsonc"
    link.symlink_to(root / "opencode.json")
    inside = root / "task"
    with pytest.raises(RuntimeError, match="符号链接"):
        prepare_workspace_local(str(root), str(inside), allowed_root=str(root))
    allowed = root.parent / ".astra-tasks" / "emp" / "task"
    link.unlink()
    created = prepare_workspace_local(str(root), str(allowed), allowed_root=str(root))
    assert created["created"] == "true"
    assert (allowed / "opencode.json").is_file()


@pytest.mark.asyncio
async def test_fetch_rejects_a_sealed_or_stale_attempt(environment_id):
    gateway = gateway_for()
    enrolled, auth = await bring_online(gateway, environment_id)
    async with SessionLocal() as session:
        employee_id = (
            await session.scalar(
                select(OpenCodeExecutionEnvironment.employee_id).where(
                    OpenCodeExecutionEnvironment.id == environment_id
                )
            )
        )
        task = Task(
            id="T-seal",
            title="seal",
            task_type="ISSUE",
            status="RUNNING",
            employee_id=employee_id,
            current_session_id="ses",
            pending_message_id="msg_seal000001",
            pending_attempt=1,
            pending_prompt={"action": "prompt_async", "body": {"messageID": "msg_seal000001"}},
        )
        from app.runtime.submission import put_attempt

        put_attempt(task, "msg_seal000001", 1, "req-seal")
        session.add(task)
        await session.commit()
        await gateway.dispatch(
            session,
            worker_instance_id=enrolled.worker_instance_id,
            request_id="req-seal",
            name="http",
            payload={
                "method": "POST",
                "path": "/session/ses/prompt_async",
                "body": {"messageID": "msg_seal000001"},
                "attempt_ref": {"task_id": "T-seal", "message_id": "msg_seal000001", "attempt": 1},
            },
            session_id="ses",
        )
    async with SessionLocal() as session:
        command = await session.scalar(select(WorkerCommand).where(WorkerCommand.request_id == "req-seal"))
        command.deadline_at = utcnow() - timedelta(seconds=1)
        await session.commit()
        with pytest.raises(WorkerGatewayError) as expired:
            await DataChannel(gateway).fetch_request(
                session,
                request_id="req-seal",
                worker_instance_id=enrolled.worker_instance_id,
                epoch=auth.epoch,
            )
        assert expired.value.code == "epoch_conflict"
        command.deadline_at = utcnow() + timedelta(minutes=5)
        task = await session.get(Task, "T-seal")
        task.pending_attempt = 2
        await session.commit()
        with pytest.raises(WorkerGatewayError) as stale:
            await DataChannel(gateway).fetch_request(
                session,
                request_id="req-seal",
                worker_instance_id=enrolled.worker_instance_id,
                epoch=auth.epoch,
            )
        assert "当前提交" in str(stale.value)
        task.pending_attempt = 1
        await session.commit()
        await seal_unfetched_request(session, "req-seal")
        await session.commit()
        with pytest.raises(WorkerGatewayError):
            await DataChannel(gateway).fetch_request(
                session,
                request_id="req-seal",
                worker_instance_id=enrolled.worker_instance_id,
                epoch=auth.epoch,
            )
        stored = await session.scalar(select(WorkerCommand).where(WorkerCommand.request_id == "req-seal"))
        assert stored.status == "failed"
        assert stored.error_code == "superseded"
        assert stored.body_fetched_at is None


@pytest.mark.asyncio
async def test_incomplete_message_pages_do_not_count_as_read():
    class Pages:
        def __init__(self, pages):
            self.pages = pages
            self.posts = 0

        async def messages_page(self, _directory, _sid, *, limit, before=None):
            return self.pages[before]

    service = RuntimeService(None)
    empty = Pages({None: {"messages": [], "next_before": None}})
    assert await service._all_messages(empty, Path("/srv/app"), "ses") == []
    repeated = Pages(
        {
            None: {"messages": [{"info": {"id": "msg_1"}}], "next_before": "same"},
            "same": {"messages": [{"info": {"id": "msg_2"}}], "next_before": "same"},
        }
    )
    with pytest.raises(MessageHistoryIncomplete):
        await service._all_messages(repeated, Path("/srv/app"), "ses")


@pytest.mark.asyncio
async def test_switch_holds_the_maintenance_lock_until_the_mark(environment_id):
    async with SessionLocal() as session:
        environment = await session.get(OpenCodeExecutionEnvironment, environment_id)
        employee_id = environment.employee_id

        async def occupy():
            async with SessionLocal() as other:
                operation = await other.scalar(
                    select(WorkerMaintenanceOperation).where(
                        WorkerMaintenanceOperation.environment_id == environment_id,
                        WorkerMaintenanceOperation.state.in_(
                            ["QUEUED", "PREFLIGHT", "RUNNING", "VERIFYING", "INTERRUPTED"]
                        ),
                    )
                )
                assert operation is not None
                other.add(
                    Task(
                        id="T-during-switch",
                        title="during",
                        task_type="ISSUE",
                        status="RUNNING",
                        employee_id=employee_id,
                    )
                )
                await other.commit()
            return _direct_switch_result()

        with pytest.raises(HTTPException) as blocked:
            await mark_worker_channel(session, environment_id, switch=occupy)
        assert blocked.value.status_code == 409
        assert await read_worker_channel(session, environment_id) == "ssh"
        operation = await session.scalar(
            select(WorkerMaintenanceOperation)
            .where(WorkerMaintenanceOperation.environment_id == environment_id)
            .order_by(WorkerMaintenanceOperation.requested_at.desc())
        )
        assert operation.state == "INTERRUPTED"
        assert operation.active_environment_id == environment_id


def test_admin_command_route_rejects_switch_channel():
    with TestClient(app) as client:
        response = client.post(
            "/api/worker-gateway/instances/wrk_missing/commands",
            headers=ADMIN_HEADERS,
            json={"request_id": "cmd-switch", "name": "switch_channel", "payload": {}},
        )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "maintenance_command"


def test_channel_admin_route_and_cli_call_mark(monkeypatch):
    seen: dict[str, int] = {}

    async def fake(_session, environment_id, *, switch=None):
        seen["id"] = environment_id
        return SimpleNamespace(id=environment_id, worker_channel="worker")

    monkeypatch.setattr("app.runtime.worker_channel.mark_worker_channel", fake)
    with TestClient(app) as client:
        response = client.post("/api/worker-gateway/environments/7/channel", headers=ADMIN_HEADERS)
    assert response.status_code == 200
    assert response.json()["worker_channel"] == "worker"
    assert seen["id"] == 7
    assert channel_main(["--environment-id", "9"]) == 0
    assert seen["id"] == 9


@pytest.mark.asyncio
async def test_worker_provisioning_writes_intranet_urls(environment_id, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "remote_gateway_local_url", "http://10.0.0.8:8110")
    monkeypatch.setattr(settings, "mcp_resource_server_url", "http://10.0.0.8:8201/mcp")
    async with SessionLocal() as session:
        environment = await session.get(OpenCodeExecutionEnvironment, environment_id)
        environment.worker_channel = "worker"
        await session.commit()
        await session.refresh(environment)
    provisioner = RemoteProvisioner(environment, {"id": "op"})
    gateway_url, mcp_url = await provisioner._channel_endpoints()
    assert gateway_url == "http://10.0.0.8:8110"
    assert mcp_url == "http://10.0.0.8:8201/mcp"
    monkeypatch.setattr(settings, "remote_gateway_local_url", "http://127.0.0.1:8110")
    with pytest.raises(RuntimeError, match="内网"):
        provisioner._direct_endpoints()


@pytest.mark.asyncio
async def test_worker_prompt_path_check_does_not_open_ssh(monkeypatch):
    async def opened(self, environment):
        raise AssertionError(environment.host)

    monkeypatch.setattr("app.runtime.environment.ExecutionEnvironmentRuntime.open_session", opened)
    environment = OpenCodeExecutionEnvironment(
        mode="LINUX_REMOTE", worker_channel="worker", workspace_root="/srv/app"
    )
    checked = await check_request_path(
        environment, "/srv/app", ".", native_path="/session/ses/prompt_async"
    )
    assert checked == "."
    calls = []

    async def validate(environment, workspace, value):
        calls.append((workspace, value))
        return "."

    monkeypatch.setattr("app.runtime.worker_files.worker_validate_path", validate)
    checked = await check_request_path(environment, "/srv/app", ".", native_path="/file")
    assert checked == "."
    assert calls == [("/srv/app", ".")]


@pytest.mark.asyncio
async def test_sweep_releases_a_held_http_slot_once(environment_id):
    gateway = gateway_for()
    enrolled, auth = await bring_online(gateway, environment_id)
    async with SessionLocal() as session:
        await gateway.dispatch(
            session,
            worker_instance_id=enrolled.worker_instance_id,
            request_id="req-slot",
            name="http",
            payload={"method": "GET", "path": "/global/health", "body": {}},
        )
        command = await session.scalar(select(WorkerCommand).where(WorkerCommand.request_id == "req-slot"))
        command.deadline_at = utcnow() - timedelta(seconds=5)
        await session.commit()
    assert await gateway.sweep() >= 1
    async with SessionLocal() as session:
        slot = await session.get(WorkerHttpSlot, (enrolled.worker_instance_id, auth.epoch))
        assert slot.inflight == 0
    await gateway.sweep()
    async with SessionLocal() as session:
        slot = await session.get(WorkerHttpSlot, (enrolled.worker_instance_id, auth.epoch))
        assert slot.inflight == 0


@pytest.mark.asyncio
async def test_local_opencode_call_has_an_overall_timeout(monkeypatch):
    class Hang:
        def __init__(self, *args, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, *_args, **_kwargs):
            await asyncio.sleep(5)
            return None

    monkeypatch.setattr(httpx, "AsyncClient", Hang)
    manager = SimpleNamespace(
        config=SimpleNamespace(
            gateway_url="http://127.0.0.1:9",
            token="wtk_test",
            opencode_url="http://127.0.0.1:9",
        )
    )
    bind_local_opencode(manager, timeout=0.05)
    with pytest.raises(TimeoutError):
        await manager.local_request("GET", "/global/health", {"query": {}, "body": {}})


@pytest.mark.asyncio
async def test_refresh_snapshot_survives_a_closed_session(monkeypatch):
    await init_database()
    async with SessionLocal() as session:
        session.add(Task(id="T-detached", title="detached", task_type="ISSUE", status="RUNNING"))
        await session.commit()

    async def refresh(self, task_id, *, client=None):
        task = await self.session.get(Task, task_id)
        await self.session.rollback()
        return task

    monkeypatch.setattr("app.runtime.recovery.RuntimeService.refresh", refresh)
    snapshot = await _refresh_with_client("T-detached", None)
    assert snapshot.status == "RUNNING"
    assert snapshot.current_session_id is None


async def _hold_operation(environment_id: int, *, expires_in: float = 300, status: str = "UNKNOWN") -> str:
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        environment = await session.get(OpenCodeExecutionEnvironment, environment_id)
        environment.status = status
        session.add(
            WorkerMaintenanceOperation(
                id="op_" + uuid4().hex,
                environment_id=environment_id,
                action="switch_channel",
                state="RUNNING",
                stage="channel",
                requested_by="test-hold",
                attempt=1,
                lease_owner="test-hold",
                lease_until=now + timedelta(seconds=expires_in),
                target_revision={},
                before_snapshot={},
                after_snapshot={},
                active_environment_id=environment_id,
                requested_at=now,
                started_at=now,
            )
        )
        await session.commit()
        return environment.employee_id


def _snapshot(environment_id: int) -> dict:
    return {
        "environment_id": environment_id,
        "mode": "LINUX_REMOTE",
        "name": "云执行机",
        "host": None,
        "port": 22,
        "username": None,
        "has_ssh_password": False,
        "opencode_server_port": 4096,
        "opencode_path": "opencode",
        "model_name": None,
        "workspace_root": "/srv/app",
        "labels": [],
    }


def test_mysql_dialect_compiles_sweep_without_returning():
    from sqlalchemy.dialects import mysql

    now = datetime.now(UTC)
    due = (
        WorkerCommand.status.in_(("pending", "accepted")),
        WorkerCommand.deadline_at.is_not(None),
        WorkerCommand.deadline_at <= now,
    )
    values = {
        "status": "expired",
        "error_code": "timeout",
        "finished_at": now,
        "updated_at": now,
        "slot_held": False,
    }
    dialect = mysql.dialect()
    statements = (
        held_slot_expire_statement("cmd-1", due, values),
        expire_due_commands_statement(due, values),
    )
    for statement in statements:
        sql = str(statement.compile(dialect=dialect))
        assert "RETURNING" not in sql.upper()
    root = Path(__file__).resolve().parents[1] / "app"
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if ".returning(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


@pytest.mark.skipif(
    not os.environ.get("ASTRA_TEST_MYSQL_URL"),
    reason="未提供 ASTRA_TEST_MYSQL_URL，只编译 MySQL 方言",
)
def test_sweep_statements_execute_on_mysql():
    from sqlalchemy import create_engine

    engine = create_engine(os.environ["ASTRA_TEST_MYSQL_URL"])
    now = datetime.now(UTC).replace(tzinfo=None)
    due = (
        WorkerCommand.status.in_(("pending", "accepted")),
        WorkerCommand.deadline_at.is_not(None),
        WorkerCommand.deadline_at <= now,
    )
    values = {
        "status": "expired",
        "error_code": "timeout",
        "finished_at": now,
        "updated_at": now,
        "slot_held": False,
    }
    with engine.begin() as connection:
        for statement in (
            held_slot_expire_statement("cmd-missing", due, values),
            expire_due_commands_statement(due, values),
        ):
            compiled = statement.compile(dialect=connection.dialect)
            assert "RETURNING" not in str(compiled).upper()
            connection.execute(statement)


@pytest.mark.asyncio
async def test_second_switch_cannot_replace_a_running_reservation(environment_id):
    started = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        started.set()
        await release.wait()
        return _direct_switch_result()

    async def first():
        async with SessionLocal() as session:
            return await mark_worker_channel(session, environment_id, switch=hold)

    running = asyncio.create_task(first())
    await asyncio.wait_for(started.wait(), 3)
    async with SessionLocal() as session:
        operation = await session.scalar(
            select(WorkerMaintenanceOperation).where(
                WorkerMaintenanceOperation.environment_id == environment_id,
                WorkerMaintenanceOperation.state.in_(
                    ["QUEUED", "PREFLIGHT", "RUNNING", "VERIFYING", "INTERRUPTED"]
                ),
            )
        )
        operation_id = operation.id
        with pytest.raises(HTTPException) as blocked:
            await mark_worker_channel(
                session,
                environment_id,
                switch=lambda: _async_result(_direct_switch_result()),
            )
        assert blocked.value.status_code == 409
        await session.rollback()
        operation = await session.scalar(
            select(WorkerMaintenanceOperation).where(
                WorkerMaintenanceOperation.environment_id == environment_id,
                WorkerMaintenanceOperation.state.in_(
                    ["QUEUED", "PREFLIGHT", "RUNNING", "VERIFYING", "INTERRUPTED"]
                ),
            )
        )
        assert operation.id == operation_id
    release.set()
    switched = await running
    assert switched.worker_channel == "worker"


async def _async_result(value):
    return value


@pytest.mark.asyncio
async def test_start_and_send_reject_an_active_maintenance_reservation(environment_id, monkeypatch):
    async def blocked(*_args, **_kwargs):
        raise AssertionError("维护预留期间不能连接 OpenCode")

    monkeypatch.setattr("app.runtime.lifecycle.connect", blocked)
    employee_id = await _hold_operation(environment_id, status="UNKNOWN")
    start_id = f"T-start-{uuid4().hex[:8]}"
    send_id = f"T-send-{uuid4().hex[:8]}"
    async with SessionLocal() as session:
        session.add(
            Task(
                id=start_id,
                title="start",
                task_type="ISSUE",
                status="PENDING",
                employee_id=employee_id,
            )
        )
        await session.commit()
        with pytest.raises(RuntimeStateError, match="维护"):
            await RuntimeService(session).start(start_id)
    async with SessionLocal() as session:
        session.add(
            Task(
                id=send_id,
                title="send",
                task_type="ISSUE",
                status="WAITING",
                employee_id=employee_id,
                current_session_id="ses_maint",
                workspace="/srv/app",
                execution_environment_snapshot=_snapshot(environment_id),
            )
        )
        await session.commit()
        with pytest.raises(RuntimeStateError, match="维护"):
            await RuntimeService(session).send(send_id, {"messageID": "msg_maint000001"})
    async with SessionLocal() as session:
        started = await session.get(Task, start_id)
        assert started.pending_message_id is None
        assert started.status == "PENDING"


@pytest.mark.asyncio
async def test_expired_lease_does_not_release_unknown_maintenance_lock(environment_id):
    await _hold_operation(environment_id, expires_in=-5, status="UNKNOWN")
    async with SessionLocal() as session:
        environment = await session.get(OpenCodeExecutionEnvironment, environment_id)
        with pytest.raises(HTTPException):
            await guard_environment_write(session, environment)
        await session.rollback()
    from app.runtime.maintenance import recover_expired

    await recover_expired("test-recovery")
    async with SessionLocal() as session:
        environment = await session.get(OpenCodeExecutionEnvironment, environment_id)
        with pytest.raises(HTTPException):
            await guard_environment_write(session, environment)
        await session.rollback()


@pytest.mark.asyncio
async def test_event_stream_waits_through_offline_and_resumes_on_a_new_epoch(environment_id, monkeypatch):
    gateway = gateway_for()
    enrolled, auth = await bring_online(gateway, environment_id)
    await _mark(environment_id)
    monkeypatch.setattr("app.runtime.worker_client.get_gateway", lambda: gateway)
    queues = gateway.bus.bound[enrolled.worker_instance_id]
    names: list[str] = []

    async def answer():
        while True:
            frame = await queues.wait_frame()
            names.append(frame["payload"]["name"])
            await _complete(gateway, frame, frame["epoch"], {"subscribed": True})

    answering = asyncio.create_task(answer())
    client = WorkerOpenCodeClient(environment_id=environment_id, gateway=gateway, timeout_seconds=5)
    client.offline_grace_seconds = 3
    stream = client.events(Path("/srv/app"), consumer="browser:offline").__aiter__()
    pending = asyncio.create_task(anext(stream))
    await asyncio.wait_for(_until(lambda: "subscribe_events" in names), 3)
    async with SessionLocal() as session:
        instance = await session.get(WorkerInstance, enrolled.worker_instance_id)
        instance.status = "offline"
        await session.commit()
    keepalive = await asyncio.wait_for(pending, 3)
    assert keepalive["type"] == "astra.keepalive"
    new_epoch = auth.epoch + 1
    async with SessionLocal() as session:
        instance = await session.get(WorkerInstance, enrolled.worker_instance_id)
        instance.status = "online"
        instance.epoch = new_epoch
        await session.commit()
    assert await gateway.bus.set_owner(enrolled.worker_instance_id, gateway.owner_id, new_epoch)
    queues.epoch = new_epoch
    gap = await asyncio.wait_for(anext(stream), 3)
    assert gap["type"] == "astra.event_gap"
    assert gap["reason"] == "worker_reconnected"
    resumed = asyncio.create_task(anext(stream))
    await asyncio.wait_for(_until(lambda: names.count("subscribe_events") >= 2), 3)
    resumed.cancel()
    await asyncio.gather(resumed, return_exceptions=True)
    await stream.aclose()
    answering.cancel()
    await asyncio.gather(answering, return_exceptions=True)


@pytest.mark.asyncio
async def test_event_stream_emits_a_gap_before_ending_when_the_worker_stays_offline(
    environment_id, monkeypatch
):
    gateway = gateway_for()
    enrolled, _auth = await bring_online(gateway, environment_id)
    await _mark(environment_id)
    monkeypatch.setattr("app.runtime.worker_client.get_gateway", lambda: gateway)
    queues = gateway.bus.bound[enrolled.worker_instance_id]
    names: list[str] = []

    async def answer():
        frame = await queues.wait_frame()
        names.append(frame["payload"]["name"])
        await _complete(gateway, frame, frame["epoch"], {"subscribed": True})

    answering = asyncio.create_task(answer())
    client = WorkerOpenCodeClient(environment_id=environment_id, gateway=gateway, timeout_seconds=5)
    client.offline_grace_seconds = 0.2
    collected: list[dict] = []

    async def drain():
        async for event in client.events(Path("/srv/app"), consumer="browser:timeout"):
            collected.append(event)

    draining = asyncio.create_task(drain())
    await asyncio.wait_for(_until(lambda: "subscribe_events" in names), 3)
    async with SessionLocal() as session:
        instance = await session.get(WorkerInstance, enrolled.worker_instance_id)
        instance.status = "offline"
        await session.commit()
    await asyncio.wait_for(draining, 3)
    assert collected[-1]["type"] == "astra.event_gap"
    assert collected[-1]["reason"] == "worker_offline"
    answering.cancel()
    await asyncio.gather(answering, return_exceptions=True)


@pytest.mark.asyncio
async def test_native_event_keepalive_is_an_sse_comment(monkeypatch):
    class Client:
        records_fetch = True

        async def events(self, directory, *, consumer):
            yield {"type": "astra.keepalive", "directory": str(directory)}
            yield {
                "type": "astra.event_gap",
                "directory": str(directory),
                "reason": "worker_reconnected",
                "from": 0,
                "to": None,
            }

        async def children(self, *_args, **_kwargs):
            return []

    @asynccontextmanager
    async def fake_connect(*_args, **_kwargs):
        yield Client()

    monkeypatch.setattr("app.api.workspace_routes.connect", fake_connect)
    chunks = [
        chunk
        async for chunk in native_events(SimpleNamespace(), "/srv/app", "ses", False)
    ]
    assert chunks[0].startswith(": keepalive")
    assert "worker_reconnected" in chunks[1]


@pytest.mark.asyncio
async def test_duplicate_upload_removes_the_staged_body(environment_id):
    gateway = gateway_for()
    enrolled, auth = await bring_online(gateway, environment_id)
    async with SessionLocal() as session:
        await gateway.dispatch(
            session,
            worker_instance_id=enrolled.worker_instance_id,
            request_id="req-dup-body",
            name="http",
            payload={"method": "GET", "path": "/global/health", "body": {}},
        )
    body = '{"ok":true}'

    async def upload():
        async with SessionLocal() as session:
            return await DataChannel(gateway).commit_upload(
                session,
                request_id="req-dup-body",
                worker_instance_id=enrolled.worker_instance_id,
                epoch=auth.epoch,
                upload_id="up-dup",
                http_status=200,
                headers={},
                content_type="application/json",
                chunk=body,
                complete=True,
            )

    first = await upload()
    assert first["status"] == "stored"
    assert await gateway.bus.get_text(response_key("req-dup-body", "up-dup"))
    # The retry restages the same bytes. The already-succeeded command must not
    # keep that copy.
    await gateway.bus.delete_text(response_key("req-dup-body", "up-dup"))
    second = await upload()
    assert second["status"] == "stored"
    assert await gateway.bus.get_text(response_key("req-dup-body", "up-dup")) is None


@pytest.mark.asyncio
async def test_duplicate_http_request_does_not_start_another_task():
    from app.worker_gateway.protocol import parse_message

    calls = {"n": 0}
    started = asyncio.Event()
    release = asyncio.Event()

    async def local_request(_method, _path, _body):
        calls["n"] += 1
        started.set()
        await release.wait()
        return {"status": 204, "body": ""}

    manager = WorkerConnectionManager(
        AgentConfig(
            gateway_url="ws://127.0.0.1/api/worker-gateway/connect",
            worker_instance_id="wrk",
            environment_id=1,
            token="wtk_test",
        )
    )
    manager.epoch = 1
    manager.clock = lambda: 0
    manager.fetch_body = lambda _request_id, _epoch: _ready({"body": "{}"})
    manager.local_request = local_request
    manager.upload_body = lambda *_args: _ready(None)

    class Transport:
        def __init__(self):
            self.sent = []

        async def send(self, message):
            self.sent.append(message)

        async def recv(self):
            raise ConnectionError("closed")

        async def close(self):
            return None

    message = envelope(
        request_id="req-http",
        worker_instance_id="wrk",
        environment_id=1,
        session_id=None,
        message_type="command",
        payload={"name": "http", "data": {"method": "POST", "path": "/x", "ready": True}},
    )
    message.epoch = 1
    message.expires_at = 10**12
    incoming = parse_message(json.dumps(message.to_dict()), max_bytes=10**6)
    transport = Transport()
    manager._start_http(transport, incoming, 1)
    await asyncio.wait_for(started.wait(), 1)
    manager._start_http(transport, incoming, 1)
    assert calls["n"] == 1
    assert len(manager.http_tasks) == 1
    release.set()
    await asyncio.gather(*list(manager.http_tasks.values()))


async def _ready(value):
    return value


def test_prepare_workspace_rejects_a_symlink_opencode_directory(tmp_path):
    root = tmp_path / "root"
    (root / ".opencode").mkdir(parents=True)
    (root / ".opencode" / "opencode.json").write_text("{}\n")
    target = tmp_path / "task"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (target / ".opencode").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="符号链接"):
        prepare_workspace_local(str(root), str(target))
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_trimmed_event_log_keeps_a_gap_for_a_lagging_cursor():
    gateway = gateway_for()
    log = EventLog(gateway.bus, limit=2)
    await log.touch_cursor(worker_id="wrk", epoch=1, directory="/work", consumer="lag", seq=1)
    for seq in (1, 2, 3):
        await log.append(
            worker_id="wrk",
            epoch=1,
            directory="/work",
            payload={"seq": seq, "directory": "/work", "event": {"type": "server.heartbeat", "n": seq}},
        )
    state = await log._load(log._key("wrk", 1, "/work"))
    assert state["events"][0]["gap"] is True
    assert state["events"][0]["reason"] == "log_trimmed"
    assert state["events"][0]["from"] == 1
    assert any(item.get("event", {}).get("n") == 3 for item in state["events"])


@pytest.mark.asyncio
async def test_event_receive_rejects_a_stale_database_epoch(environment_id):
    gateway = gateway_for()
    enrolled, auth = await bring_online(gateway, environment_id)

    def frame(seq: int):
        message = envelope(
            request_id=f"evt-{seq}",
            worker_instance_id=enrolled.worker_instance_id,
            environment_id=environment_id,
            session_id=None,
            message_type="event",
            payload={"directory": "/srv/app", "seq": seq, "event": {"type": "server.heartbeat"}},
        )
        message.epoch = auth.epoch
        return message

    assert await gateway.record_worker_event(auth, frame(1)) is True
    async with SessionLocal() as session:
        instance = await session.get(WorkerInstance, enrolled.worker_instance_id)
        instance.epoch = auth.epoch + 1
        await session.commit()
    assert await gateway.record_worker_event(auth, frame(2)) is False
    state = await EventLog(gateway.bus, limit=8)._load(
        EventLog(gateway.bus, limit=8)._key(enrolled.worker_instance_id, auth.epoch, "/srv/app")
    )
    assert [item["seq"] for item in state["events"]] == [1]


def test_child_environ_keeps_proxy_ca_and_an_explicit_allowlist():
    cleaned = child_environ(
        {
            "PATH": "/usr/bin",
            "HTTP_PROXY": "http://proxy:8080",
            "https_proxy": "http://proxy:8080",
            "NO_PROXY": "localhost",
            "no_proxy": "localhost",
            "NODE_EXTRA_CA_CERTS": "/etc/ssl/certs/ca.pem",
            "CUSTOM_CA_BUNDLE": "/etc/ssl/custom.pem",
            "NEXUS_WORKER_CHILD_ENV_ALLOW": "CUSTOM_CA_BUNDLE,NEXUS_WORKER_TOKEN,OPENAI_API_KEY",
            "NEXUS_WORKER_TOKEN": "wtk_secret",
            "OPENAI_API_KEY": "sk-secret",
        }
    )
    assert cleaned["HTTP_PROXY"] == "http://proxy:8080"
    assert cleaned["https_proxy"] == "http://proxy:8080"
    assert cleaned["NO_PROXY"] == "localhost"
    assert cleaned["no_proxy"] == "localhost"
    assert cleaned["NODE_EXTRA_CA_CERTS"] == "/etc/ssl/certs/ca.pem"
    assert cleaned["CUSTOM_CA_BUNDLE"] == "/etc/ssl/custom.pem"
    assert "NEXUS_WORKER_TOKEN" not in cleaned
    assert "OPENAI_API_KEY" not in cleaned
    assert "NEXUS_WORKER_CHILD_ENV_ALLOW" not in cleaned


async def _until(predicate) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")
