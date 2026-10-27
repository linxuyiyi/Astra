import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain import TaskStatus
from app.models import Base, Event, Task, TaskWakeOutbox
from app.runtime.task_wakeup import HttpWakeSender, WakePermanent, WakeRetry, deliver_pending, retry_delay
from app.services import TaskService
from tests.platform.test_runtime import new_task


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


class RecordingSender:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.sent: list[str] = []

    async def send(self, row: TaskWakeOutbox) -> None:
        if self.error:
            raise self.error
        self.sent.append(row.task_id)


async def test_waiting_notification_is_chinese_without_changing_status_codes(session):
    task = await TaskService(session).create(new_task(responsible_employee_id="emp_1"))
    await TaskService(session).transition(task, TaskStatus.WAITING)
    event = await session.scalar(select(Event).where(Event.event_type == "TASK_WAITING"))
    row = await session.scalar(select(TaskWakeOutbox))
    assert event.message == "任务状态从待执行变更为等待人工或外部条件"
    assert event.detail == {"from": "PENDING", "to": "WAITING"}
    assert "状态变为等待人工或外部条件" in row.hint
    assert "WAITING" not in row.hint
    assert task.status == "WAITING"


async def test_delivery_marks_row_without_changing_task_status(session):
    task = await TaskService(session).create(new_task(responsible_employee_id="emp_1"))
    await TaskService(session).transition(task, TaskStatus.FAILED)
    sender = RecordingSender()

    delivered = await deliver_pending(session, sender)

    assert delivered == 1
    assert sender.sent == [task.id]
    current = await session.get(Task, task.id)
    assert current.status == TaskStatus.FAILED.value
    row = await session.scalar(select(TaskWakeOutbox))
    assert row.status == "delivered"


async def test_missing_session_stays_pending_for_retry(session):
    task = await TaskService(session).create(
        new_task(responsible_employee_id="emp_1", source_session_id="missing")
    )
    await TaskService(session).transition(task, TaskStatus.WAITING)
    sender = RecordingSender(WakeRetry("没有可唤醒的会话"))

    delivered = await deliver_pending(session, sender)

    assert delivered == 0
    row = await session.scalar(select(TaskWakeOutbox))
    assert row.status == "pending"
    assert row.attempts == 1
    current = await session.get(Task, task.id)
    assert current.status == TaskStatus.WAITING.value
    assert row.next_retry_at > row.updated_at


def test_retry_delay_is_capped():
    assert retry_delay(1, base=5, cap=300) == 5
    assert retry_delay(3, base=5, cap=300) == 20
    assert retry_delay(31, base=5, cap=300) == 300


async def test_transient_errors_stay_pending_after_many_attempts(session):
    task = await TaskService(session).create(new_task(responsible_employee_id="emp_1"))
    await TaskService(session).transition(task, TaskStatus.FAILED)
    moment = datetime.now(UTC)
    sender = RecordingSender(WakeRetry("暂时不可达"))
    for _ in range(31):
        assert await deliver_pending(session, sender, now=moment) == 0
        row = await session.scalar(select(TaskWakeOutbox))
        moment = row.next_retry_at
    assert row.status == "pending"
    assert row.attempts == 31


async def test_permanent_error_is_not_retried(session):
    task = await TaskService(session).create(new_task(responsible_employee_id="emp_1"))
    await TaskService(session).transition(task, TaskStatus.FAILED)
    sender = RecordingSender(WakePermanent("请求无效"))

    assert await deliver_pending(session, sender) == 0
    row = await session.scalar(select(TaskWakeOutbox))
    assert row.status == "failed"
    assert await deliver_pending(session, sender, now=datetime.now(UTC) + timedelta(hours=1)) == 0
    assert sender.sent == []


async def test_claim_prevents_a_second_delivery(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'wake.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    class Gate:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.sent: list[int] = []

        async def send(self, row: TaskWakeOutbox) -> None:
            self.sent.append(row.id)
            self.started.set()
            await self.release.wait()

    gate = Gate()
    other = RecordingSender()
    async with factory() as first_session, factory() as second_session:
        task = await TaskService(first_session).create(new_task(responsible_employee_id="emp_1"))
        await TaskService(first_session).transition(task, TaskStatus.FAILED)
        running = asyncio.create_task(deliver_pending(first_session, gate))
        await gate.started.wait()
        assert await deliver_pending(second_session, other) == 0
        gate.release.set()
        assert await running == 1
    assert len(gate.sent) == 1
    assert other.sent == []
    await engine.dispose()


async def test_wake_post_includes_event_id_and_retries_disabled_employee(session):
    task = await TaskService(session).create(
        new_task(responsible_employee_id="emp_1", source_session_id="ses_1")
    )
    await TaskService(session).transition(task, TaskStatus.FAILED)
    row = await session.scalar(select(TaskWakeOutbox))
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wake-target"):
            return httpx.Response(409, text="员工已禁用，稍后再投递")
        seen["event"] = request.headers.get("x-nexus-event-id", "")
        seen["body"] = request.content.decode()
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(WakeRetry):
            await HttpWakeSender(client).send(row)

    def ok(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wake-target"):
            return httpx.Response(200, json={"session_id": "ses_1", "agent_id": "agt_1"})
        seen["event"] = request.headers.get("x-nexus-event-id", "")
        seen["body"] = json.loads(request.content.decode())["event_id"]
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(ok)) as client:
        await HttpWakeSender(client).send(row)
    assert seen["event"] == str(row.event_id)
    assert seen["body"] == str(row.event_id)


async def test_group_wake_posts_canonical_event_without_agent_inference(session):
    task = await TaskService(session).create(new_task(responsible_employee_id="emp_1"))
    await TaskService(session).transition(task, TaskStatus.FAILED)
    row = await session.scalar(select(TaskWakeOutbox))
    seen = []

    def handler(request):
        if request.url.path.endswith("/wake-target"):
            return httpx.Response(200, json={"channel_id": "group-a"})
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await HttpWakeSender(client).send(row)
    assert seen == [("/api/v1/internal/group-task-event", {"event_id": row.event_id})]
