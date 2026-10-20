from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.settings import get_settings
from app.models import AgentWorker, Base, RuntimeSession, Task
from app.services import dashboard_metrics
from app.usage import day_bounds, employee_usage, task_usage


@pytest.fixture
async def usage_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    today = datetime.now(ZoneInfo(get_settings().timezone)).date().isoformat()
    async with factory() as session:
        session.add(AgentWorker(id="E1", name="Engineer", role="build", status="IDLE"))
        session.add_all(
            [
                Task(
                    id="T1",
                    title="ok",
                    task_type="ISSUE",
                    status="SUCCEEDED",
                    employee_id="E1",
                    finished_at=now,
                    completed_at=now,
                ),
                Task(
                    id="T2",
                    title="bad",
                    task_type="ISSUE",
                    status="FAILED",
                    employee_id="E1",
                    finished_at=now,
                ),
            ]
        )
        session.add_all(
            [
                RuntimeSession(
                    id="ses_1",
                    task_id="T1",
                    employee_id="E1",
                    workspace="/tmp/ws",
                    environment={},
                    status="SUCCEEDED",
                    started_at=now - timedelta(minutes=10),
                    finished_at=now,
                    input_tokens=100,
                    output_tokens=40,
                    cache_read_tokens=500,
                    total_tokens=640,
                    llm_call_count=3,
                    tool_call_count=4,
                    tool_usage=[
                        {
                            "tool_name": "bash",
                            "call_count": 3,
                            "success_count": 3,
                            "failure_count": 0,
                            "duration_ms": 120,
                        },
                        {
                            "tool_name": "read",
                            "call_count": 1,
                            "success_count": 0,
                            "failure_count": 1,
                            "duration_ms": 5,
                        },
                    ],
                    usage_by_day=[
                        {"date": today, "total_tokens": 640, "tool_call_count": 4}
                    ],
                    model_usage=[
                        {
                            "model_name": "provider/model-a",
                            "total_tokens": 640,
                            "llm_call_count": 3,
                            "tool_call_count": 4,
                        }
                    ],
                ),
                RuntimeSession(
                    id="ses_2",
                    task_id="T2",
                    employee_id="E1",
                    workspace="/tmp/ws",
                    environment={},
                    status="FAILED",
                    started_at=now - timedelta(minutes=5),
                    finished_at=now,
                    total_tokens=60,
                    tool_call_count=1,
                    tool_usage=[
                        {
                            "tool_name": "bash",
                            "call_count": 1,
                            "success_count": 0,
                            "failure_count": 1,
                            "duration_ms": 9,
                        }
                    ],
                    usage_by_day=[
                        {"date": today, "total_tokens": 60, "tool_call_count": 1}
                    ],
                    model_usage=[
                        {
                            "model_name": "provider/model-b",
                            "total_tokens": 60,
                            "llm_call_count": 0,
                            "tool_call_count": 1,
                        }
                    ],
                ),
            ]
        )
        await session.commit()
        yield session
    await engine.dispose()


async def test_today_work_duration_follows_task_span_not_short_session():
    """A 15-minute task must not shrink to the 2-minute RuntimeSession row."""

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    start, _end = day_bounds(1)
    task_started = start + timedelta(hours=1)
    task_finished = task_started + timedelta(minutes=15)
    today = task_finished.astimezone(ZoneInfo(get_settings().timezone)).date().isoformat()
    async with factory() as session:
        session.add(AgentWorker(id="E1", name="Engineer", role="build", status="IDLE"))
        session.add(
            Task(
                id="T1",
                title="远程执行",
                task_type="ISSUE",
                status="SUCCEEDED",
                employee_id="E1",
                started_at=task_started,
                finished_at=task_finished,
                completed_at=task_finished,
            )
        )
        session.add(
            RuntimeSession(
                id="ses_short",
                task_id="T1",
                employee_id="E1",
                workspace="/tmp/ws",
                environment={},
                status="SUCCEEDED",
                started_at=task_finished - timedelta(minutes=2),
                finished_at=task_finished,
                execution_seconds=120,
                usage_by_day=[{"date": today, "total_tokens": 10}],
            )
        )
        await session.commit()
        result = await employee_usage(session, "E1", 1)
        assert result["summary"]["duration_seconds"] == 15 * 60
        assert result["trend"][-1]["duration_seconds"] == 15 * 60
    await engine.dispose()


async def test_employee_usage_aggregates_runtime_sessions(usage_session):
    result = await employee_usage(usage_session, "E1", 7)
    summary = result["summary"]
    assert summary["total_tokens"] == 700
    assert summary["input_tokens"] == 100
    assert summary["tool_call_count"] == 5
    assert summary["tool_success_count"] == 3
    assert summary["tool_failure_count"] == 2
    assert summary["task_count"] == 2
    assert summary["completed_tasks"] == 1
    assert summary["duration_seconds"] >= 900
    assert result["tools"][0]["tool_name"] == "bash"
    assert result["tools"][0]["call_count"] == 4
    assert len(result["trend"]) == 7


async def test_task_usage_counts_sessions(usage_session):
    result = await task_usage(usage_session, "T1")
    assert result["sessions"] == 1
    assert result["summary"]["total_tokens"] == 640


async def test_dashboard_metrics_use_session_usage(usage_session):
    metrics = await dashboard_metrics(usage_session)
    assert metrics["today_tokens"] == 700
    assert metrics["today_tool_calls"] == 5
    # Business completion (immutable completed_at) vs Session completion.
    assert metrics["completed_today"] == 1
    assert metrics["completed_sessions_today"] == 1
    assert metrics["failed_sessions_today"] == 1