"""Phase 2 fault barriers: epoch, owners, republish, gaps, and cursor leases."""

import asyncio
import json
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.database import SessionLocal, init_database
from app.models import (
    AgentWorker,
    OpenCodeExecutionEnvironment,
    Task,
    WorkerCommand,
    WorkerInstance,
    utcnow,
)
from app.runtime.worker_client import WorkerOpenCodeClient
from app.worker_gateway.bus import RedisRouteBus
from app.worker_gateway.data_channel import DataChannel, response_key
from app.worker_gateway.events import EventLog
from app.worker_gateway.protocol import WorkerGatewayError, dump_message, envelope
from tests.platform.test_worker_gateway import (
    REDIS_URL,
    FakeWebSocket,
    auth_message,
    bring_online,
    dispatch,
    gateway_for,
    wait_until,
)


@pytest.fixture
async def environment_id() -> int:
    await init_database()
    async with SessionLocal() as session:
        employee = AgentWorker(id=f"emp-{uuid4().hex[:8]}", name="故障员工", role="执行")
        session.add(employee)
        await session.flush()
        environment = OpenCodeExecutionEnvironment(
            employee_id=employee.id, mode="LINUX_REMOTE", name="故障执行机", worker_channel="worker"
        )
        session.add(environment)
        await session.commit()
        return environment.id


def _event(worker_id: str, environment_id: int, epoch: int, seq: int, directory: str = "/work") -> str:
    message = envelope(
        request_id=f"evt-{epoch}-{seq}",
        worker_instance_id=worker_id,
        environment_id=environment_id,
        session_id=None,
        message_type="event",
        payload={"seq": seq, "directory": directory, "event": {"type": "server.connected"}},
    )
    message.epoch = epoch
    message.expires_at = 10**12
    return dump_message(message)


@pytest.mark.asyncio
async def test_old_epoch_events_and_bodies_do_not_change_the_task(environment_id):
    gateway = gateway_for(worker_gateway_ping_seconds=60, worker_gateway_ping_timeout_seconds=30)
    async with SessionLocal() as session:
        enrolled = await gateway.enroll(session, environment_id)
        employee_id = await session.scalar(
            select(OpenCodeExecutionEnvironment.employee_id).where(
                OpenCodeExecutionEnvironment.id == environment_id
            )
        )
        session.add(
            Task(
                id="T-epoch",
                title="epoch",
                task_type="ISSUE",
                status="RUNNING",
                employee_id=employee_id,
                pending_message_id="msg_epoch000001",
                pending_attempt=1,
            )
        )
        await session.commit()
    socket = FakeWebSocket()
    running = asyncio.create_task(gateway.serve_socket(socket))
    await socket.incoming.put(dump_message(auth_message(
        enrolled.worker_instance_id, environment_id, enrolled.token, "auth-fault"
    )))
    await wait_until(lambda: bool(socket.sent))
    ack = json.loads(socket.sent[-1])
    epoch = ack["payload"]["epoch"]
    log = EventLog(gateway.bus, limit=8)
    await log.touch_cursor(
        worker_id=enrolled.worker_instance_id,
        epoch=epoch,
        directory="/work",
        consumer="browser:test",
        seq=0,
    )
    await socket.incoming.put(_event(enrolled.worker_instance_id, environment_id, epoch - 1, 1))
    await socket.incoming.put(_event(enrolled.worker_instance_id, environment_id, epoch, 1))

    async def saved_log() -> str | None:
        for _ in range(50):
            raw = await gateway.bus.get_text(log._key(enrolled.worker_instance_id, epoch, "/work"))
            if raw:
                return raw
            await asyncio.sleep(0.02)
        return None

    current_raw = await saved_log()
    assert current_raw is not None
    stale = await gateway.bus.get_text(log._key(enrolled.worker_instance_id, epoch - 1, "/work"))
    current = json.loads(current_raw)
    assert stale is None
    assert [item["seq"] for item in current["events"]] == [1]
    assert current["events"][0].get("gap") is not True
    async with SessionLocal() as session:
        sent = await gateway.dispatch(
            session,
            worker_instance_id=enrolled.worker_instance_id,
            request_id="req-old-epoch",
            name="http",
            payload={
                "method": "POST",
                "path": "/session/ses/prompt_async",
                "body": {"messageID": "msg_epoch000001"},
                "attempt_ref": {"task_id": "T-epoch", "message_id": "msg_epoch000001", "attempt": 1},
            },
            session_id="ses",
        )
    assert sent.delivered is True
    with pytest.raises(WorkerGatewayError) as fetched:
        async with SessionLocal() as session:
            await DataChannel(gateway).fetch_request(
                session,
                request_id="req-old-epoch",
                worker_instance_id=enrolled.worker_instance_id,
                epoch=epoch - 1,
            )
    assert fetched.value.code == "epoch_conflict"
    with pytest.raises(WorkerGatewayError) as uploaded:
        async with SessionLocal() as session:
            await DataChannel(gateway).commit_upload(
                session,
                request_id="req-old-epoch",
                worker_instance_id=enrolled.worker_instance_id,
                epoch=epoch - 1,
                upload_id="up-old",
                http_status=204,
                headers={},
                content_type=None,
                chunk="",
                complete=True,
            )
    assert uploaded.value.code == "epoch_conflict"
    async with SessionLocal() as session:
        task = await session.get(Task, "T-epoch")
    assert task.status == "RUNNING"
    assert task.submission_attempts is None
    await socket.close()
    await asyncio.wait_for(running, 2)


@pytest.mark.asyncio
async def test_second_owner_cannot_register_or_republish(environment_id):
    bus_owner = gateway_for()
    other = gateway_for(bus_owner.bus)
    enrolled, auth = await bring_online(bus_owner, environment_id)
    assert await bus_owner.bus.set_owner(enrolled.worker_instance_id, other.owner_id, auth.epoch) is False
    assert await other.dispatch_due(enrolled.worker_instance_id, auth.epoch) == 0
    sent = await dispatch(bus_owner, enrolled.worker_instance_id, "req-owner", "ping")
    assert sent.delivered is True


@pytest.mark.asyncio
async def test_side_effect_http_is_not_republished_after_publish(environment_id):
    gateway = gateway_for(worker_gateway_dispatch_lease_seconds=10)
    enrolled, auth = await bring_online(gateway, environment_id)
    queues = gateway.bus.bound[enrolled.worker_instance_id]
    post = await dispatch(
        gateway, enrolled.worker_instance_id, "req-post", "http", {"method": "POST", "path": "/x", "body": {}}
    )
    get = await dispatch(
        gateway, enrolled.worker_instance_id, "req-get", "http", {"method": "GET", "path": "/x", "body": {}}
    )
    assert post.delivered is True and get.delivered is True
    assert (await queues.wait_frame())["request_id"] == "req-post"
    assert (await queues.wait_frame())["request_id"] == "req-get"
    async with SessionLocal() as session:
        await session.execute(
            update(WorkerCommand)
            .where(WorkerCommand.request_id.in_(("req-post", "req-get")))
            .values(last_dispatched_at=utcnow() - timedelta(seconds=30))
        )
        await session.commit()
    assert await gateway.dispatch_due(enrolled.worker_instance_id, auth.epoch) == 1
    assert (await queues.wait_frame())["request_id"] == "req-get"


@pytest.mark.asyncio
async def test_crashed_consumer_retries_get_and_new_epoch_drops_the_old_list(environment_id):
    gateway = gateway_for(worker_gateway_dispatch_lease_seconds=10)
    enrolled, auth = await bring_online(gateway, environment_id)
    queues = gateway.bus.bound[enrolled.worker_instance_id]
    sent = await dispatch(
        gateway,
        enrolled.worker_instance_id,
        "req-get-crash",
        "http",
        {"method": "GET", "path": "/health", "body": {}},
    )
    assert sent.delivered is True
    assert (await queues.wait_frame())["request_id"] == "req-get-crash"
    await gateway.bus.unbind(enrolled.worker_instance_id, queues)
    rebound = await gateway.bus.bind(
        enrolled.worker_instance_id, epoch=auth.epoch, owner_id=gateway.owner_id
    )
    assert rebound is not None
    assert rebound.control.empty()
    async with SessionLocal() as session:
        await session.execute(
            update(WorkerCommand)
            .where(WorkerCommand.request_id == "req-get-crash")
            .values(last_dispatched_at=utcnow() - timedelta(seconds=30))
        )
        await session.commit()
    assert await gateway.dispatch_due(enrolled.worker_instance_id, auth.epoch) == 1
    assert (await rebound.wait_frame())["request_id"] == "req-get-crash"
    async with SessionLocal() as session:
        instance = await session.get(WorkerInstance, enrolled.worker_instance_id)
        instance.epoch = auth.epoch + 1
        await session.commit()
    assert await gateway.bus.set_owner(enrolled.worker_instance_id, gateway.owner_id, auth.epoch + 1) is True
    fresh = await gateway.bus.bind(
        enrolled.worker_instance_id, epoch=auth.epoch + 1, owner_id=gateway.owner_id
    )
    assert fresh is not None and fresh.control.empty()
    assert await gateway.dispatch_due(enrolled.worker_instance_id, auth.epoch) == 0
    assert await gateway.dispatch_due(enrolled.worker_instance_id, auth.epoch + 1) == 0


@pytest.mark.asyncio
async def test_event_gap_yields_one_refresh_signal(environment_id):
    gateway = gateway_for()
    enrolled, auth = await bring_online(gateway, environment_id)
    log = EventLog(gateway.bus, limit=8)
    await log.append(
        worker_id=enrolled.worker_instance_id,
        epoch=auth.epoch,
        directory="/work",
        payload={"seq": 1, "event": {"type": "server.connected"}},
    )
    gap = await log.append(
        worker_id=enrolled.worker_instance_id,
        epoch=auth.epoch,
        directory="/work",
        payload={"seq": 3, "event": {"type": "session.status"}},
    )
    assert gap["action"] == "gap"
    client = WorkerOpenCodeClient(environment_id=environment_id, gateway=gateway, timeout_seconds=2)

    async def ready(*_args, **_kwargs):
        return {"subscribed": True}

    client.run_command = ready
    events = client.events("/work")
    first = await asyncio.wait_for(events.__anext__(), 1)
    second = await asyncio.wait_for(events.__anext__(), 1)
    await events.aclose()
    assert first["type"] == "server.connected"
    assert second["type"] == "astra.event_gap"
    assert await client.claim_gap_refresh("/work") is True
    assert await client.claim_gap_refresh("/work") is False
    assert await client.finish_gap_refresh("/work") is True
    assert await client.finish_gap_refresh("/work") is False


@pytest.mark.asyncio
async def test_epoch_switch_after_claim_rejects_the_old_side_effect(environment_id):
    gateway = gateway_for()
    enrolled, auth = await bring_online(gateway, environment_id)
    sent = await dispatch(
        gateway,
        enrolled.worker_instance_id,
        "req-claimed",
        "http",
        {"method": "POST", "path": "/session/ses/prompt_async", "body": {"messageID": "msg_claimed0001"}},
    )
    assert sent.delivered is True
    async with SessionLocal() as session:
        claimed = await session.scalar(select(WorkerCommand).where(WorkerCommand.request_id == "req-claimed"))
        attempts = claimed.delivery_attempts
    async with SessionLocal() as session:
        instance = await session.get(WorkerInstance, enrolled.worker_instance_id)
        instance.epoch = auth.epoch + 1
        await session.commit()
    with pytest.raises(WorkerGatewayError) as error:
        async with SessionLocal() as session:
            await DataChannel(gateway).fetch_request(
                session,
                request_id="req-claimed",
                worker_instance_id=enrolled.worker_instance_id,
                epoch=auth.epoch,
            )
    assert error.value.code == "epoch_conflict"
    assert await gateway.dispatch_due(enrolled.worker_instance_id, auth.epoch + 1) == 0
    async with SessionLocal() as session:
        stored = await session.scalar(select(WorkerCommand).where(WorkerCommand.request_id == "req-claimed"))
    assert stored.epoch == auth.epoch
    assert stored.delivery_attempts == attempts


@pytest.mark.asyncio
async def test_epoch_switch_during_upload_keeps_the_winner_body(environment_id):
    gateway = gateway_for()
    enrolled, auth = await bring_online(gateway, environment_id)
    async with SessionLocal() as session:
        employee_id = await session.scalar(
            select(OpenCodeExecutionEnvironment.employee_id).where(
                OpenCodeExecutionEnvironment.id == environment_id
            )
        )
        session.add(
            Task(id="T-upload", title="up", task_type="ISSUE", status="RUNNING", employee_id=employee_id)
        )
        await session.commit()
        await gateway.dispatch(
            session,
            worker_instance_id=enrolled.worker_instance_id,
            request_id="req-mid",
            name="http",
            payload={"method": "GET", "path": "/session", "body": {}},
        )
    async with SessionLocal() as session:
        await DataChannel(gateway).commit_upload(
            session,
            request_id="req-mid",
            worker_instance_id=enrolled.worker_instance_id,
            epoch=auth.epoch,
            upload_id="up-win",
            http_status=200,
            headers={},
            content_type="application/json",
            chunk="[]",
            complete=True,
        )
        await DataChannel(gateway).commit_upload(
            session,
            request_id="req-mid",
            worker_instance_id=enrolled.worker_instance_id,
            epoch=auth.epoch,
            upload_id="up-partial",
            http_status=200,
            headers={},
            content_type="application/json",
            chunk="no",
            complete=False,
        )
    async with SessionLocal() as session:
        instance = await session.get(WorkerInstance, enrolled.worker_instance_id)
        instance.epoch = auth.epoch + 1
        await session.commit()
        with pytest.raises(WorkerGatewayError):
            await DataChannel(gateway).commit_upload(
                session,
                request_id="req-mid",
                worker_instance_id=enrolled.worker_instance_id,
                epoch=auth.epoch,
                upload_id="up-partial",
                http_status=200,
                headers={},
                content_type="application/json",
                chunk="more",
                complete=True,
            )
    assert await gateway.bus.get_text(response_key("req-mid", "up-win"))
    assert await gateway.bus.get_text(response_key("req-mid", "up-partial")) is None
    async with SessionLocal() as session:
        task = await session.get(Task, "T-upload")
    assert task.status == "RUNNING"


@pytest.mark.asyncio
async def test_expired_event_cursor_releases_pause(environment_id):
    bus = gateway_for().bus
    log = EventLog(bus, limit=2, lease_seconds=30)
    await log.append(worker_id="wrk", epoch=1, directory="/work", payload={"seq": 1, "event": {"type": "a"}})
    await log.append(worker_id="wrk", epoch=1, directory="/work", payload={"seq": 2, "event": {"type": "b"}})
    await log.touch_cursor(worker_id="wrk", epoch=1, directory="/work", consumer="browser:1", seq=0)
    assert await log.control(worker_id="wrk", epoch=1, directory="/work") == "pause_events"
    key = log._cursor_key("wrk", 1, "/work", "browser:1")
    await bus.put_text(key, json.dumps({"seq": 0, "expires_at": 1}), 30)
    assert await log.control(worker_id="wrk", epoch=1, directory="/work") == "stop_events"
    await log.append(worker_id="wrk", epoch=1, directory="/work", payload={"seq": 1, "event": {"type": "a"}})
    await log.append(worker_id="wrk", epoch=1, directory="/work", payload={"seq": 2, "event": {"type": "b"}})
    assert await log.control(worker_id="wrk", epoch=1, directory="/work") == "stop_events"


@pytest.mark.asyncio
@pytest.mark.skipif(
    not REDIS_URL,
    reason="未提供 ASTRA_TEST_REDIS_URL：Worker 路由未在真实 Redis 上联调，默认验证进程内等价语义",
)
async def test_redis_crashed_consumer_releases_capacity_and_drops_the_old_list(environment_id):
    prefix = f"astra:worker-gateway:test:{os.getpid()}:{uuid4().hex}"
    bus = RedisRouteBus(REDIS_URL, prefix=prefix, control_limit=1, event_limit=1)
    gateway = gateway_for(bus, worker_gateway_dispatch_lease_seconds=10)
    try:
        enrolled, auth = await bring_online(gateway, environment_id)
        worker_id = enrolled.worker_instance_id
        queues = gateway.bus.bound[worker_id]
        sent = await dispatch(
            gateway,
            worker_id,
            "req-redis-get",
            "http",
            {"method": "GET", "path": "/health", "body": {}},
        )
        assert sent.delivered is True
        frame = await asyncio.wait_for(queues.wait_frame(), 2)
        assert frame["request_id"] == "req-redis-get"
        held = bus._held_key(worker_id, "control", auth.epoch)
        old_list = bus._control_key(worker_id, auth.epoch)
        assert int(await bus._connection().get(held) or 0) >= 1
        await gateway.bus.unbind(worker_id, queues)
        assert await bus._connection().exists(held) == 0
        assert await bus._connection().exists(old_list) == 0
        rebound = await gateway.bus.bind(worker_id, epoch=auth.epoch, owner_id=gateway.owner_id)
        assert rebound is not None
        async with SessionLocal() as session:
            await session.execute(
                update(WorkerCommand)
                .where(WorkerCommand.request_id == "req-redis-get")
                .values(last_dispatched_at=utcnow() - timedelta(seconds=30))
            )
            await session.commit()
        assert await gateway.dispatch_due(worker_id, auth.epoch) == 1
        again = await asyncio.wait_for(rebound.wait_frame(), 2)
        assert again["request_id"] == "req-redis-get"
        await rebound.release_delivered(again)
        await gateway.bus.unbind(worker_id, rebound)
        async with SessionLocal() as session:
            instance = await session.get(WorkerInstance, worker_id)
            instance.epoch = auth.epoch + 1
            await session.commit()
        assert await gateway.bus.set_owner(worker_id, gateway.owner_id, auth.epoch + 1) is True
        fresh = await gateway.bus.bind(worker_id, epoch=auth.epoch + 1, owner_id=gateway.owner_id)
        assert fresh is not None and fresh.control.empty()
        assert await gateway.dispatch_due(worker_id, auth.epoch) == 0
        assert await gateway.dispatch_due(worker_id, auth.epoch + 1) == 0
        new_held = bus._held_key(worker_id, "control", auth.epoch + 1)
        assert await bus._connection().exists(old_list) == 0
        assert await bus._connection().exists(new_held) == 0
    finally:
        await bus.delete_prefix()
        await bus.aclose()
