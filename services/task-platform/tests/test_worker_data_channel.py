"""Request-ready frames, HTTP bodies, event cursors, and the worker mark."""

import asyncio
import json
import subprocess
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config.settings import get_settings
from app.database import SessionLocal, init_database
from app.models import (
    AgentWorker,
    OpenCodeExecutionEnvironment,
    Task,
    WorkerCommand,
    WorkerHttpSlot,
    WorkerInstance,
)
from app.runtime.submission import put_attempt
from app.runtime.worker_channel import mark_worker_channel, read_worker_channel
from app.runtime.worker_client import WorkerOpenCodeClient
from app.worker_agent.connection import WorkerConnectionManager
from app.worker_agent.workspace import prepare_workspace_local
from app.worker_gateway.bus import MemoryRouteBus
from app.worker_gateway.data_channel import DataChannel, request_key, response_key
from app.worker_gateway.events import EventLog
from app.worker_gateway.protocol import WorkerGatewayError, envelope
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


async def _online(gateway, environment_id):
    enrolled, auth = await bring_online(gateway, environment_id)
    return enrolled, auth, gateway.bus.bound[enrolled.worker_instance_id]


@pytest.mark.asyncio
async def test_http_ready_frame_omits_body_and_get_records_fetch(environment_id):
    gateway = gateway_for()
    enrolled, auth, queues = await _online(gateway, environment_id)
    async with SessionLocal() as session:
        task = Task(
            id="T-body",
            title="body",
            task_type="ISSUE",
            status="RUNNING",
            employee_id=(
                await session.scalar(
                    select(OpenCodeExecutionEnvironment.employee_id).where(
                        OpenCodeExecutionEnvironment.id == environment_id
                    )
                )
            ),
            pending_message_id="msg_body000001",
            pending_attempt=1,
        )
        put_attempt(task, "msg_body000001", 1, "req-http-1")
        session.add(task)
        await session.commit()
        result = await gateway.dispatch(
            session,
            worker_instance_id=enrolled.worker_instance_id,
            request_id="req-http-1",
            name="http",
            payload={
                "method": "POST",
                "path": "/session/ses/prompt_async",
                "body": {"messageID": "msg_body000001"},
                "attempt_ref": {"task_id": "T-body", "message_id": "msg_body000001", "attempt": 1},
            },
            session_id="ses",
        )
    frame = await queues.wait_frame()
    assert frame["payload"]["data"]["ready"] is True
    assert "body" not in frame["payload"]["data"]
    assert result.delivered is True
    async with SessionLocal() as session:
        fetched = await DataChannel(gateway).fetch_request(
            session,
            request_id="req-http-1",
            worker_instance_id=enrolled.worker_instance_id,
            epoch=auth.epoch,
        )
        task = await session.get(Task, "T-body")
        command = await session.scalar(select(WorkerCommand).where(WorkerCommand.request_id == "req-http-1"))
    assert "msg_body000001" in fetched["body"]
    assert command.body_fetched_at is not None
    assert task.submission_attempts["msg_body000001:1"]["body_fetched_at"]
    async with SessionLocal() as session:
        instance = await session.get(WorkerInstance, enrolled.worker_instance_id)
        instance.epoch += 1
        await session.commit()
        with pytest.raises(WorkerGatewayError) as error:
            await DataChannel(gateway).fetch_request(
                session,
                request_id="req-http-1",
                worker_instance_id=enrolled.worker_instance_id,
                epoch=auth.epoch,
            )
    assert error.value.code == "epoch_conflict"


@pytest.mark.asyncio
async def test_upload_cas_keeps_the_winner_body(environment_id):
    gateway = gateway_for()
    enrolled, auth, _queues = await _online(gateway, environment_id)
    async with SessionLocal() as session:
        await gateway.dispatch(
            session,
            worker_instance_id=enrolled.worker_instance_id,
            request_id="req-upload",
            name="http",
            payload={"method": "GET", "path": "/session", "body": {}},
        )
    async with SessionLocal() as session:
        stored = await DataChannel(gateway).commit_upload(
            session,
            request_id="req-upload",
            worker_instance_id=enrolled.worker_instance_id,
            epoch=auth.epoch,
            upload_id="up-win",
            http_status=200,
            headers={"x-next-cursor": "cursor-2"},
            content_type="application/json",
            chunk="[]",
            complete=True,
        )
    assert stored["result"]["headers"]["x-next-cursor"] == "cursor-2"
    assert await gateway.bus.get_text(response_key("req-upload", "up-win"))
    async with SessionLocal() as session:
        with pytest.raises(WorkerGatewayError):
            await DataChannel(gateway).commit_upload(
                session,
                request_id="req-upload",
                worker_instance_id=enrolled.worker_instance_id,
                epoch=auth.epoch,
                upload_id="up-lose",
                http_status=200,
                headers={},
                content_type="application/json",
                chunk="other",
                complete=True,
            )
    assert await gateway.bus.get_text(response_key("req-upload", "up-win"))
    assert await gateway.bus.get_text(response_key("req-upload", "up-lose")) is None
    assert await gateway.bus.get_text(request_key("req-upload")) is not None


@pytest.mark.asyncio
async def test_event_gap_coalesces_refresh_and_cursor_pause():
    bus = MemoryRouteBus()
    log = EventLog(bus, limit=2, lease_seconds=30)
    first = await log.append(
        worker_id="wrk", epoch=1, directory="/work", payload={"seq": 1, "event": {"type": "a"}}
    )
    assert first["action"] == "store"
    gap = await log.append(
        worker_id="wrk", epoch=1, directory="/work", payload={"seq": 3, "event": {"type": "c"}}
    )
    assert gap["action"] == "gap"
    assert await log.begin_refresh(worker_id="wrk", epoch=1, directory="/work") is True
    assert await log.begin_refresh(worker_id="wrk", epoch=1, directory="/work") is False
    paced = EventLog(MemoryRouteBus(), limit=2, lease_seconds=30)

    async def store(seq: int, kind: str) -> None:
        await paced.append(
            worker_id="wrk",
            epoch=1,
            directory="/work",
            payload={"seq": seq, "event": {"type": kind}},
        )

    await store(1, "a")
    await store(2, "b")
    assert await paced.control(worker_id="wrk", epoch=1, directory="/work") == "stop_events"
    await store(1, "a")
    await store(2, "b")
    await paced.touch_cursor(worker_id="wrk", epoch=1, directory="/work", consumer="browser:1", seq=0)
    assert await paced.control(worker_id="wrk", epoch=1, directory="/work") == "pause_events"
    await paced.touch_cursor(worker_id="wrk", epoch=1, directory="/work", consumer="browser:1", seq=2)
    assert await paced.control(worker_id="wrk", epoch=1, directory="/work") == "resume_events"


@pytest.mark.asyncio
async def test_http_command_does_not_block_ping():
    from app.worker_agent.connection import AgentConfig
    from app.worker_gateway.protocol import parse_message

    started = asyncio.Event()
    release = asyncio.Event()

    async def local_request(_method, _path, _body):
        started.set()
        await release.wait()
        return {"status": 204, "body": ""}

    async def fetch_body(_request_id, _epoch):
        return {"body": "{}"}

    async def upload_body(_request_id, _epoch, _result):
        return None

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
    manager.fetch_body = fetch_body
    manager.local_request = local_request
    manager.upload_body = upload_body

    class Transport:
        def __init__(self):
            self.sent = []

        async def send(self, message):
            self.sent.append(message)

        async def recv(self):
            raise ConnectionError("closed")

        async def close(self):
            return None

    def framed(name, request_id):
        message = envelope(
            request_id=request_id,
            worker_instance_id="wrk",
            environment_id=1,
            session_id=None,
            message_type="command",
            payload={"name": name, "data": {"method": "POST", "path": "/x", "ready": True}},
        )
        message.epoch = 1
        message.expires_at = 10**12
        return parse_message(json.dumps(message.to_dict()), max_bytes=10**6)

    transport = Transport()
    manager._start_http(transport, framed("http", "req-http"), 1)
    await asyncio.wait_for(started.wait(), 1)
    await manager._handle_command(transport, framed("ping", "ping-1"), 1)
    assert any(item["payload"].get("result", {}).get("pong") for item in transport.sent)
    release.set()
    await asyncio.gather(*manager.http_tasks.values())


def test_prepare_workspace_copies_opencode_config(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "opencode.json").write_text('{"model":"deepseek-flash"}\n')
    target = tmp_path / "task"
    result = prepare_workspace_local(str(root), str(target))
    assert result["created"] == "true"
    assert json.loads((target / "opencode.json").read_text())["model"] == "deepseek-flash"


def _commit_repo(path):
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init"], check=True, capture_output=True)
    subprocess.run(
        [
            "git", "-C", str(path),
            "-c", "user.email=astra@localhost",
            "-c", "user.name=Astra",
            "commit", "--allow-empty", "-m", "init",
        ],
        check=True,
        capture_output=True,
    )


def test_prepare_workspace_adds_a_detached_worktree_once(tmp_path):
    root = tmp_path / "root"
    _commit_repo(root)
    (root / "opencode.json").write_text('{"model":"deepseek-flash"}\n')
    subprocess.run(["git", "-C", str(root), "add", "opencode.json"], check=True, capture_output=True)
    subprocess.run(
        [
            "git", "-C", str(root),
            "-c", "user.email=astra@localhost",
            "-c", "user.name=Astra",
            "commit", "-m", "config",
        ],
        check=True,
        capture_output=True,
    )
    target = tmp_path / "tasks" / "T1"
    first = prepare_workspace_local(str(root), str(target))
    assert first["created"] == "true"
    assert (target / ".git").exists()
    (target / "opencode.json").write_text('{"model":"local"}\n')
    second = prepare_workspace_local(str(root), str(target))
    assert second["created"] == "false"
    assert json.loads((target / "opencode.json").read_text())["model"] == "local"
    listed = subprocess.check_output(
        ["git", "-C", str(root), "worktree", "list", "--porcelain"], text=True
    )
    assert f"worktree {target.resolve()}" in listed


def test_prepare_workspace_rejects_reuse_symlink_subdir_and_foreign_repo(tmp_path):
    root = tmp_path / "root"
    _commit_repo(root)
    with pytest.raises(RuntimeError, match="请显式重新执行"):
        prepare_workspace_local(str(root), str(tmp_path / "missing"), reuse_existing=True)
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(RuntimeError, match="符号链接"):
        prepare_workspace_local(str(root), str(link))
    nested = root / "nested"
    nested.mkdir()
    with pytest.raises(RuntimeError, match="不是目录根"):
        prepare_workspace_local(str(root), str(nested))
    other = tmp_path / "other"
    _commit_repo(other)
    with pytest.raises(RuntimeError, match="不属于当前仓库"):
        prepare_workspace_local(str(root), str(other))


def _direct_switch_result() -> dict[str, object]:
    settings = get_settings()
    return {
        "gateway_url": settings.remote_gateway_local_url + "/",
        "mcp_url": settings.mcp_resource_server_url + "/",
        "reloaded": True,
        "verified": True,
    }


@pytest.mark.asyncio
async def test_worker_channel_switch_checks_effective_config_and_occupancy(environment_id):
    async with SessionLocal() as session:
        assert await read_worker_channel(session, environment_id) == "ssh"
        calls: list[str] = []

        async def fail():
            calls.append("fail")
            raise RuntimeError("内网 Gateway 或 MCP 探测失败")

        with pytest.raises(HTTPException) as blocked:
            await mark_worker_channel(session, environment_id, switch=fail)
        assert blocked.value.status_code == 409
        assert "探测失败" in blocked.value.detail
        assert await read_worker_channel(session, environment_id) == "ssh"

        async def mismatch():
            calls.append("mismatch")
            result = _direct_switch_result()
            result["gateway_url"] = "http://127.0.0.1:19999"
            return result

        with pytest.raises(HTTPException) as differed:
            await mark_worker_channel(session, environment_id, switch=mismatch)
        assert differed.value.status_code == 409
        assert "Gateway" in differed.value.detail
        assert await read_worker_channel(session, environment_id) == "ssh"

        async def ok():
            calls.append("ok")
            return _direct_switch_result()

        marked = await mark_worker_channel(session, environment_id, switch=ok)
        assert marked.worker_channel == "worker"
        employee_id = marked.employee_id
        session.add(
            Task(
                id="T-hold",
                title="hold",
                task_type="ISSUE",
                status="FAILED",
                employee_id=employee_id,
                claimed_at=marked.updated_at,
            )
        )
        await session.commit()
        with pytest.raises(HTTPException) as occupied:
            await mark_worker_channel(session, environment_id, switch=ok)
        assert occupied.value.status_code == 409
        assert calls == ["fail", "mismatch", "ok"]


@pytest.mark.asyncio
async def test_worker_channel_without_an_online_worker_stays_ssh(environment_id, monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("离线环境不能下发切换")

    monkeypatch.setattr(WorkerOpenCodeClient, "run_command", forbidden)
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as offline:
            await mark_worker_channel(session, environment_id)
        assert offline.value.status_code == 409
        assert "不在线" in offline.value.detail
        assert await read_worker_channel(session, environment_id) == "ssh"


@pytest.mark.asyncio
async def test_worker_channel_dispatches_workspace_and_port_only(environment_id, monkeypatch):
    gateway = gateway_for()
    await _online(gateway, environment_id)
    captured: dict[str, object] = {}

    async def fake_run(_self, name, data, *, session_id=None):
        captured["name"] = name
        captured["data"] = dict(data)
        captured["session_id"] = session_id
        return _direct_switch_result()

    monkeypatch.setattr(WorkerOpenCodeClient, "run_command", fake_run)
    async with SessionLocal() as session:
        environment = await session.get(OpenCodeExecutionEnvironment, environment_id)
        assert environment is not None
        expected = {"workspace": environment.workspace_root, "port": environment.opencode_server_port}
        marked = await mark_worker_channel(session, environment_id)
        assert marked.worker_channel == "worker"
    assert captured["name"] == "switch_channel"
    assert captured["data"] == expected
    assert "gateway_url" not in captured["data"]
    assert "mcp_url" not in captured["data"]


@pytest.mark.asyncio
async def test_http_inflight_stays_pending_past_eight(environment_id):
    gateway = gateway_for()
    enrolled, auth, _queues = await _online(gateway, environment_id)
    for index in range(8):
        async with SessionLocal() as session:
            result = await gateway.dispatch(
                session,
                worker_instance_id=enrolled.worker_instance_id,
                request_id=f"req-cap-{index}",
                name="http",
                payload={"method": "GET", "path": "/global/health", "body": {}},
            )
        assert result.delivered is True
    async with SessionLocal() as session:
        held = await gateway.dispatch(
            session,
            worker_instance_id=enrolled.worker_instance_id,
            request_id="req-cap-held",
            name="http",
            payload={"method": "GET", "path": "/global/health", "body": {}},
        )
        assert held.delivered is False
        row = await session.scalar(select(WorkerCommand).where(WorkerCommand.request_id == "req-cap-held"))
    assert row.status == "pending"
    assert row.delivery_state == "unpublished"
    async with SessionLocal() as session:
        await DataChannel(gateway).commit_upload(
            session,
            request_id="req-cap-0",
            worker_instance_id=enrolled.worker_instance_id,
            epoch=auth.epoch,
            upload_id="up-cap",
            http_status=200,
            headers={},
            content_type="application/json",
            chunk="{}",
            complete=True,
        )
    assert await gateway.dispatch_due(enrolled.worker_instance_id, auth.epoch) == 1


@pytest.mark.asyncio
async def test_http_claims_stop_at_eight_under_concurrency(environment_id):
    gateway = gateway_for()
    enrolled, auth, _queues = await _online(gateway, environment_id)

    async def claim(index: int):
        async with SessionLocal() as session:
            return await gateway.dispatch(
                session,
                worker_instance_id=enrolled.worker_instance_id,
                request_id=f"req-race-{index}",
                name="http",
                payload={"method": "GET", "path": "/global/health", "body": {}},
            )

    results = await asyncio.gather(*(claim(index) for index in range(12)))
    assert sum(result.delivered for result in results) == 8
    async with SessionLocal() as session:
        slot = await session.get(WorkerHttpSlot, (enrolled.worker_instance_id, auth.epoch))
        rows = (
            await session.scalars(
                select(WorkerCommand).where(WorkerCommand.request_id.like("req-race-%"))
            )
        ).all()
    assert slot is not None
    assert slot.inflight == 8
    assert sum(row.slot_held for row in rows) == 8
    held_back = [row for row in rows if not row.slot_held]
    assert len(held_back) == 4
    assert {row.status for row in held_back} == {"pending"}
    assert {row.delivery_state for row in held_back} == {"unpublished"}


@pytest.mark.asyncio
async def test_same_request_id_rejects_a_different_session(environment_id):
    gateway = gateway_for()
    enrolled, _auth, _queues = await _online(gateway, environment_id)
    async with SessionLocal() as session:
        await gateway.dispatch(
            session,
            worker_instance_id=enrolled.worker_instance_id,
            request_id="req-session",
            name="ping",
            payload={},
            session_id="ses-a",
        )
        with pytest.raises(WorkerGatewayError) as error:
            await gateway.dispatch(
                session,
                worker_instance_id=enrolled.worker_instance_id,
                request_id="req-session",
                name="ping",
                payload={},
                session_id="ses-b",
            )
    assert error.value.code == "duplicate_request"


@pytest.mark.asyncio
async def test_slow_cursor_queues_pause_events(environment_id):
    gateway = gateway_for()
    enrolled, auth, queues = await _online(gateway, environment_id)
    gateway.settings = gateway.settings.model_copy(update={"worker_gateway_event_queue_size": 2})
    log = EventLog(gateway.bus, limit=2)
    await log.append(
        worker_id=enrolled.worker_instance_id,
        epoch=auth.epoch,
        directory="/work",
        payload={"seq": 1, "event": {"type": "a"}},
    )
    await log.append(
        worker_id=enrolled.worker_instance_id,
        epoch=auth.epoch,
        directory="/work",
        payload={"seq": 2, "event": {"type": "b"}},
    )
    await log.touch_cursor(
        worker_id=enrolled.worker_instance_id,
        epoch=auth.epoch,
        directory="/work",
        consumer="browser:1",
        seq=0,
    )
    assert await gateway.publish_event_control(auth, "/work") == "pause_events"
    frame = await queues.wait_frame()
    assert frame["payload"]["name"] == "pause_events"
    assert frame["payload"]["data"]["directory"] == "/work"
