import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain import TaskStatus
from app.models import Base, Event, Task, TaskWakeOutbox
from app.runtime.task_wakeup import WakeRetry, deliver_pending, retry_delay
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
    task = await TaskService(session).create(new_task(origin_employee_id="emp_1"))
    await TaskService(session).transition(task, TaskStatus.RUNNING)
    await TaskService(session).transition(task, TaskStatus.WAITING)
    event = await session.scalar(select(Event).where(Event.event_type == "TASK_WAITING"))
    row = await session.scalar(select(TaskWakeOutbox))
    assert event.message == "任务状态从执行中变更为等待人工或外部条件"
    assert event.detail == {"from": "RUNNING", "to": "WAITING"}
    assert "状态变为等待人工或外部条件" in row.hint
    assert "WAITING" not in row.hint
    assert task.status == "WAITING"


async def test_missing_session_stays_pending_for_retry(session):
    task = await TaskService(session).create(
        new_task(origin_employee_id="emp_1", source="opencode", external_task_id="missing")
    )
    await TaskService(session).transition(task, TaskStatus.RUNNING)
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
