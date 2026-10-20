from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.executors.usage import parse_usage_breakdowns
from app.models import AgentWorker, Base, RuntimeSession
from app.usage import summarize_sessions


def _ms(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp() * 1000


def test_parse_usage_breakdowns_uses_real_message_day_and_model():
    timezone = ZoneInfo("UTC")
    messages = [
        {
            "info": {
                "role": "assistant",
                "modelID": "m1",
                "providerID": "p",
                "time": {"created": _ms(2026, 1, 1, 23, 50)},
                "tokens": {"input": 10, "output": 5},
            },
            "parts": [],
        },
        {
            "info": {
                "role": "assistant",
                "modelID": "m2",
                "providerID": "p",
                "time": {"created": _ms(2026, 1, 2, 0, 10)},
                "tokens": {"input": 3, "output": 2},
            },
            "parts": [{"type": "tool", "tool": "bash"}],
        },
    ]

    days, models = parse_usage_breakdowns(
        messages, timezone=timezone, fallback_date="2026-01-01"
    )

    by_day = {entry["date"]: entry for entry in days}
    assert set(by_day) == {"2026-01-01", "2026-01-02"}
    assert by_day["2026-01-01"]["total_tokens"] == 15
    assert by_day["2026-01-02"]["total_tokens"] == 5
    assert by_day["2026-01-02"]["tool_call_count"] == 1

    by_model = {entry["model_name"]: entry for entry in models}
    assert set(by_model) == {"p/m1", "p/m2"}
    assert by_model["p/m1"]["total_tokens"] == 15
    assert by_model["p/m2"]["llm_call_count"] == 1


@pytest.fixture
async def usage_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(AgentWorker(id="E1", name="Engineer", role="build", status="IDLE"))
        await session.commit()
        yield session
    await engine.dispose()


async def test_unknown_execution_seconds_stay_unknown(usage_db):
    session = usage_db
    session.add_all(
        [
            RuntimeSession(
                id="ses_known",
                task_id="T1",
                employee_id="E1",
                workspace="/tmp/ws",
                environment={},
                status="SUCCEEDED",
                started_at=datetime(2026, 1, 1, 9, tzinfo=UTC),
                finished_at=datetime(2026, 1, 1, 9, 5, tzinfo=UTC),
                execution_seconds=120,
            ),
            RuntimeSession(
                id="ses_unknown",
                task_id="T2",
                employee_id="E1",
                workspace="/tmp/ws",
                environment={},
                status="SUCCEEDED",
                started_at=datetime(2026, 1, 1, 10, tzinfo=UTC),
                finished_at=datetime(2026, 1, 1, 10, 5, tzinfo=UTC),
                execution_seconds=None,
            ),
        ]
    )
    await session.commit()

    sessions = (await session.execute(_select_sessions())).scalars().all()
    summary = await summarize_sessions(session, sessions)

    assert summary["duration_seconds"] == 600
    # Unknown execution time is never folded into idle time.
    assert summary["execution_seconds"] is None
    assert summary["idle_seconds"] is None


def _select_sessions():
    from sqlalchemy import select

    return select(RuntimeSession)