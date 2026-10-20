import asyncio
import json
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
import websockets
from sqlalchemy import select, update
from websockets.exceptions import ConnectionClosed

from app.database import SessionLocal, init_database
from app.models import (
    AgentWorker,
    OpenCodeExecutionEnvironment,
    WorkerCommand,
    WorkerInstance,
    utcnow,
)
from app.worker_agent.connection import AgentConfig, WorkerConnectionManager, event_request_id
from app.worker_agent.supervisor import RuntimeSupervisor
from app.worker_agent.transport import (
    WebSocketTransport,
    WorkerTransportError,
    translate_transport_error,
)
from app.worker_gateway.admin_key import ENV_NAME, provision_worker_gateway_admin_key
from app.worker_gateway.bus import _RESTORE_LUA, MemoryRouteBus, RedisRouteBus
from app.worker_gateway.protocol import WorkerGatewayError, dump_message, envelope
from app.worker_gateway.service import WorkerGateway, _SocketLease
from tests.platform.test_worker_gateway import (
    ADMIN_HEADERS,
    FakeWebSocket,
    ScriptedTransport,
    auth_message,
    bring_online,
    dispatch,
    gateway_for,
    live_frame,
    load_command,
    load_instance,
    wait_until,
)

REDIS_URL = os.environ.get("ASTRA_TEST_REDIS_URL")


class Adapter:
    def __init__(self, socket) -> None:
        self.socket = socket

    async def accept(self) -> None:
        return None

    async def send_text(self, data: str) -> None:
        await self.socket.send(data)

    async def receive_text(self) -> str:
        message = await self.socket.recv()
        if isinstance(message, bytes):
            return message.decode()
        return message

    async def close(self, code: int = 1000) -> None:
        await self.socket.close(code)


class HoldingTransport:
    def __init__(self, inbound: list[dict]) -> None:
        self.inbound = list(inbound)
        self.sent: list[dict] = []
        self.hold = asyncio.Event()

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    async def recv(self) -> dict:
        if self.inbound:
            return self.inbound.pop(0)
        await self.hold.wait()
        raise ConnectionError("closed")

    async def close(self) -> None:
        self.hold.set()


@pytest.fixture
async def environment_id() -> int:
    await init_database()
    async with SessionLocal() as session:
        employee = AgentWorker(id=f"emp-{uuid4().hex[:8]}", name="故障员工", role="执行")
        session.add(employee)
        await session.flush()
        environment = OpenCodeExecutionEnvironment(
            employee_id=employee.id, mode="LINUX_REMOTE", name="故障执行机"
        )
        session.add(environment)
        await session.commit()
        return environment.id


def auth_frame(worker_id: str, environment_id: int, token: str, request_id: str = "auth-1") -> dict:
    return envelope(
        request_id=request_id,
        worker_instance_id=worker_id,
        environment_id=environment_id,
        session_id=None,
        message_type="command",
        payload={"name": "authenticate", "token": token, "agent_version": "0.1.0"},
    ).to_dict()


async def enroll(gateway: WorkerGateway, environment_id: int):
    async with SessionLocal() as session:
        return await gateway.enroll(session, environment_id)


async def test_agent_reconnects_after_close_refusal_and_auth_timeout():
    modes: asyncio.Queue[str] = asyncio.Queue()
    for mode in ("close", "timeout", "ok"):
        await modes.put(mode)

    async def handler(socket) -> None:
        mode = await modes.get()
        await socket.recv()
        if mode == "timeout":
            await asyncio.sleep(2)
            return
        reply = envelope(
            request_id="auth-wrk_agent",
            worker_instance_id="wrk_agent",
            environment_id=7,
            session_id=None,
            message_type="response",
            payload={"ok": True, "epoch": 1 if mode == "close" else 2, "ping_seconds": 30},
        )
        await socket.send(dump_message(reply))
        await socket.close()

    server = await websockets.serve(handler, "127.0.0.1", 0, ping_interval=None)
    port = server.sockets[0].getsockname()[1]
    steps = ["close", "refuse", "timeout", "ok"]

    async def connect():
        step = steps.pop(0)
        target = "ws://127.0.0.1:9" if step == "refuse" else f"ws://127.0.0.1:{port}"
        try:
            socket = await websockets.connect(target, open_timeout=0.5, ping_interval=None)
        except Exception as exc:
            raise translate_transport_error(exc) from exc
        return WebSocketTransport(socket)

    config = AgentConfig(
        gateway_url=f"ws://127.0.0.1:{port}",
        worker_instance_id="wrk_agent",
        environment_id=7,
        token="wtk_test",
        auth_timeout_seconds=0.3,
        ping_seconds=30,
    )
    manager = WorkerConnectionManager(
        config, supervisor=RuntimeSupervisor(), sleep=lambda _seconds: asyncio.sleep(0)
    )
    try:
        await manager.run(connect, asyncio.Event(), max_sessions=4)
    finally:
        server.close()
        await server.wait_closed()

    assert manager.epochs == [1, 2]
    assert manager.supervisor.restart_count == 0
    assert len(manager.delays) == 3


async def test_protocol_violation_closes_real_socket(environment_id: int):
    gateway = gateway_for(
        MemoryRouteBus(),
        worker_gateway_max_frame_bytes=480,
        worker_gateway_ping_seconds=30,
    )
    enrolled = await enroll(gateway, environment_id)

    async def handler(socket) -> None:
        await gateway.serve_socket(Adapter(socket))

    server = await websockets.serve(handler, "127.0.0.1", 0, ping_interval=None, max_size=2**20)
    port = server.sockets[0].getsockname()[1]
    url = f"ws://127.0.0.1:{port}"
    oversized = {
        "request_id": "too-big",
        "worker_instance_id": enrolled.worker_instance_id,
        "environment_id": environment_id,
        "session_id": None,
        "type": "command",
        "payload": {"name": "authenticate", "token": enrolled.token, "blob": "x" * 400},
    }
    try:
        async with websockets.connect(url, ping_interval=None, max_size=2**20) as socket:
            await socket.send(json.dumps(oversized))
            code = await _close_code(socket)
        assert code == 1009

        async with websockets.connect(url, ping_interval=None, max_size=2**20) as socket:
            await socket.send(json.dumps(auth_frame(
                enrolled.worker_instance_id, environment_id, enrolled.token, "auth-ok"
            )))
            ack = json.loads(await asyncio.wait_for(socket.recv(), 2))
            assert ack["payload"]["ok"] is True, ack
            epoch = ack["payload"]["epoch"]
            await socket.send(json.dumps({**oversized, "request_id": "too-big-2"}))
            await socket.send("not-json")
            await socket.send(json.dumps({
                "request_id": "evt-" + ("目录" * 40),
                "worker_instance_id": enrolled.worker_instance_id,
                "environment_id": environment_id,
                "session_id": None,
                "type": "event",
                "payload": {"seq": 1, "directory": "/work", "event": {"type": "server.connected"}},
                "epoch": epoch,
            }))
            ping = envelope(
                request_id="hb-stay",
                worker_instance_id=enrolled.worker_instance_id,
                environment_id=environment_id,
                session_id=None,
                message_type="heartbeat",
                payload={"kind": "ping"},
            )
            ping.epoch = epoch
            ping.expires_at = int(time.time()) + 60
            await socket.send(dump_message(ping))
            saw_pong = False
            deadline = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < deadline:
                raw = await asyncio.wait_for(socket.recv(), 2)
                frame = json.loads(raw)
                if frame.get("type") == "heartbeat" and frame.get("payload", {}).get("kind") == "pong":
                    saw_pong = True
                    break
            assert saw_pong
        assert (await load_instance(enrolled.worker_instance_id)).status == "online"
    finally:
        server.close()
        await server.wait_closed()


async def test_long_directory_subscription_stays_online(environment_id: int):
    directory = "/srv/tasks/" + ("工作区" * 40)
    assert len(directory) > 100
    assert len(event_request_id(directory, 1)) <= 80
    assert event_request_id(directory, 1) == event_request_id(directory, 1)
    assert event_request_id(directory, 1) != event_request_id(directory, 2)
    assert "工作区" not in event_request_id(directory, 1)

    async def serve_sse(reader, writer):
        try:
            while True:
                line = await reader.readline()
                if not line or line in {b"\r\n", b"\n"}:
                    break
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\n"
                b"\r\n"
                b'data: {"type":"server.connected"}\n\n'
            )
            await writer.drain()
            await asyncio.sleep(30)
        finally:
            writer.close()
            await writer.wait_closed()

    sse = await asyncio.start_server(serve_sse, "127.0.0.1", 0)
    sse_port = sse.sockets[0].getsockname()[1]
    gateway = gateway_for(
        MemoryRouteBus(),
        worker_gateway_ping_seconds=60,
        worker_gateway_ping_timeout_seconds=30,
        worker_gateway_dispatch_lease_seconds=60,
    )
    enrolled = await enroll(gateway, environment_id)
    seen: list = []
    record = gateway.record_worker_event

    async def spy(auth, message):
        seen.append(message)
        return await record(auth, message)

    gateway.record_worker_event = spy

    async def handler(socket):
        await gateway.serve_socket(Adapter(socket))

    server = await websockets.serve(handler, "127.0.0.1", 0, ping_interval=None, max_size=2**20)
    port = server.sockets[0].getsockname()[1]

    async def connect():
        socket = await websockets.connect(
            f"ws://127.0.0.1:{port}", ping_interval=None, max_size=2**20
        )
        return WebSocketTransport(socket)

    manager = WorkerConnectionManager(
        AgentConfig(
            gateway_url=f"ws://127.0.0.1:{port}",
            worker_instance_id=enrolled.worker_instance_id,
            environment_id=environment_id,
            token=enrolled.token,
            ping_seconds=60,
            opencode_url=f"http://127.0.0.1:{sse_port}",
        ),
        supervisor=RuntimeSupervisor(),
        sleep=lambda _seconds: asyncio.sleep(0),
    )
    stop = asyncio.Event()
    agent = asyncio.create_task(manager.run(connect, stop))
    try:
        await wait_until(lambda: manager.epoch is not None, timeout=10)
        assert (await load_instance(enrolled.worker_instance_id)).status == "online"
        subscribed = await dispatch(
            gateway,
            enrolled.worker_instance_id,
            "req-sub-long",
            "subscribe_events",
            {"directory": directory},
        )
        assert subscribed.delivered is True

        def connected() -> bool:
            return any(
                isinstance(item.payload, dict)
                and isinstance(item.payload.get("event"), dict)
                and item.payload["event"].get("type") == "server.connected"
                for item in seen
            )

        await wait_until(connected, timeout=10)
        event = next(
            item
            for item in seen
            if isinstance(item.payload.get("event"), dict)
            and item.payload["event"].get("type") == "server.connected"
        )
        assert event.payload["directory"] == directory
        assert event.payload["seq"] == 1
        assert len(event.request_id) <= 80
        assert directory not in event.request_id
        assert "工作区" not in event.request_id
        assert event.request_id == event_request_id(directory, 1)
        assert (await load_instance(enrolled.worker_instance_id)).status == "online"
        ping = await dispatch(gateway, enrolled.worker_instance_id, "req-still-up", "ping")
        assert ping.delivered is True
        deadline = asyncio.get_running_loop().time() + 10
        command = await load_command("req-still-up")
        while command.status != "succeeded" and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
            command = await load_command("req-still-up")
        assert command.status == "succeeded"
        assert (await load_instance(enrolled.worker_instance_id)).status == "online"
    finally:
        stop.set()
        agent.cancel()
        await asyncio.gather(agent, return_exceptions=True)
        server.close()
        await server.wait_closed()
        sse.close()
        await sse.wait_closed()


async def _close_code(socket) -> int | None:
    while True:
        try:
            await asyncio.wait_for(socket.recv(), 2)
        except ConnectionClosed as exc:
            return None if exc.rcvd is None else exc.rcvd.code


async def test_older_registration_cannot_replace_owner_or_socket():
    bus = MemoryRouteBus()
    gateway = gateway_for(bus)
    assert await bus.set_owner("wrk_order", "gw-new", 4) is True
    assert await bus.set_owner("wrk_order", "gw-old", 3) is False
    assert await bus.set_owner("wrk_order", "gw-other", 4) is False
    assert await bus.get_owner("wrk_order") == ("gw-new", 4)
    current = _SocketLease(4, object(), deque(), 1)
    stale = _SocketLease(3, object(), deque(), 1)
    assert gateway._remember_socket("wrk_order", current) is True
    assert gateway._remember_socket("wrk_order", stale) is False
    assert gateway.sockets["wrk_order"] is current


async def test_concurrent_request_id_replays_match_and_rejects_conflict(environment_id: int):
    gateway = gateway_for()
    async with SessionLocal() as session:
        first = await gateway.enroll(session, environment_id)
        employee = AgentWorker(id=f"emp-{uuid4().hex[:8]}", name="另一员工", role="执行")
        session.add(employee)
        await session.flush()
        other_env = OpenCodeExecutionEnvironment(
            employee_id=employee.id, mode="LINUX_REMOTE", name="另一执行机"
        )
        session.add(other_env)
        await session.commit()
        other_env_id = other_env.id
    async with SessionLocal() as session:
        second = await gateway.enroll(session, other_env_id)

    async def submit(worker_id: str, payload: dict):
        async with SessionLocal() as session:
            return await gateway.dispatch(
                session,
                worker_instance_id=worker_id,
                request_id="req-shared",
                name="echo",
                payload=payload,
            )

    results = await asyncio.gather(
        submit(first.worker_instance_id, {"n": 1}),
        submit(first.worker_instance_id, {"n": 1}),
    )
    assert {item.command.id for item in results} == {results[0].command.id}
    assert sum(item.replayed for item in results) == 1

    with pytest.raises(WorkerGatewayError) as mismatched:
        await submit(first.worker_instance_id, {"n": 2})
    assert mismatched.value.code == "duplicate_request"
    with pytest.raises(WorkerGatewayError) as other_worker:
        await submit(second.worker_instance_id, {"n": 1})
    assert other_worker.value.code == "duplicate_request"


async def test_sweep_and_result_keep_a_single_terminal_state(environment_id: int):
    gateway = gateway_for()
    async with SessionLocal() as session:
        enrolled = await gateway.enroll(session, environment_id)
        auth = await gateway.authenticate(
            session,
            envelope(
                request_id="auth-sweep",
                worker_instance_id=enrolled.worker_instance_id,
                environment_id=environment_id,
                session_id=None,
                message_type="command",
                payload={"name": "authenticate", "token": enrolled.token},
            ),
            enrolled.token,
        )
        succeeded = WorkerCommand(
            request_id="req-succeeded",
            worker_instance_id=enrolled.worker_instance_id,
            environment_id=environment_id,
            name="ping",
            payload={},
            status="succeeded",
            epoch=auth.epoch,
            result={"pong": True},
            deadline_at=utcnow() - timedelta(seconds=5),
            finished_at=utcnow(),
        )
        pending = WorkerCommand(
            request_id="req-pending",
            worker_instance_id=enrolled.worker_instance_id,
            environment_id=environment_id,
            name="ping",
            payload={},
            status="pending",
            epoch=auth.epoch,
            deadline_at=utcnow() - timedelta(seconds=5),
        )
        session.add(succeeded)
        session.add(pending)
        await session.commit()

    async def finish():
        async with SessionLocal() as session:
            return await gateway.apply_response(
                session,
                worker_instance_id=enrolled.worker_instance_id,
                epoch=auth.epoch,
                message=envelope(
                    request_id="req-pending",
                    worker_instance_id=enrolled.worker_instance_id,
                    environment_id=environment_id,
                    session_id=None,
                    message_type="response",
                    payload={"stage": "result", "ok": True, "result": {"pong": True}, "error_code": None},
                ),
            )

    await asyncio.gather(finish(), gateway.sweep(now=utcnow()))
    async with SessionLocal() as session:
        kept = await session.scalar(select(WorkerCommand).where(WorkerCommand.request_id == "req-succeeded"))
        raced = await session.scalar(select(WorkerCommand).where(WorkerCommand.request_id == "req-pending"))
    assert kept is not None and kept.status == "succeeded" and kept.result == {"pong": True}
    assert raced is not None and raced.status in {"succeeded", "expired"}
    if raced.status == "succeeded":
        assert raced.result == {"pong": True}
        assert raced.error_code is None
    else:
        assert raced.error_code == "timeout"
    await gateway.sweep(now=utcnow())
    async with SessionLocal() as session:
        kept = await session.scalar(select(WorkerCommand).where(WorkerCommand.request_id == "req-succeeded"))
    assert kept is not None and kept.status == "succeeded"


async def test_owner_retries_idempotent_delivery_and_holds_prompt(environment_id: int):
    bus = MemoryRouteBus()
    gateway = gateway_for(bus, worker_gateway_dispatch_lease_seconds=10)
    async with SessionLocal() as session:
        enrolled = await gateway.enroll(session, environment_id)
    async with SessionLocal() as session:
        auth = await gateway.authenticate(
            session,
            envelope(
                request_id="auth-deliver",
                worker_instance_id=enrolled.worker_instance_id,
                environment_id=environment_id,
                session_id=None,
                message_type="command",
                payload={"name": "authenticate", "token": enrolled.token},
            ),
            enrolled.token,
        )
    assert await bus.set_owner(enrolled.worker_instance_id, gateway.owner_id, auth.epoch) is True
    queues = await bus.bind(enrolled.worker_instance_id, epoch=auth.epoch, owner_id=gateway.owner_id)
    assert queues is not None

    async def send(request_id: str, name: str):
        async with SessionLocal() as session:
            return await gateway.dispatch(
                session,
                worker_instance_id=enrolled.worker_instance_id,
                request_id=request_id,
                name=name,
                payload={"text": name},
            )

    ping = await send("req-ping", "ping")
    prompt = await send("req-prompt", "prompt")
    assert ping.delivered is True and prompt.delivered is True
    assert (await queues.wait_frame())["request_id"] == "req-ping"
    assert (await queues.wait_frame())["request_id"] == "req-prompt"
    await bus.unbind(enrolled.worker_instance_id, queues)
    rebound = await bus.bind(enrolled.worker_instance_id, epoch=auth.epoch, owner_id=gateway.owner_id)
    assert rebound is not None
    async with SessionLocal() as session:
        await session.execute(
            update(WorkerCommand)
            .where(WorkerCommand.request_id.in_(("req-ping", "req-prompt")))
            .values(last_dispatched_at=utcnow() - timedelta(seconds=30))
        )
        await session.commit()
    assert await gateway.dispatch_due(enrolled.worker_instance_id, auth.epoch) == 1
    retried = await rebound.wait_frame()
    assert retried["request_id"] == "req-ping"
    assert retried["epoch"] == auth.epoch
    assert await gateway.dispatch_due(enrolled.worker_instance_id, auth.epoch) == 0


async def test_agent_dedupes_commands_and_ping_timeout_reconnects():
    config = AgentConfig(
        gateway_url="ws://nexus.internal/api/worker-gateway/connect",
        worker_instance_id="wrk_agent",
        environment_id=7,
        token="wtk_test",
        ping_seconds=0.05,
        ping_timeout_seconds=0.05,
        heartbeat_misses=1,
        auth_timeout_seconds=1,
    )
    def auth_reply(ping: float) -> dict:
        return envelope(
            request_id="auth-wrk_agent",
            worker_instance_id="wrk_agent",
            environment_id=7,
            session_id=None,
            message_type="response",
            payload={
                "ok": True,
                "epoch": 3,
                "ping_seconds": ping,
                "ping_timeout_seconds": 0.05,
            },
        ).to_dict()

    reply = auth_reply(30)
    command = envelope(
        request_id="req-agent",
        worker_instance_id="wrk_agent",
        environment_id=7,
        session_id=None,
        message_type="command",
        payload={"name": "echo", "data": {"value": 1}},
    )
    command.epoch = 3
    command.expires_at = int(time.time()) + 60
    finished = ScriptedTransport([reply, command.to_dict()])
    executing = ScriptedTransport([reply, command.to_dict()])
    supervisor = RuntimeSupervisor()

    async def scenario():
        cursor = {"index": 0}
        transports = [finished, executing]

        async def connect():
            transport = transports[cursor["index"]]
            cursor["index"] += 1
            return transport

        manager = WorkerConnectionManager(
            config, supervisor=supervisor, sleep=lambda _seconds: asyncio.sleep(0)
        )
        manager.ledger.finish("req-agent", {"echo": {"value": 1}}, None, time.time())
        with pytest.raises(ConnectionError):
            await manager.run_session(finished)
        manager.ledger.begin("req-agent", time.time())
        with pytest.raises(ConnectionError):
            await manager.run_session(executing)
        quick = auth_reply(0.05)
        holding = HoldingTransport([quick])
        follow = ScriptedTransport([quick])

        async def reconnect():
            transport = holding if cursor["index"] == 2 else follow
            cursor["index"] += 1
            return transport

        cursor["index"] = 2
        await manager.run(reconnect, asyncio.Event(), max_sessions=2)
        return manager, holding

    manager, holding = await scenario()
    assert finished.sent[1]["payload"]["stage"] == "result"
    assert finished.sent[1]["payload"]["result"] == {"echo": {"value": 1}}
    assert executing.sent[1]["payload"]["stage"] == "duplicate"
    assert executing.sent[1]["payload"]["result"] == {"state": "already_executing"}
    assert manager.supervisor.restart_count == 0
    assert any(item["payload"].get("kind") == "ping" for item in holding.sent)


@pytest.mark.skipif(
    not REDIS_URL,
    reason="未提供 ASTRA_TEST_REDIS_URL：Worker 路由未在真实 Redis 上联调，默认验证进程内等价语义",
)
async def test_redis_old_consumer_stops_after_newer_epoch():
    prefix = f"astra:worker-gateway:test:{os.getpid()}:{uuid4().hex}"
    older = RedisRouteBus(REDIS_URL, prefix=prefix, control_limit=4)
    newer = RedisRouteBus(REDIS_URL, prefix=prefix, control_limit=4)
    worker_id = f"wrk_{uuid4().hex}"
    try:
        assert await older.set_owner(worker_id, "gw-old", 1) is True
        old_queues = await older.bind(worker_id, epoch=1, owner_id="gw-old")
        assert old_queues is not None
        assert await newer.set_owner(worker_id, "gw-new", 2) is True
        assert await older.set_owner(worker_id, "gw-old", 1) is False
        new_queues = await newer.bind(worker_id, epoch=2, owner_id="gw-new")
        assert new_queues is not None
        assert await newer.publish_command(worker_id, live_frame({"request_id": "current"}, 2)) is True
        assert await newer.publish_command(worker_id, live_frame({"request_id": "stale"}, 1)) is False
        delivered = await asyncio.wait_for(new_queues.wait_frame(), 2)
        assert delivered["request_id"] == "current"
        assert delivered["epoch"] == 2
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(old_queues.wait_frame(), 0.3)
        await asyncio.sleep(0.1)
        assert older._pulls[id(old_queues)][1].done()
    finally:
        await older.delete_prefix()
        await newer.delete_prefix()
        await older.aclose()
        await newer.aclose()


def _restore_admin_env(previous: str | None) -> None:
    if previous is None:
        os.environ.pop(ENV_NAME, None)
    else:
        os.environ[ENV_NAME] = previous


def test_startup_script_writes_admin_key_once(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("ASTRA_REDIS_URL=redis://127.0.0.1:6379/0\n", encoding="utf-8")
    previous = os.environ.pop(ENV_NAME, None)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            keys = list(pool.map(lambda _index: provision_worker_gateway_admin_key(env_file), (0, 1)))
        assert keys[0] is not None and keys[0] == keys[1]
        assert keys[0].startswith("wgk_")
        text = env_file.read_text(encoding="utf-8")
        assert "ASTRA_REDIS_URL=redis://127.0.0.1:6379/0" in text
        assert text.count(f"{ENV_NAME}=") == 1
        assert (env_file.stat().st_mode & 0o777) == 0o600
        assert provision_worker_gateway_admin_key(env_file) == keys[0]
    finally:
        _restore_admin_env(previous)


def test_injected_admin_key_is_not_replaced(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("ASTRA_REDIS_URL=redis://127.0.0.1:6379/0\n", encoding="utf-8")
    previous = os.environ.get(ENV_NAME)
    os.environ[ENV_NAME] = "wgk_from_env"
    try:
        assert provision_worker_gateway_admin_key(env_file) == "wgk_from_env"
        assert os.environ[ENV_NAME] == "wgk_from_env"
        assert f"{ENV_NAME}=" not in env_file.read_text(encoding="utf-8")
    finally:
        _restore_admin_env(previous)


def test_stale_admin_lock_file_does_not_block_generation(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    env_file.with_name(env_file.name + ".gateway-admin.lock").write_bytes(b"\0")
    previous = os.environ.pop(ENV_NAME, None)
    try:
        started = time.monotonic()
        key = provision_worker_gateway_admin_key(env_file)
        assert time.monotonic() - started < 1
        assert key is not None and key.startswith("wgk_")
        assert f"{ENV_NAME}={key}" in env_file.read_text(encoding="utf-8")
    finally:
        _restore_admin_env(previous)


def test_admin_key_provision_survives_unwritable_parent(tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("x", encoding="utf-8")
    previous = os.environ.pop(ENV_NAME, None)
    try:
        assert provision_worker_gateway_admin_key(blocked / ".env") is None
    finally:
        _restore_admin_env(previous)


def test_admin_header_constant_matches_routes():
    assert ADMIN_HEADERS["X-Astra-Worker-Gateway-Key"] == "wgk_test"


def _agent_config(**overrides) -> AgentConfig:
    values = {
        "gateway_url": "ws://nexus.internal/api/worker-gateway/connect",
        "worker_instance_id": "wrk_agent",
        "environment_id": 7,
        "token": "wtk_test",
        "ping_seconds": 3600,
        "auth_timeout_seconds": 1,
    }
    values.update(overrides)
    return AgentConfig(**values)


def _auth_reply() -> dict:
    return envelope(
        request_id="auth-wrk_agent",
        worker_instance_id="wrk_agent",
        environment_id=7,
        session_id=None,
        message_type="response",
        payload={"ok": True, "epoch": 3, "ping_seconds": 3600},
    ).to_dict()


async def test_cleared_or_expired_lease_rejects_older_epoch():
    bus = MemoryRouteBus()
    assert await bus.set_owner("wrk_floor", "gw-new", 2) is True
    assert await bus.clear_owner("wrk_floor", 2, "gw-new") is True
    assert await bus.get_owner("wrk_floor") is None
    assert await bus.set_owner("wrk_floor", "gw-late", 1) is False
    assert await bus.set_owner("wrk_floor", "gw-new", 2) is True

    expiring = MemoryRouteBus(owner_ttl_seconds=0.2)
    assert await expiring.set_owner("wrk_expire", "gw-new", 2) is True
    await asyncio.sleep(0.3)
    assert await expiring.get_owner("wrk_expire") is None
    assert await expiring.set_owner("wrk_expire", "gw-late", 1) is False
    assert await expiring.set_owner("wrk_expire", "gw-new", 2) is True


async def test_handoff_refuses_command_when_database_epoch_moved(environment_id: int):
    gateway = gateway_for(MemoryRouteBus())
    enrolled, auth = await bring_online(gateway, environment_id)
    request_id = f"req-fenced-{uuid4().hex}"
    sent = await dispatch(gateway, enrolled.worker_instance_id, request_id, "ping")
    assert sent.delivered is True
    async with SessionLocal() as session:
        await session.execute(
            update(WorkerInstance)
            .where(WorkerInstance.id == enrolled.worker_instance_id)
            .values(epoch=auth.epoch + 1)
        )
        await session.execute(
            update(WorkerCommand)
            .where(WorkerCommand.request_id == request_id)
            .values(last_dispatched_at=utcnow() - timedelta(seconds=30))
        )
        await session.commit()
    ok, reason = await gateway._handoff(sent.command.id, owner_only=False)
    assert ok is False
    assert reason == "offline"
    stored = await load_command(request_id)
    assert stored.delivery_state == "published"
    assert stored.delivery_attempts == 1
    assert await gateway.dispatch_due(enrolled.worker_instance_id, auth.epoch) == 0


class _EpochShiftBus(MemoryRouteBus):
    async def set_owner(self, worker_instance_id: str, owner_id: str, epoch: int) -> bool:
        ok = await super().set_owner(worker_instance_id, owner_id, epoch)
        async with SessionLocal() as session:
            await session.execute(
                update(WorkerInstance)
                .where(WorkerInstance.id == worker_instance_id)
                .values(epoch=epoch + 3, status="online")
            )
            await session.commit()
        return ok


async def test_serve_socket_drops_owner_when_database_epoch_moved(environment_id: int):
    gateway = gateway_for(_EpochShiftBus(), worker_gateway_ping_seconds=30)
    enrolled = await enroll(gateway, environment_id)
    socket = FakeWebSocket()
    task = asyncio.create_task(gateway.serve_socket(socket))
    await socket.incoming.put(dump_message(auth_message(
        enrolled.worker_instance_id, environment_id, enrolled.token
    )))
    await asyncio.wait_for(task, 2)
    assert socket.close_code == 4000
    assert await gateway.bus.get_owner(enrolled.worker_instance_id) is None


class _LostReplyBus(MemoryRouteBus):
    def __init__(self) -> None:
        super().__init__()
        self.lose_reply = False

    async def publish_command(self, worker_instance_id: str, frame: dict) -> bool:
        if self.lose_reply:
            raise RuntimeError("reply lost")
        return await super().publish_command(worker_instance_id, frame)


async def test_unknown_publish_is_not_rolled_back_and_accepted_is_not_resent(environment_id: int):
    bus = _LostReplyBus()
    gateway = gateway_for(bus, worker_gateway_dispatch_lease_seconds=10)
    enrolled, auth = await bring_online(gateway, environment_id)
    prompt_id = f"req-prompt-{uuid4().hex}"
    ping_id = f"req-ping-{uuid4().hex}"
    bus.lose_reply = True
    lost = await dispatch(gateway, enrolled.worker_instance_id, prompt_id, "prompt", {"text": "go"})
    assert lost.delivered is False
    prompt = await load_command(prompt_id)
    assert prompt.delivery_state == "uncertain"
    assert prompt.delivery_attempts == 1
    async with SessionLocal() as session:
        await session.execute(
            update(WorkerCommand)
            .where(WorkerCommand.request_id == prompt_id)
            .values(last_dispatched_at=utcnow() - timedelta(seconds=30))
        )
        await session.commit()
    assert await gateway.dispatch_due(enrolled.worker_instance_id, auth.epoch) == 0
    assert (await load_command(prompt_id)).delivery_attempts == 1

    bus.lose_reply = False
    queues = bus.bound[enrolled.worker_instance_id]
    await bus.unbind(enrolled.worker_instance_id, queues)
    rejected = await dispatch(gateway, enrolled.worker_instance_id, ping_id, "ping")
    assert rejected.delivered is False
    ping = await load_command(ping_id)
    assert ping.delivery_state == "unpublished"
    assert ping.delivery_attempts == 0
    rebound = await bus.bind(enrolled.worker_instance_id, epoch=auth.epoch, owner_id=gateway.owner_id)
    assert rebound is not None
    assert await gateway.dispatch_due(enrolled.worker_instance_id, auth.epoch) == 1
    assert (await load_command(ping_id)).delivery_state == "published"
    async with SessionLocal() as session:
        acked = await gateway.apply_response(
            session,
            worker_instance_id=enrolled.worker_instance_id,
            epoch=auth.epoch,
            message=envelope(
                request_id=ping_id,
                worker_instance_id=enrolled.worker_instance_id,
                environment_id=environment_id,
                session_id=None,
                message_type="response",
                payload={"stage": "ack", "ok": True, "result": None, "error_code": None},
            ),
        )
    assert acked is True
    async with SessionLocal() as session:
        await session.execute(
            update(WorkerCommand)
            .where(WorkerCommand.request_id == ping_id)
            .values(last_dispatched_at=utcnow() - timedelta(seconds=30))
        )
        await session.commit()
    assert await gateway.dispatch_due(enrolled.worker_instance_id, auth.epoch) == 0
    assert (await load_command(ping_id)).status == "accepted"


class _BoomSocket(FakeWebSocket):
    async def send_text(self, data: str) -> None:
        self.sent.append(data)
        if len(self.sent) >= 2:
            raise ConnectionError("send failed")


async def test_send_loop_failure_ends_session_and_closes_socket(environment_id: int):
    gateway = gateway_for(
        MemoryRouteBus(),
        worker_gateway_ping_seconds=0.05,
        worker_gateway_ping_timeout_seconds=5,
        worker_gateway_dispatch_lease_seconds=60,
    )
    enrolled = await enroll(gateway, environment_id)
    socket = _BoomSocket()
    task = asyncio.create_task(gateway.serve_socket(socket))
    await socket.incoming.put(dump_message(auth_message(
        enrolled.worker_instance_id, environment_id, enrolled.token
    )))
    await asyncio.wait_for(task, 2)
    assert task.done()
    assert socket.close_code is not None


async def test_sweep_closes_socket_and_heartbeat_does_not_revive(environment_id: int):
    gateway = gateway_for(
        MemoryRouteBus(),
        worker_gateway_ping_seconds=60,
        worker_gateway_ping_timeout_seconds=10,
    )
    enrolled = await enroll(gateway, environment_id)
    socket = FakeWebSocket()
    task = asyncio.create_task(gateway.serve_socket(socket))
    await socket.incoming.put(dump_message(auth_message(
        enrolled.worker_instance_id, environment_id, enrolled.token
    )))
    await wait_until(lambda: socket.sent)
    ack = json.loads(socket.sent[0])
    epoch = ack["payload"]["epoch"]
    async with SessionLocal() as session:
        await session.execute(
            update(WorkerInstance)
            .where(WorkerInstance.id == enrolled.worker_instance_id)
            .values(last_heartbeat_at=utcnow() - timedelta(seconds=120))
        )
        await session.commit()
    await gateway.sweep(now=utcnow())
    assert (await load_instance(enrolled.worker_instance_id)).status == "offline"
    await wait_until(lambda: socket.close_code is not None)
    assert await gateway.bus.get_owner(enrolled.worker_instance_id) is None
    async with SessionLocal() as session:
        revived = await gateway.note_heartbeat(session, enrolled.worker_instance_id, epoch)
    assert revived is False
    assert (await load_instance(enrolled.worker_instance_id)).status == "offline"
    await asyncio.wait_for(task, 2)


async def test_old_epoch_disconnect_leaves_a_reconnected_worker_online(environment_id: int):
    gateway = gateway_for(MemoryRouteBus())
    enrolled = await enroll(gateway, environment_id)
    socket = FakeWebSocket()
    task = asyncio.create_task(gateway.serve_socket(socket))
    await socket.incoming.put(dump_message(auth_message(
        enrolled.worker_instance_id, environment_id, enrolled.token
    )))
    await wait_until(lambda: socket.sent)
    old_epoch = json.loads(socket.sent[0])["payload"]["epoch"]
    async with SessionLocal() as session:
        await session.execute(
            update(WorkerInstance)
            .where(WorkerInstance.id == enrolled.worker_instance_id)
            .values(epoch=old_epoch + 1, status="online", last_heartbeat_at=utcnow())
        )
        await session.commit()
        await gateway.disconnect(session, enrolled.worker_instance_id, old_epoch)
    stored = await load_instance(enrolled.worker_instance_id)
    assert stored.epoch == old_epoch + 1
    assert stored.status == "online"
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_auth_failure_closes_transport_and_ack_failure_skips_ledger():
    manager = WorkerConnectionManager(_agent_config(), sleep=lambda _seconds: asyncio.sleep(0))
    failed = ScriptedTransport([])
    with pytest.raises(ConnectionError):
        await manager.run_session(failed)
    assert failed.closed is True

    class AckFail(ScriptedTransport):
        async def send(self, message: dict) -> None:
            self.sent.append(message)
            if message.get("payload", {}).get("stage") == "ack":
                raise ConnectionError("ack lost")

    command = envelope(
        request_id="req-agent",
        worker_instance_id="wrk_agent",
        environment_id=7,
        session_id=None,
        message_type="command",
        payload={"name": "ping", "data": {}},
    )
    command.epoch = 3
    command.expires_at = int(time.time()) + 60
    transport = AckFail([_auth_reply(), command.to_dict()])
    with pytest.raises(ConnectionError):
        await manager.run_session(transport)
    assert transport.closed is True
    assert manager.ledger.lookup("req-agent", time.time()) is None


@pytest.mark.skipif(
    not REDIS_URL,
    reason="未提供 ASTRA_TEST_REDIS_URL：Worker 路由未在真实 Redis 上联调，默认验证进程内等价语义",
)
async def test_redis_floor_survives_clear_and_expiry():
    prefix = f"astra:worker-gateway:test:{os.getpid()}:{uuid4().hex}"
    bus = RedisRouteBus(REDIS_URL, prefix=prefix, owner_ttl_seconds=1)
    worker_id = f"wrk_{uuid4().hex}"
    try:
        assert await bus.set_owner(worker_id, "gw-new", 2) is True
        assert await bus.clear_owner(worker_id, 2, "gw-new") is True
        assert await bus.get_owner(worker_id) is None
        assert await bus.set_owner(worker_id, "gw-late", 1) is False
        expiring = f"wrk_{uuid4().hex}"
        assert await bus.set_owner(expiring, "gw-new", 2) is True
        await asyncio.sleep(1.6)
        assert await bus.get_owner(expiring) is None
        assert await bus.set_owner(expiring, "gw-late", 1) is False
    finally:
        await bus.delete_prefix()
        await bus.aclose()


@pytest.mark.skipif(
    not REDIS_URL,
    reason="未提供 ASTRA_TEST_REDIS_URL：Worker 路由未在真实 Redis 上联调，默认验证进程内等价语义",
)
async def test_local_heartbeats_do_not_release_redis_capacity_and_unbind_deletes_list():
    prefix = f"astra:worker-gateway:test:{os.getpid()}:{uuid4().hex}"
    bus = RedisRouteBus(REDIS_URL, prefix=prefix, control_limit=2)
    worker_id = f"wrk_{uuid4().hex}"
    try:
        assert await bus.set_owner(worker_id, "gw", 4) is True
        queues = await bus.bind(worker_id, epoch=4, owner_id="gw")
        assert queues is not None
        assert await queues.put_control(live_frame({"request_id": "hb"}, 4)) is True
        assert await bus.publish_command(worker_id, live_frame({"request_id": "c1"}, 4)) is True
        await wait_until(lambda: queues.control.qsize() >= 2)
        assert await bus.publish_command(worker_id, live_frame({"request_id": "c2"}, 4)) is True
        key = bus._control_key(worker_id, 4)
        assert await bus._connection().ttl(key) > 0
        popped = await asyncio.wait_for(queues.wait_frame(), 1)
        assert popped["request_id"] == "hb"
        assert await bus.publish_command(worker_id, live_frame({"request_id": "c3"}, 4)) is False
        await bus.unbind(worker_id, queues)
        assert await bus._connection().exists(key) == 0
    finally:
        await bus.delete_prefix()
        await bus.aclose()


async def test_sweep_and_reenroll_close_only_the_matching_epoch(environment_id: int):
    gateway = gateway_for(
        MemoryRouteBus(),
        worker_gateway_ping_seconds=60,
        worker_gateway_ping_timeout_seconds=10,
    )
    enrolled, auth = await bring_online(gateway, environment_id)
    matching = FakeWebSocket()
    gateway.sockets[enrolled.worker_instance_id] = _SocketLease(auth.epoch, matching, deque(), 1)
    gateway._close_local_socket(enrolled.worker_instance_id, auth.epoch)
    await asyncio.sleep(0.05)
    assert matching.close_code == 4000

    newer = FakeWebSocket()
    gateway.sockets[enrolled.worker_instance_id] = _SocketLease(auth.epoch + 5, newer, deque(), 1)
    async with SessionLocal() as session:
        await session.execute(
            update(WorkerInstance)
            .where(WorkerInstance.id == enrolled.worker_instance_id)
            .values(last_heartbeat_at=utcnow() - timedelta(seconds=120))
        )
        await session.commit()
    await gateway.sweep(now=utcnow())
    await asyncio.sleep(0.05)
    assert newer.close_code is None
    assert gateway.sockets[enrolled.worker_instance_id].epoch == auth.epoch + 5

    rotated_socket = FakeWebSocket()
    gateway.sockets[enrolled.worker_instance_id] = _SocketLease(9, rotated_socket, deque(), 1)
    rotated = await enroll(gateway, environment_id)
    await asyncio.sleep(0.05)
    assert rotated.rotated is True
    assert rotated_socket.close_code is None
    assert gateway.sockets[enrolled.worker_instance_id].websocket is rotated_socket


class _HoldPublish(MemoryRouteBus):
    def __init__(self) -> None:
        super().__init__()
        self.hold = asyncio.Event()

    async def publish_command(self, worker_instance_id: str, frame: dict) -> bool:
        await self.hold.wait()
        return await super().publish_command(worker_instance_id, frame)


async def test_ack_arriving_before_publish_still_records_published(environment_id: int):
    bus = _HoldPublish()
    gateway = gateway_for(bus)
    enrolled, auth = await bring_online(gateway, environment_id)
    request_id = f"req-ack-{uuid4().hex}"
    task = asyncio.create_task(
        dispatch(gateway, enrolled.worker_instance_id, request_id, "ping")
    )
    row = None
    for _ in range(50):
        async with SessionLocal() as session:
            row = await session.scalar(
                select(WorkerCommand).where(WorkerCommand.request_id == request_id)
            )
        if row is not None and row.delivery_state == "uncertain":
            break
        await asyncio.sleep(0.02)
    assert row is not None and row.delivery_state == "uncertain"
    async with SessionLocal() as session:
        acked = await gateway.apply_response(
            session,
            worker_instance_id=enrolled.worker_instance_id,
            epoch=auth.epoch,
            message=envelope(
                request_id=request_id,
                worker_instance_id=enrolled.worker_instance_id,
                environment_id=environment_id,
                session_id=None,
                message_type="response",
                payload={"stage": "ack", "ok": True, "result": None, "error_code": None},
            ),
        )
    assert acked is True
    bus.hold.set()
    result = await asyncio.wait_for(task, 2)
    assert result.delivered is True
    stored = await load_command(request_id)
    assert stored.status == "accepted"
    assert stored.delivery_state == "published"


class _HangSend(ScriptedTransport):
    async def send(self, message: dict) -> None:
        self.sent.append(message)
        await asyncio.Event().wait()


async def test_auth_send_timeout_closes_transport():
    transport = _HangSend([])
    manager = WorkerConnectionManager(
        _agent_config(auth_timeout_seconds=0.05),
        sleep=lambda _seconds: asyncio.sleep(0),
    )
    with pytest.raises(WorkerTransportError, match="auth_timeout"):
        await manager.run_session(transport)
    assert transport.closed is True


class _DyingPull(RedisRouteBus):
    async def _pull(self, *args, **kwargs) -> None:
        raise RuntimeError("redis unavailable")


@pytest.mark.skipif(
    not REDIS_URL,
    reason="未提供 ASTRA_TEST_REDIS_URL：Worker 路由未在真实 Redis 上联调，默认验证进程内等价语义",
)
async def test_redis_pull_failure_closes_the_socket(environment_id: int):
    prefix = f"astra:worker-gateway:test:{os.getpid()}:{uuid4().hex}"
    bus = _DyingPull(REDIS_URL, prefix=prefix)
    gateway = gateway_for(
        bus,
        worker_gateway_ping_seconds=30,
        worker_gateway_dispatch_lease_seconds=60,
    )
    enrolled = await enroll(gateway, environment_id)
    socket = FakeWebSocket()
    task = asyncio.create_task(gateway.serve_socket(socket))
    await socket.incoming.put(dump_message(auth_message(
        enrolled.worker_instance_id, environment_id, enrolled.token
    )))
    try:
        await asyncio.wait_for(task, 2)
    finally:
        await bus.delete_prefix()
        await bus.aclose()
    assert socket.close_code == 1011


@pytest.mark.skipif(
    not REDIS_URL,
    reason="未提供 ASTRA_TEST_REDIS_URL：Worker 路由未在真实 Redis 上联调，默认验证进程内等价语义",
)
async def test_restore_refreshes_queue_ttl():
    prefix = f"astra:worker-gateway:test:{os.getpid()}:{uuid4().hex}"
    bus = RedisRouteBus(REDIS_URL, prefix=prefix, owner_ttl_seconds=60)
    worker_id = f"wrk_{uuid4().hex}"
    try:
        client = bus._connection()
        list_key = bus._control_key(worker_id, 1)
        held_key = bus._held_key(worker_id, "control", 1)
        await client.eval(_RESTORE_LUA, 2, list_key, held_key, "frame", str(bus._queue_ttl()))
        ttl = await client.ttl(list_key)
        assert ttl > 60
    finally:
        await bus.delete_prefix()
        await bus.aclose()
