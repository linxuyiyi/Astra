import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.assistant import tool_gateway
from app.models import AgentWorker, Base, RuntimeSession, Task


@pytest.fixture
async def session_tools_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(AgentWorker(id="E1", name="Engineer", role="build", status="IDLE"))
        session.add_all(
            [
                Task(id="T1", title="ok", task_type="ISSUE", status="SUCCEEDED", employee_id="E1"),
                Task(id="T2", title="bad", task_type="ISSUE", status="FAILED", employee_id="E1"),
            ]
        )
        session.add_all(
            [
                RuntimeSession(
                    id="ses_ok",
                    task_id="T1",
                    employee_id="E1",
                    workspace="/tmp/ws",
                    environment={},
                    status="SUCCEEDED",
                    model_name="deepseek/deepseek-flash",
                    total_tokens=640,
                    tool_call_count=4,
                ),
                RuntimeSession(
                    id="ses_bad",
                    task_id="T2",
                    employee_id="E1",
                    workspace="/tmp/ws",
                    environment={},
                    status="INTERRUPTED",
                    terminal_reason="timeout",
                ),
            ]
        )
        await session.commit()
        yield session
    await engine.dispose()


async def test_session_search_and_get(session_tools_session):
    found = await tool_gateway.session_search(session_tools_session)
    assert found["total"] == 2
    assert {item["session_id"] for item in found["items"]} == {"ses_ok", "ses_bad"}

    filtered = await tool_gateway.session_search(
        session_tools_session, task_id="T1", status=["succeeded"]
    )
    assert [item["session_id"] for item in filtered["items"]] == ["ses_ok"]

    detail = await tool_gateway.session_get(session_tools_session, "ses_ok")
    assert detail["session_id"] == "ses_ok"
    assert detail["task_id"] == "T1"
    assert detail["card"]["card_type"] == "session"
    assert detail["card"]["status"] == "SUCCEEDED"


async def test_session_get_missing(session_tools_session):
    with pytest.raises(ValueError):
        await tool_gateway.session_get(session_tools_session, "ses_missing")


async def test_session_failure_summary(session_tools_session):
    result = await tool_gateway.session_failure_summary(session_tools_session)
    assert result["total"] == 1
    item = result["items"][0]
    assert item["session_id"] == "ses_bad"
    assert item["terminal_reason"] == "timeout"
    assert item["task_status"] == "FAILED"