"""Worker-marked environments use the Gateway and leave SSH forwarding in place."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.config.settings import get_settings
from app.database import SessionLocal, init_database
from app.domain import ExecutionMode
from app.executors.opencode_remote_server import RemoteOpenCodeServerConnection
from app.models import AgentWorker, OpenCodeExecutionEnvironment
from app.runtime.environment import ExecutionEnvironmentRuntime
from app.runtime.session_connection import connect
from app.runtime.worker_client import WorkerOpenCodeClient
from app.worker_agent.connection import handle_command
from app.worker_agent.local_opencode import gateway_http_base
from app.worker_agent.supervisor import RuntimeSupervisor
from app.worker_gateway.data_channel import DataChannel
from app.worker_gateway.protocol import envelope, parse_message
from tests.test_worker_gateway import bring_online, gateway_for


@pytest.fixture
async def environment_id() -> int:
    await init_database()
    async with SessionLocal() as session:
        employee = AgentWorker(id=f"emp-{uuid4().hex[:8]}", name="远程员工", role="执行")
        session.add(employee)
        await session.flush()
        environment = OpenCodeExecutionEnvironment(
            employee_id=employee.id, mode="LINUX_REMOTE", name="云执行机", worker_channel="ssh"
        )
        session.add(environment)
        await session.commit()
        return environment.id


@pytest.mark.asyncio
async def test_worker_connect_does_not_open_ssh(environment_id, monkeypatch):
    await _mark(environment_id, "worker")
    called = False

    async def opened(self, environment):
        nonlocal called
        called = True
        raise AssertionError(environment.host)

    monkeypatch.setattr(ExecutionEnvironmentRuntime, "open_session", opened)
    environment = await _load(environment_id)
    async with connect(environment, start=False) as client:
        assert isinstance(client, WorkerOpenCodeClient)
    assert called is False


@pytest.mark.asyncio
async def test_unmarked_connect_does_not_open_ssh(environment_id, monkeypatch):
    await _mark(environment_id, "ssh")
    called = False

    async def opened(self, environment):
        nonlocal called
        called = True
        raise AssertionError(environment.host)

    monkeypatch.setattr(ExecutionEnvironmentRuntime, "open_session", opened)
    environment = await _load(environment_id)
    with pytest.raises(RuntimeError, match="尚未安装 Worker"):
        async with connect(environment, start=False):
            pass
    assert called is False


@pytest.mark.asyncio
async def test_worker_http_exchange_returns_cursor_headers(environment_id):
    gateway = gateway_for()
    enrolled, auth = await bring_online(gateway, environment_id)
    await _mark(environment_id, "worker")
    queues = gateway.bus.bound[enrolled.worker_instance_id]

    async def answer():
        frame = await queues.wait_frame()
        async with SessionLocal() as session:
            fetched = await DataChannel(gateway).fetch_request(
                session,
                request_id=frame["request_id"],
                worker_instance_id=enrolled.worker_instance_id,
                epoch=auth.epoch,
            )
        assert fetched["query"]["directory"] == "/work"
        async with SessionLocal() as session:
            await DataChannel(gateway).commit_upload(
                session,
                request_id=frame["request_id"],
                worker_instance_id=enrolled.worker_instance_id,
                epoch=auth.epoch,
                upload_id="up-call",
                http_status=200,
                headers={"x-next-cursor": "cursor-9", "link": "<next>"},
                content_type="application/json",
                chunk=json.dumps([{"id": "ses"}]),
                complete=True,
            )

    running = asyncio.create_task(answer())
    client = WorkerOpenCodeClient(
        environment_id=environment_id, gateway=gateway, timeout_seconds=5
    )
    payload, headers = await client.request(
        "GET", "/session", directory=Path("/work"), include_headers=True
    )
    await running
    assert payload == [{"id": "ses"}]
    assert headers["x-next-cursor"] == "cursor-9"
    assert headers["link"] == "<next>"


@pytest.mark.asyncio
async def test_worker_http_records_the_session_and_keeps_the_body_off_the_frame(environment_id, monkeypatch):
    import httpx

    gateway = gateway_for()
    enrolled, auth = await bring_online(gateway, environment_id)
    queues = gateway.bus.bound[enrolled.worker_instance_id]

    class Refused:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("worker transport dialed base_url")

    monkeypatch.setattr(httpx, "AsyncClient", Refused)

    async def answer():
        frame = await queues.wait_frame()
        assert frame["session_id"] == "ses_bound"
        data = frame["payload"]["data"]
        assert data["ready"] is True
        assert data["task_id"] == "task-1"
        assert "body" not in data
        async with SessionLocal() as session:
            fetched = await DataChannel(gateway).fetch_request(
                session,
                request_id=frame["request_id"],
                worker_instance_id=enrolled.worker_instance_id,
                epoch=auth.epoch,
            )
            assert fetched["query"]["directory"] == "/ws"
            assert json.loads(fetched["body"]) == {"hidden": True}
            await DataChannel(gateway).commit_upload(
                session,
                request_id=frame["request_id"],
                worker_instance_id=enrolled.worker_instance_id,
                epoch=auth.epoch,
                upload_id="up-session",
                http_status=200,
                headers={"x-next-cursor": "cursor-session", "x-has-more": "false"},
                content_type="application/json",
                chunk='{"ok":true}',
                complete=True,
            )

    running = asyncio.create_task(answer())
    client = WorkerOpenCodeClient(environment_id=environment_id, gateway=gateway, timeout_seconds=5)
    status, content, headers = await client.exchange(
        "GET",
        "/file",
        params={"directory": "/ws"},
        body={"hidden": True},
        session_id="ses_bound",
        task_id="task-1",
    )
    await running
    assert status == 200
    assert json.loads(content) == {"ok": True}
    assert headers["x-next-cursor"] == "cursor-session"
    assert headers["x-has-more"] == "false"


@pytest.mark.asyncio
async def test_prepare_workspace_command_copies_config(environment_id, tmp_path):
    gateway = gateway_for()
    enrolled, auth = await bring_online(gateway, environment_id)
    root = tmp_path / "root"
    root.mkdir()
    (root / "opencode.json").write_text('{"model":"deepseek-flash"}\n')
    target = tmp_path / ".astra-tasks" / "emp" / "task"
    queues = gateway.bus.bound[enrolled.worker_instance_id]

    async def answer():
        frame = await queues.wait_frame()
        message = parse_message(json.dumps(frame), max_bytes=10**6)
        result, error = handle_command(message, RuntimeSupervisor(), workspace_root=str(root))
        reply = envelope(
            request_id=message.request_id,
            worker_instance_id=message.worker_instance_id,
            environment_id=message.environment_id,
            session_id=message.session_id,
            message_type="response",
            payload={"stage": "result", "ok": error is None, "result": result, "error_code": error},
        )
        reply.epoch = auth.epoch
        async with SessionLocal() as session:
            assert await gateway.apply_response(
                session,
                worker_instance_id=enrolled.worker_instance_id,
                epoch=auth.epoch,
                message=reply,
            )
        await gateway.bus.publish_response(message.request_id, reply.to_dict())

    running = asyncio.create_task(answer())
    client = WorkerOpenCodeClient(
        environment_id=environment_id, gateway=gateway, timeout_seconds=5
    )
    result = await client.run_command(
        "prepare_workspace",
        {"root": str(root), "target": str(target), "reuse_existing": False},
    )
    await running
    assert result["created"] == "true"
    assert json.loads((target / "opencode.json").read_text())["model"] == "deepseek-flash"


@pytest.mark.asyncio
async def test_remote_connection_on_worker_does_not_start_ssh(environment_id, monkeypatch):
    await _mark(environment_id, "worker")

    async def opened(self, environment):
        raise AssertionError("ssh opened")

    monkeypatch.setattr(ExecutionEnvironmentRuntime, "open_session", opened)
    environment = await _load(environment_id)
    connection = RemoteOpenCodeServerConnection(
        environment, get_settings(), ExecutionEnvironmentRuntime()
    )
    with pytest.raises(RuntimeError, match="启动方式=Worker"):
        async with connection:
            pass


async def _mark(environment_id: int, channel: str) -> None:
    await init_database()
    async with SessionLocal() as session:
        environment = await session.get(OpenCodeExecutionEnvironment, environment_id)
        environment.worker_channel = channel
        environment.mode = ExecutionMode.LINUX_REMOTE
        await session.commit()


async def _load(environment_id: int) -> OpenCodeExecutionEnvironment:
    async with SessionLocal() as session:
        stored = await session.get(OpenCodeExecutionEnvironment, environment_id)
        return OpenCodeExecutionEnvironment(
            id=stored.id,
            employee_id=stored.employee_id,
            mode=stored.mode,
            name=stored.name,
            host=stored.host,
            port=stored.port,
            username=stored.username,
            opencode_server_port=stored.opencode_server_port,
            opencode_path=stored.opencode_path,
            model_name=stored.model_name,
            workspace_root=stored.workspace_root,
            worker_channel=stored.worker_channel,
            status=stored.status,
        )


def test_gateway_http_base_is_the_origin():
    assert (
        gateway_http_base("ws://127.0.0.1:8877/api/worker-gateway/connect")
        == "http://127.0.0.1:8877"
    )
    assert gateway_http_base("wss://worker.example/connect") == "https://worker.example"
