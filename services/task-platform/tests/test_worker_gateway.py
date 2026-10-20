import asyncio
import os
import random
import time
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from app.config.settings import get_settings
from app.database import SessionLocal, init_database
from app.gateway.security import verify_gateway_key
from app.main import app
from app.models import AgentWorker, OpenCodeExecutionEnvironment, WorkerCommand, WorkerInstance, utcnow
from app.worker_agent.connection import (
    AgentConfig,
    WorkerConnectionManager,
    handle_command,
    load_agent_config,
    reconnect_delay,
)
from app.worker_agent.supervisor import RuntimeSupervisor
from app.worker_gateway.bus import (
    SINGLE_PROCESS_WARNING,
    ConnectionQueues,
    MemoryRouteBus,
    RedisRouteBus,
    resolve_worker_gateway_bus,
)
from app.worker_gateway.protocol import WorkerGatewayError, dump_message, envelope, parse_message
from app.worker_gateway.service import HeartbeatMonitor, WorkerGateway

REDIS_URL = os.environ.get("ASTRA_TEST_REDIS_URL")
ADMIN_HEADERS = {"X-Astra-Worker-Gateway-Key": "wgk_test"}


def live_frame(frame: dict, epoch: int, ttl: int = 120) -> dict:
    return {**frame, "epoch": epoch, "expires_at": int(time.time()) + ttl}


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.accepted = False
        self.close_code: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        item = await self.incoming.get()
        if item is None:
            raise WebSocketDisconnect()
        return item

    async def close(self, code: int = 1000) -> None:
        self.close_code = code
        await self.incoming.put(None)


class RecordingBus(MemoryRouteBus):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.published: list[str] = []
        self.status_at_publish: list[str | None] = []

    async def publish_command(self, worker_instance_id: str, frame: dict) -> bool:
        async with SessionLocal() as session:
            row = await session.scalar(
                select(WorkerCommand).where(WorkerCommand.request_id == frame["request_id"])
            )
            self.status_at_publish.append(None if row is None else row.status)
        self.published.append(frame["request_id"])
        return await super().publish_command(worker_instance_id, frame)


class ScriptedTransport:
    def __init__(self, inbound: list[dict]) -> None:
        self.inbound = list(inbound)
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    async def recv(self) -> dict:
        if not self.inbound:
            raise ConnectionError("closed")
        return self.inbound.pop(0)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
async def environment_id() -> int:
    await init_database()
    async with SessionLocal() as session:
        employee = AgentWorker(id=f"emp-{uuid4().hex[:8]}", name="远程员工", role="执行")
        session.add(employee)
        await session.flush()
        environment = OpenCodeExecutionEnvironment(
            employee_id=employee.id, mode="LINUX_REMOTE", name="云执行机"
        )
        session.add(environment)
        await session.commit()
        return environment.id


def gateway_for(bus: MemoryRouteBus | None = None, **settings) -> WorkerGateway:
    copied = get_settings().model_copy(update=settings) if settings else get_settings()
    return WorkerGateway(settings=copied, bus=bus or MemoryRouteBus())


def auth_message(worker_id: str, environment_id: int, token: str, request_id: str = "auth-1"):
    return envelope(
        request_id=request_id,
        worker_instance_id=worker_id,
        environment_id=environment_id,
        session_id=None,
        message_type="command",
        payload={"name": "authenticate", "token": token, "agent_version": "0.1.0"},
    )


async def enroll(gateway: WorkerGateway, environment_id: int):
    async with SessionLocal() as session:
        return await gateway.enroll(session, environment_id)


async def authenticate(
    gateway: WorkerGateway, environment_id: int, worker_id: str, token: str, request_id: str
):
    async with SessionLocal() as session:
        return await gateway.authenticate(
            session, auth_message(worker_id, environment_id, token, request_id), token
        )


async def bring_online(gateway: WorkerGateway, environment_id: int):
    enrolled = await enroll(gateway, environment_id)
    auth = await authenticate(
        gateway, environment_id, enrolled.worker_instance_id, enrolled.token, "auth-online"
    )
    assert await gateway.bus.set_owner(auth.worker_instance_id, gateway.owner_id, auth.epoch) is True
    queues = await gateway.bus.bind(
        auth.worker_instance_id, epoch=auth.epoch, owner_id=gateway.owner_id
    )
    assert queues is not None
    return enrolled, auth


async def dispatch(gateway: WorkerGateway, worker_id: str, request_id: str, name: str = "ping", payload=None):
    async with SessionLocal() as session:
        return await gateway.dispatch(
            session,
            worker_instance_id=worker_id,
            request_id=request_id,
            name=name,
            payload={} if payload is None else payload,
        )


async def load_instance(worker_id: str) -> WorkerInstance:
    async with SessionLocal() as session:
        instance = await session.get(WorkerInstance, worker_id)
        assert instance is not None
        return instance


async def load_command(request_id: str) -> WorkerCommand:
    async with SessionLocal() as session:
        command = await session.scalar(select(WorkerCommand).where(WorkerCommand.request_id == request_id))
        assert command is not None
        return command


async def wait_until(predicate, timeout: float = 2) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


async def test_enroll_stores_digest_and_rejects_bad_token(environment_id: int):
    gateway = gateway_for()
    enrolled = await enroll(gateway, environment_id)
    stored = await load_instance(enrolled.worker_instance_id)

    assert stored.token_digest != enrolled.token
    assert verify_gateway_key(enrolled.token, stored.token_digest)
    assert stored.epoch == 0
    assert stored.status == "offline"

    with pytest.raises(WorkerGatewayError) as caught:
        await authenticate(gateway, environment_id, enrolled.worker_instance_id, "wtk_wrong", "auth-bad")
    assert caught.value.code == "unauthorized"
    assert (await load_instance(enrolled.worker_instance_id)).epoch == 0

    mismatch = auth_message(enrolled.worker_instance_id, environment_id + 1, enrolled.token, "auth-mismatch")
    async with SessionLocal() as session:
        with pytest.raises(WorkerGatewayError) as mismatch_error:
            await gateway.authenticate(session, mismatch, enrolled.token)
    assert mismatch_error.value.code == "unauthorized"


async def test_reenroll_rotates_token_and_keeps_instance_id(environment_id: int):
    gateway = gateway_for()
    first = await enroll(gateway, environment_id)
    second = await enroll(gateway, environment_id)

    assert second.rotated is True
    assert second.worker_instance_id == first.worker_instance_id
    assert second.token != first.token
    assert second.epoch == 1
    with pytest.raises(WorkerGatewayError):
        await authenticate(gateway, environment_id, first.worker_instance_id, first.token, "auth-old")
    auth = await authenticate(gateway, environment_id, second.worker_instance_id, second.token, "auth-new")
    assert auth.epoch == 2


async def test_command_is_persisted_before_publish_and_duplicate_is_not_redeilvered(environment_id: int):
    bus = RecordingBus()
    gateway = gateway_for(bus)
    enrolled, _auth = await bring_online(gateway, environment_id)

    first = await dispatch(gateway, enrolled.worker_instance_id, "req-1")
    second = await dispatch(gateway, enrolled.worker_instance_id, "req-1")

    assert first.delivered is True
    assert first.replayed is False
    assert bus.status_at_publish == ["pending"]
    assert bus.published == ["req-1"]
    assert second.replayed is True
    assert second.command.id == first.command.id
    assert (await load_command("req-1")).status == "pending"


async def test_offline_dispatch_is_persisted_as_failed_without_publish(environment_id: int):
    bus = RecordingBus()
    gateway = gateway_for(bus)
    enrolled = await enroll(gateway, environment_id)

    result = await dispatch(gateway, enrolled.worker_instance_id, "req-offline")

    assert result.delivered is False
    assert result.error_code == "offline"
    assert bus.published == []
    stored = await load_command("req-offline")
    assert stored.status == "failed"
    assert stored.error_code == "offline"


async def test_stale_epoch_response_cannot_overwrite_and_old_disconnect_keeps_new_epoch(environment_id: int):
    gateway = gateway_for()
    enrolled, auth = await bring_online(gateway, environment_id)
    await dispatch(gateway, enrolled.worker_instance_id, "req-epoch")
    async with SessionLocal() as session:
        later = await gateway.authenticate(
            session,
            auth_message(enrolled.worker_instance_id, environment_id, enrolled.token, "auth-2"),
            enrolled.token,
        )
    stale = envelope(
        request_id="req-epoch",
        worker_instance_id=enrolled.worker_instance_id,
        environment_id=environment_id,
        session_id=None,
        message_type="response",
        payload={"stage": "result", "ok": True, "result": {"pong": True}, "error_code": None},
    )
    async with SessionLocal() as session:
        applied = await gateway.apply_response(
            session,
            worker_instance_id=enrolled.worker_instance_id,
            epoch=auth.epoch,
            message=stale,
        )
    assert applied is False
    assert (await load_command("req-epoch")).status == "pending"

    async with SessionLocal() as session:
        await gateway.disconnect(session, enrolled.worker_instance_id, auth.epoch)
    current = await load_instance(enrolled.worker_instance_id)
    assert current.epoch == later.epoch
    assert current.status == "online"


async def test_ack_then_result_and_late_ack_does_not_downgrade(environment_id: int):
    gateway = gateway_for()
    enrolled, auth = await bring_online(gateway, environment_id)
    await dispatch(gateway, enrolled.worker_instance_id, "req-ack")

    async def respond(stage: str, result: dict | None = None):
        message = envelope(
            request_id="req-ack",
            worker_instance_id=enrolled.worker_instance_id,
            environment_id=environment_id,
            session_id=None,
            message_type="response",
            payload={"stage": stage, "ok": True, "result": result, "error_code": None},
        )
        async with SessionLocal() as session:
            return await gateway.apply_response(
                session,
                worker_instance_id=enrolled.worker_instance_id,
                epoch=auth.epoch,
                message=message,
            )

    assert await respond("ack") is True
    assert (await load_command("req-ack")).status == "accepted"
    assert await respond("result", {"pong": True}) is True
    assert (await load_command("req-ack")).status == "succeeded"
    assert await respond("ack") is False
    assert (await load_command("req-ack")).status == "succeeded"


async def test_queue_full_keeps_command_pending_and_oversized_frame_is_rejected(environment_id: int):
    bus = MemoryRouteBus(control_limit=1)
    gateway = gateway_for(bus)
    enrolled, _auth = await bring_online(gateway, environment_id)
    assert await bus.bound[enrolled.worker_instance_id].put_control({"request_id": "filler"}) is True

    queued = await dispatch(gateway, enrolled.worker_instance_id, "req-full")
    assert queued.delivered is False
    assert queued.error_code == "queue_full"
    assert (await load_command("req-full")).status == "pending"

    small = gateway_for(MemoryRouteBus(), worker_gateway_max_frame_bytes=1024)
    enrolled_small, _auth_small = await bring_online(small, environment_id)
    with pytest.raises(WorkerGatewayError) as caught:
        await dispatch(small, enrolled_small.worker_instance_id, "req-big", payload={"blob": "x" * 2000})
    assert caught.value.code == "frame_too_large"
    async with SessionLocal() as session:
        stored = await session.scalar(select(WorkerCommand).where(WorkerCommand.request_id == "req-big"))
        assert stored is None


async def test_heartbeat_timeout_marks_offline_and_expired_result_is_ignored(environment_id: int):
    gateway = gateway_for()
    enrolled, auth = await bring_online(gateway, environment_id)
    await gateway.sweep(now=utcnow())
    assert (await load_instance(enrolled.worker_instance_id)).status == "online"

    async with SessionLocal() as session:
        instance = await session.get(WorkerInstance, enrolled.worker_instance_id)
        assert instance is not None
        instance.last_heartbeat_at = utcnow() - timedelta(seconds=31)
        await session.commit()
    assert await gateway.sweep(now=utcnow()) >= 1
    assert (await load_instance(enrolled.worker_instance_id)).status == "offline"

    async with SessionLocal() as session:
        instance = await session.get(WorkerInstance, enrolled.worker_instance_id)
        assert instance is not None
        instance.status = "online"
        instance.last_heartbeat_at = utcnow()
        session.add(
            WorkerCommand(
                request_id="req-expire",
                worker_instance_id=enrolled.worker_instance_id,
                environment_id=environment_id,
                name="ping",
                payload={},
                status="pending",
                epoch=auth.epoch,
                deadline_at=utcnow() - timedelta(seconds=1),
            )
        )
        await session.commit()
    await gateway.sweep(now=utcnow())
    assert (await load_command("req-expire")).status == "expired"
    message = envelope(
        request_id="req-expire",
        worker_instance_id=enrolled.worker_instance_id,
        environment_id=environment_id,
        session_id=None,
        message_type="response",
        payload={"stage": "result", "ok": True, "result": {"late": True}, "error_code": None},
    )
    async with SessionLocal() as session:
        instance = await session.get(WorkerInstance, enrolled.worker_instance_id)
        assert instance is not None
        instance.epoch = auth.epoch
        instance.status = "online"
        await session.commit()
        applied = await gateway.apply_response(
            session,
            worker_instance_id=enrolled.worker_instance_id,
            epoch=auth.epoch,
            message=message,
        )
    assert applied is False
    assert (await load_command("req-expire")).status == "expired"


async def test_socket_roundtrip_supersede_and_event_backpressure(environment_id: int):
    gateway = gateway_for(worker_gateway_event_queue_size=2)
    enrolled = await enroll(gateway, environment_id)
    socket = FakeWebSocket()
    task = asyncio.create_task(gateway.serve_socket(socket))
    await socket.incoming.put(dump_message(auth_message(
        enrolled.worker_instance_id, environment_id, enrolled.token
    )))
    await wait_until(lambda: socket.sent)
    ack = parse_message(socket.sent[0], max_bytes=10000)
    assert ack.payload["ok"] is True
    assert ack.payload["epoch"] == 1
    assert "token" not in ack.payload

    delivered = await dispatch(gateway, enrolled.worker_instance_id, "req-socket")
    assert delivered.delivered is True
    await wait_until(lambda: len(socket.sent) >= 2)
    command = parse_message(socket.sent[-1], max_bytes=10000)
    assert command.payload["name"] == "ping"
    for stage, result in (("ack", None), ("result", {"pong": True})):
        await socket.incoming.put(dump_message(envelope(
            request_id="req-socket",
            worker_instance_id=enrolled.worker_instance_id,
            environment_id=environment_id,
            session_id=None,
            message_type="response",
            payload={"stage": stage, "ok": True, "result": result, "error_code": None},
        )))
    for _attempt in range(50):
        if (await load_command("req-socket")).status == "succeeded":
            break
        await asyncio.sleep(0.02)
    assert (await load_command("req-socket")).status == "succeeded"

    for number in range(3):
        await socket.incoming.put(dump_message(envelope(
            request_id=f"event-{number}",
            worker_instance_id=enrolled.worker_instance_id,
            environment_id=environment_id,
            session_id=None,
            message_type="event",
            payload={"number": number},
        )))
    await wait_until(lambda: gateway.sockets[enrolled.worker_instance_id].dropped_events == 1)
    lease = gateway.sockets[enrolled.worker_instance_id]
    assert len(lease.inbound_events) == 2

    replacement = FakeWebSocket()
    replacement_task = asyncio.create_task(gateway.serve_socket(replacement))
    await replacement.incoming.put(dump_message(auth_message(
        enrolled.worker_instance_id, environment_id, enrolled.token, "auth-2"
    )))
    await wait_until(lambda: replacement.sent)
    assert parse_message(replacement.sent[0], max_bytes=10000).payload["epoch"] == 2
    await asyncio.wait_for(task, 2)
    assert socket.close_code == 4000
    assert (await load_instance(enrolled.worker_instance_id)).status == "online"

    await replacement.close()
    await asyncio.wait_for(replacement_task, 2)
    assert (await load_instance(enrolled.worker_instance_id)).status == "offline"
    assert (await load_instance(enrolled.worker_instance_id)).epoch == 2


async def test_control_frames_preempt_events_and_memory_buses_do_not_share_owner():
    queues = ConnectionQueues(control_limit=2, event_limit=2)
    assert await queues.put_event({"kind": "event"}) is True
    assert await queues.put_control({"kind": "command"}) is True
    assert (await queues.wait_frame())["kind"] == "command"
    assert (await queues.wait_frame())["kind"] == "event"
    assert await queues.put_event({"n": 1}) is True
    assert await queues.put_event({"n": 2}) is True
    assert await queues.put_event({"n": 3}) is False
    assert queues.dropped_events == 1

    left = MemoryRouteBus()
    right = MemoryRouteBus()
    await left.set_owner("wrk_shared", "gw-a", 2)
    assert await right.get_owner("wrk_shared") is None
    assert await left.clear_owner("wrk_shared", 1, None) is False
    assert await left.clear_owner("wrk_shared", 2, "gw-b") is False
    assert await left.get_owner("wrk_shared") == ("gw-a", 2)
    assert await left.clear_owner("wrk_shared", 2, "gw-a") is True


def test_heartbeat_monitor_closes_after_consecutive_misses_and_pong_resets():
    monitor = HeartbeatMonitor(2)
    monitor.track("a")
    assert monitor.timeout("a") is False
    assert monitor.timeout("missing") is True
    monitor.track("c")
    assert monitor.resolve("c") is True
    assert monitor.misses == 0


def test_reconnect_delay_is_exponential_and_capped():
    first = reconnect_delay(1, 1, 30, random.Random(0))
    third = reconnect_delay(3, 1, 30, random.Random(0))
    assert 1 <= first <= 2
    assert 4 <= third <= 5
    assert reconnect_delay(6, 1, 30, random.Random(0)) == 30


def test_disconnect_does_not_restart_opencode_and_reconnect_uses_new_epoch():
    supervisor = RuntimeSupervisor()
    supervisor.on_connection_lost()
    assert supervisor.restart_count == 0
    supervisor.on_process_exit(1)
    assert supervisor.restart_count == 1
    supervisor.restart_count = 0

    config = AgentConfig(
        gateway_url="ws://nexus.internal/api/worker-gateway/connect",
        worker_instance_id="wrk_agent",
        environment_id=7,
        token="wtk_test",
        ping_seconds=3600,
    )

    def reply(epoch: int) -> dict:
        return envelope(
            request_id="auth-wrk_agent",
            worker_instance_id="wrk_agent",
            environment_id=7,
            session_id=None,
            message_type="response",
            payload={"ok": True, "epoch": epoch},
        ).to_dict()

    command_message = envelope(
        request_id="req-agent",
        worker_instance_id="wrk_agent",
        environment_id=7,
        session_id=None,
        message_type="command",
        payload={"name": "ping", "data": {}},
    )
    command_message.epoch = 1
    command_message.expires_at = int(time.time()) + 120
    command = command_message.to_dict()
    transports = [ScriptedTransport([reply(1), command]), ScriptedTransport([reply(2)])]

    async def scenario():
        cursor = {"index": 0}

        async def connect():
            transport = transports[cursor["index"]]
            cursor["index"] += 1
            return transport

        manager = WorkerConnectionManager(
            config,
            supervisor=supervisor,
            rng=random.Random(0),
            sleep=lambda _seconds: asyncio.sleep(0),
        )
        await manager.run(connect, asyncio.Event(), max_sessions=2)
        return manager

    manager = asyncio.run(scenario())
    assert manager.epochs == [1, 2]
    assert supervisor.restart_count == 0
    assert transports[0].closed is True
    assert transports[0].sent[0]["payload"]["name"] == "authenticate"
    assert transports[0].sent[1]["payload"]["stage"] == "ack"
    assert transports[0].sent[2]["payload"]["stage"] == "result"
    assert transports[0].sent[2]["payload"]["result"] == {"pong": True}
    assert len(manager.delays) == 1
    assert 1 <= manager.delays[0] <= 2
    result, error = handle_command(
        parse_message(
            dump_message(envelope(
                request_id="req-bad",
                worker_instance_id="wrk_agent",
                environment_id=7,
                session_id=None,
                message_type="command",
                payload={"name": "explode", "data": {}},
            )),
            max_bytes=10000,
        ),
        supervisor,
    )
    assert result is None
    assert error == "unsupported_command"


def test_agent_config_requires_token_and_does_not_echo_it():
    with pytest.raises(SystemExit) as caught:
        load_agent_config({"NEXUS_WORKER_GATEWAY_URL": "ws://nexus", "NEXUS_WORKER_INSTANCE_ID": "wrk"})
    assert "TOKEN" in str(caught.value)
    assert "wtk_" not in str(caught.value)


def test_worker_gateway_routes_are_mounted_and_reject_unknown_workers():
    with TestClient(app) as client:
        health = client.get("/api/worker-gateway/health")
        assert health.status_code == 200
        mode, warning = resolve_worker_gateway_bus(
            get_settings().worker_gateway_bus, get_settings().redis_url
        )
        assert health.json()["bus"] == mode
        assert health.json()["requested_bus"] == get_settings().worker_gateway_bus
        assert health.json()["warning"] == warning
        assert health.json()["owner_id"].startswith("gw_")
        denied = client.post("/api/worker-gateway/instances", json={"environment_id": 9_999_999})
        assert denied.status_code == 401
        missing = client.post(
            "/api/worker-gateway/instances",
            json={"environment_id": 9_999_999},
            headers=ADMIN_HEADERS,
        )
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "unknown_environment"
        assert client.get("/api/worker-gateway/commands/missing-command").status_code == 401
        assert client.get(
            "/api/worker-gateway/commands/missing-command", headers=ADMIN_HEADERS
        ).status_code == 404
        with client.websocket_connect("/api/worker-gateway/connect") as websocket:
            websocket.send_json({
                "request_id": "auth-missing",
                "worker_instance_id": "wrk_missing",
                "environment_id": 1,
                "session_id": None,
                "type": "command",
                "payload": {"name": "authenticate", "token": "wtk_missing"},
            })
            reply = websocket.receive_json()
        assert reply["payload"]["ok"] is False
        assert reply["payload"]["error_code"] == "unauthorized"


async def test_http_enroll_returns_token_once(environment_id: int):
    mini = FastAPI()
    from app.worker_gateway.routes import router

    mini.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mini), base_url="http://astra") as client:
        created = await client.post(
            "/api/worker-gateway/instances",
            json={"environment_id": environment_id},
            headers=ADMIN_HEADERS,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["token"].startswith("wtk_")
        visible = await client.get(
            f"/api/worker-gateway/instances/{body['worker_instance_id']}",
            headers=ADMIN_HEADERS,
        )
        assert visible.status_code == 200
        assert "token" not in visible.json()
        assert "token_digest" not in visible.json()


def test_bus_auto_uses_redis_url_and_memory_override_warns():
    assert resolve_worker_gateway_bus("auto", "redis://127.0.0.1:6379/0") == ("redis", None)
    assert resolve_worker_gateway_bus("auto", "  ") == ("memory", SINGLE_PROCESS_WARNING)
    assert resolve_worker_gateway_bus("memory", "redis://127.0.0.1:6379/0") == (
        "memory",
        SINGLE_PROCESS_WARNING,
    )
    assert resolve_worker_gateway_bus("redis", "") == ("redis", None)
    memory_gateway = WorkerGateway(
        settings=get_settings().model_copy(update={"worker_gateway_bus": "memory"})
    )
    assert memory_gateway.bus_mode == "memory"
    assert memory_gateway.bus_warning == SINGLE_PROCESS_WARNING
    redis_gateway = WorkerGateway(
        settings=get_settings().model_copy(
            update={"worker_gateway_bus": "auto", "redis_url": "redis://127.0.0.1:6379/15"}
        )
    )
    assert redis_gateway.bus_mode == "redis"
    assert redis_gateway.bus_warning is None


async def _assert_response_and_expiry(bus) -> None:
    await bus.set_owner("wrk-expire", "gw-a", 2)
    await bus.publish_response("expire-me", {"ok": True})
    await asyncio.sleep(0.4)
    assert await bus.touch_owner("wrk-expire", "gw-a", 2) is True
    assert await bus.touch_owner("wrk-expire", "gw-b", 2) is False
    await asyncio.sleep(0.4)
    assert await bus.get_owner("wrk-expire") == ("gw-a", 2)
    await asyncio.sleep(1.2)
    assert await bus.get_owner("wrk-expire") is None
    assert await bus.wait_response("expire-me", 0.2) is None
    pending = asyncio.create_task(bus.wait_response("late", 2))
    await asyncio.sleep(0.1)
    await bus.publish_response("late", {"stage": "result", "ok": True})
    assert await pending == {"stage": "result", "ok": True}


async def test_memory_response_route_matches_owner_expiry():
    bus = MemoryRouteBus(owner_ttl_seconds=1, result_ttl_seconds=1, control_limit=1, event_limit=1)
    assert await bus.set_owner("wrk-bound", "gw", 1) is True
    queues = await bus.bind("wrk-bound", epoch=1, owner_id="gw")
    assert queues is not None
    assert await bus.publish_command("wrk-bound", live_frame({"request_id": "c1"}, 1)) is True
    assert (await queues.wait_frame())["request_id"] == "c1"
    assert await bus.publish_command("wrk-bound", live_frame({"request_id": "c2"}, 1)) is True
    assert await bus.publish_command("wrk-bound", live_frame({"request_id": "c3"}, 1)) is False
    assert await bus.publish_event("wrk-bound", live_frame({"n": 1}, 1)) is True
    assert await bus.publish_event("wrk-bound", live_frame({"n": 2}, 1)) is False
    await _assert_response_and_expiry(bus)


@pytest.mark.skipif(
    not REDIS_URL,
    reason="未提供 ASTRA_TEST_REDIS_URL：Worker 路由未在真实 Redis 上联调，默认验证进程内等价语义",
)
async def test_redis_owner_epoch_and_bounded_command_list():
    prefix = f"astra:worker-gateway:test:{os.getpid()}:{uuid4().hex}"
    owner = RedisRouteBus(REDIS_URL, prefix=prefix, control_limit=1, event_limit=1)
    other = RedisRouteBus(REDIS_URL, prefix=prefix, control_limit=1, event_limit=1)
    worker_id = f"wrk_{uuid4().hex}"
    try:
        assert await owner.set_owner(worker_id, "gw-owner", 3) is True
        assert await other.set_owner(worker_id, "gw-late", 2) is False
        assert await other.get_owner(worker_id) == ("gw-owner", 3)
        assert await other.clear_owner(worker_id, 2, None) is False
        assert await other.clear_owner(worker_id, 3, "gw-other") is False
        assert await other.touch_owner(worker_id, "gw-other", 3) is False
        queues = await owner.bind(worker_id, epoch=3, owner_id="gw-owner")
        assert queues is not None
        assert await other.publish_command(worker_id, live_frame({"request_id": "redis-1"}, 3)) is True
        frame = await asyncio.wait_for(queues.wait_frame(), 2)
        assert frame["request_id"] == "redis-1"
        await queues.release_delivered(frame)
        assert await other.publish_command(worker_id, live_frame({"request_id": "redis-2"}, 3)) is True
        assert await other.publish_command(worker_id, live_frame({"request_id": "redis-3"}, 3)) is False
        assert await other.publish_event(worker_id, live_frame({"n": 1}, 3)) is True
        assert await other.publish_event(worker_id, live_frame({"n": 2}, 3)) is False
        race_id = f"wrk_{uuid4().hex}"
        assert await other.set_owner(race_id, "gw-owner", 1) is True
        raced = await asyncio.gather(
            *[
                other.publish_command(race_id, live_frame({"request_id": f"race-{i}"}, 1))
                for i in range(16)
            ]
        )
        assert sum(raced) == 1
        await other.publish_response("redis-result", {"ok": True, "stage": "result"})
        assert await owner.wait_response("redis-result", 2) == {"ok": True, "stage": "result"}
        assert await owner.clear_owner(worker_id, 3, "gw-owner") is True
        assert await other.get_owner(worker_id) is None
    finally:
        await owner.delete_prefix()
        await other.delete_prefix()
        await owner.aclose()
        await other.aclose()


@pytest.mark.skipif(
    not REDIS_URL,
    reason="未提供 ASTRA_TEST_REDIS_URL：Worker 路由未在真实 Redis 上联调，默认验证进程内等价语义",
)
async def test_redis_lease_and_result_expire_like_memory():
    prefix = f"astra:worker-gateway:test:{os.getpid()}:{uuid4().hex}"
    bus = RedisRouteBus(
        REDIS_URL, prefix=prefix, owner_ttl_seconds=1, result_ttl_seconds=1
    )
    try:
        await _assert_response_and_expiry(bus)
    finally:
        await bus.delete_prefix()
        await bus.aclose()
