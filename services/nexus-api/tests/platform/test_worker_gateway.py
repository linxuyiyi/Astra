import asyncio
import os
import random
import time
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from app.config.settings import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import WorkerCommand
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
from app.worker_gateway.protocol import dump_message, envelope, parse_message
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

    async def publish_command(self, agent_worker_id: str, frame: dict) -> bool:
        async with SessionLocal() as session:
            row = await session.scalar(
                select(WorkerCommand).where(WorkerCommand.request_id == frame["request_id"])
            )
            self.status_at_publish.append(None if row is None else row.status)
        self.published.append(frame["request_id"])
        return await super().publish_command(agent_worker_id, frame)


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


def gateway_for(bus: MemoryRouteBus | None = None, **settings) -> WorkerGateway:
    copied = get_settings().model_copy(update=settings) if settings else get_settings()
    return WorkerGateway(settings=copied, bus=bus or MemoryRouteBus())


async def dispatch(gateway: WorkerGateway, worker_id: str, request_id: str, name: str = "ping", payload=None):
    async with SessionLocal() as session:
        return await gateway.dispatch(
            session,
            agent_worker_id=worker_id,
            request_id=request_id,
            name=name,
            payload={} if payload is None else payload,
        )


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
        agent_worker_id="wrk_agent",
        token="wtk_test",
        ping_seconds=3600,
    )

    def reply(epoch: int) -> dict:
        return envelope(
            request_id="auth-wrk_agent",
            agent_worker_id="wrk_agent",
            session_id=None,
            message_type="response",
            payload={"ok": True, "epoch": epoch},
        ).to_dict()

    command_message = envelope(
        request_id="req-agent",
        agent_worker_id="wrk_agent",
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
            dump_message(
                envelope(
                    request_id="req-bad",
                    agent_worker_id="wrk_agent",
                    session_id=None,
                    message_type="command",
                    payload={"name": "explode", "data": {}},
                )
            ),
            max_bytes=10000,
        ),
        supervisor,
    )
    assert result is None
    assert error == "unsupported_command"


def test_agent_config_requires_token_and_does_not_echo_it():
    with pytest.raises(SystemExit) as caught:
        load_agent_config({"NEXUS_WORKER_GATEWAY_URL": "ws://nexus", "NEXUS_AGENT_WORKER_ID": "wrk"})
    assert "TOKEN" in str(caught.value)
    assert "wtk_" not in str(caught.value)


def test_worker_gateway_routes_are_mounted_and_reject_unknown_workers():
    from app.worker_gateway.routes import router

    assert {route.path for route in router.routes if getattr(route, "methods", None)} <= app.openapi()[
        "paths"
    ].keys()
    isolated_app = FastAPI()
    isolated_app.include_router(router)
    with TestClient(isolated_app) as client:
        health = client.get("/api/worker-gateway/health")
        assert health.status_code == 200
        mode, warning = resolve_worker_gateway_bus(
            get_settings().worker_gateway_bus, get_settings().redis_url
        )
        assert health.json()["bus"] == mode
        assert health.json()["requested_bus"] == get_settings().worker_gateway_bus
        assert health.json()["warning"] == warning
        assert health.json()["owner_id"].startswith("gw_")
        denied = client.post("/api/worker-gateway/workers", json={"agent_worker_id": "unknown-worker"})
        assert denied.status_code == 401
        missing = client.post(
            "/api/worker-gateway/workers",
            json={"agent_worker_id": "unknown-worker"},
            headers=ADMIN_HEADERS,
        )
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "unknown_worker"
        assert client.get("/api/worker-gateway/commands/missing-command").status_code == 401
        assert (
            client.get("/api/worker-gateway/commands/missing-command", headers=ADMIN_HEADERS).status_code
            == 404
        )
        with client.websocket_connect("/api/worker-gateway/connect") as websocket:
            websocket.send_json(
                {
                    "request_id": "auth-missing",
                    "agent_worker_id": "wrk_missing",
                    "session_id": None,
                    "type": "command",
                    "payload": {"name": "authenticate", "token": "wtk_missing"},
                }
            )
            reply = websocket.receive_json()
        assert reply["payload"]["ok"] is False
        assert reply["payload"]["error_code"] == "unauthorized"


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
            *[other.publish_command(race_id, live_frame({"request_id": f"race-{i}"}, 1)) for i in range(16)]
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
    bus = RedisRouteBus(REDIS_URL, prefix=prefix, owner_ttl_seconds=1, result_ttl_seconds=1)
    try:
        await _assert_response_and_expiry(bus)
    finally:
        await bus.delete_prefix()
        await bus.aclose()
