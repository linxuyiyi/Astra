import asyncio
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
import websockets
from websockets.exceptions import ConnectionClosed

from app.worker_agent.connection import AgentConfig, WorkerConnectionManager
from app.worker_agent.supervisor import RuntimeSupervisor
from app.worker_agent.transport import (
    WebSocketTransport,
    WorkerTransportError,
    translate_transport_error,
)
from app.worker_gateway.admin_key import ENV_NAME, provision_worker_gateway_admin_key
from app.worker_gateway.bus import _RESTORE_LUA, MemoryRouteBus, RedisRouteBus
from app.worker_gateway.protocol import dump_message, envelope
from app.worker_gateway.service import _SocketLease
from tests.platform.test_worker_gateway import (
    ADMIN_HEADERS,
    FakeWebSocket,
    ScriptedTransport,
    gateway_for,
    live_frame,
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
            agent_worker_id="wrk_agent",
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
        agent_worker_id="wrk_agent",
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


async def test_agent_dedupes_commands_and_ping_timeout_reconnects():
    config = AgentConfig(
        gateway_url="ws://nexus.internal/api/worker-gateway/connect",
        agent_worker_id="wrk_agent",
        token="wtk_test",
        ping_seconds=0.05,
        ping_timeout_seconds=0.05,
        heartbeat_misses=1,
        auth_timeout_seconds=1,
    )

    def auth_reply(ping: float) -> dict:
        return envelope(
            request_id="auth-wrk_agent",
            agent_worker_id="wrk_agent",
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
        agent_worker_id="wrk_agent",
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
        "agent_worker_id": "wrk_agent",
        "token": "wtk_test",
        "ping_seconds": 3600,
        "auth_timeout_seconds": 1,
    }
    values.update(overrides)
    return AgentConfig(**values)


def _auth_reply() -> dict:
    return envelope(
        request_id="auth-wrk_agent",
        agent_worker_id="wrk_agent",
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


class _LostReplyBus(MemoryRouteBus):
    def __init__(self) -> None:
        super().__init__()
        self.lose_reply = False

    async def publish_command(self, agent_worker_id: str, frame: dict) -> bool:
        if self.lose_reply:
            raise RuntimeError("reply lost")
        return await super().publish_command(agent_worker_id, frame)


class _BoomSocket(FakeWebSocket):
    async def send_text(self, data: str) -> None:
        self.sent.append(data)
        if len(self.sent) >= 2:
            raise ConnectionError("send failed")


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
        agent_worker_id="wrk_agent",
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


class _HoldPublish(MemoryRouteBus):
    def __init__(self) -> None:
        super().__init__()
        self.hold = asyncio.Event()

    async def publish_command(self, agent_worker_id: str, frame: dict) -> bool:
        await self.hold.wait()
        return await super().publish_command(agent_worker_id, frame)


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
