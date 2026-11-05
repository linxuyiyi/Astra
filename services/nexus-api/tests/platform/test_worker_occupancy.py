"""Worker occupancy stays until success or a matching human release."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain import TaskStatus
from app.models import Base, Task
from app.runtime.occupancy import task_occupies


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


def _task(**overrides):
    values = dict(
        id="T1",
        title="occupied",
        task_type="ISSUE",
        category_id="testing",
        status=TaskStatus.CANCELED,
        agent_worker_id="E1",
        claimed_at=datetime.now(UTC),
        current_session_id="ses_1",
    )
    values.update(overrides)
    return Task(**values)


def test_unclaimed_pending_does_not_occupy():
    task = _task(
        status=TaskStatus.PENDING,
        claimed_at=None,
        current_session_id=None,
        pending_message_id=None,
        agent_worker_id="E1",
    )
    assert task_occupies(task) is False
