"""星系首页只读投影的聚合契约测试。

不启动 AgentScope，也不经真实业务写入口：直接准备员工目录、Worker、任务和
职责请求事实，验证投影只读取、不推断、不修改。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base
from app.sql_storage import create_test_schema
from nexus_api import nebula
from nexus_api.db import connect


def _insert_employee(connection, employee_id: str, name: str, agent_id: str | None, enabled: bool = True):
    from nexus_api.db_models import employees

    connection.execute(
        employees.insert().values(
            id=employee_id,
            name=name,
            domain=f"{name}领域",
            description=f"{name}职责",
            instructions="直接回答",
            avatar="✦",
            enabled=int(enabled),
            knowledge_json="[]",
            skills_json="[]",
            mcp_json="[]",
            tools_json="[]",
            agentscope_agent_id=agent_id,
            mailbox_session_id=f"mb_{employee_id}",
            capability_json="{}",
            model_route_id="",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
    )
    connection.commit()


@pytest.fixture()
async def facts(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'platform.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    chat_path = tmp_path / "nexus.db"
    create_test_schema(chat_path)
    chat = connect(chat_path)
    try:
        yield sessions, chat
    finally:
        chat.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_collaboration_dedupes_directions_and_hides_unowned_conversations(facts):
    sessions, chat = facts
    _insert_employee(chat, "e1", "星一", "agent-1")
    _insert_employee(chat, "e2", "星二", "agent-2")
    chat.execute(
        sql_text("""
        INSERT INTO t_nexus_conversations (
            id, employee_id, owner_id, kind, name, created_at, updated_at
        ) VALUES (
            'conv-1', 'e1', 'u1', 'private', '对话',
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
        )
        """)
    )
    now = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    rows = [
        ("d1", "private:conv-1", "agent-1", "agent-2", "请协助", "2026-01-02T10:00:00+00:00"),
        ("d2", "private:conv-1", "agent-1", "agent-2", "请协助", "2026-01-02T10:05:00+00:00"),
        ("d3", "private:conv-1", "agent-2", "agent-1", "收到", "2026-01-02T11:00:00+00:00"),
        ("d4", "private:conv-1", "agent-1", "agent-2", "另一件事", "2025-12-31T10:00:00+00:00"),
        ("d5", "private:conv-1", "agent-1", "ghost", "找不到人", "2026-01-02T11:10:00+00:00"),
        ("d6", "private:conv-1", "agent-1", "agent-1", "自言自语", "2026-01-02T11:20:00+00:00"),
    ]
    from nexus_api.db_models import peer_deliveries

    columns = ["id", "scope", "sender_agent_id", "target_agent_id", "message_key", "created_at"]
    chat.execute(peer_deliveries.insert(), [dict(zip(columns, row, strict=True)) for row in rows])
    chat.commit()
    async with sessions() as session:
        snapshot = await nebula.collect_snapshot(session, chat, now=now)
        detail = await nebula.collect_collaborations(session, chat, owner_id="u1", now=now)

    edges = snapshot["collaborationEdges"]
    assert len(edges) == 1
    edge = edges[0]
    assert edge["requestCount"] == 3
    assert edge["recent"] is True
    assert edge["lastRequestedAt"] == "2026-01-02T11:00:00+00:00"
    assert len(edge["directions"]) == 2
    assert all("references" in item for item in edge["directions"])
    assert not any(
        reference["kind"] == "conversation" for item in edge["directions"] for reference in item["references"]
    )
    assert snapshot["dataWarnings"][0]["code"] == "COLLABORATION_UNMAPPED"
    assert snapshot["dataWarnings"][0]["count"] == 2

    owned = detail["items"][0]
    conversation_refs = [
        reference["id"]
        for item in owned["directions"]
        for reference in item["references"]
        if reference["kind"] == "conversation"
    ]
    assert set(conversation_refs) == {"conv-1"}


@pytest.mark.asyncio
async def test_snapshot_stays_readable_when_platform_source_degrades(facts, monkeypatch):
    sessions, chat = facts
    _insert_employee(chat, "e1", "星一", "agent-1")

    async def broken(_session):
        raise SQLAlchemyError("platform unavailable")

    monkeypatch.setattr(nebula, "_load_workers", broken)
    now = datetime(2026, 1, 2, 0, 30, tzinfo=UTC)
    async with sessions() as session:
        snapshot = await nebula.collect_snapshot(session, chat, now=now)

    assert snapshot["employees"][0]["id"] == "e1"
    assert snapshot["workers"] == []
    assert "workers" in snapshot["degradedSources"]
    assert "tasks" in snapshot["degradedSources"]
    assert snapshot["totals"]["employees"] == 1


@pytest.mark.asyncio
async def test_office_missing_employee_and_degraded_tasks_do_not_report_empty_success(facts, monkeypatch):
    sessions, chat = facts
    _insert_employee(chat, "e1", "自动化", "agent-1")
    async with sessions() as session:
        with pytest.raises(ValueError, match="员工不存在"):
            await nebula.collect_employee_office(session, chat, "missing")

        async def unavailable(*args, **kwargs):
            return {"employees": [{"id": "e1"}], "degradedSources": ["tasks"]}

        monkeypatch.setattr(nebula, "_collect_core", unavailable)
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await nebula.collect_employee_office(session, chat, "e1")
        assert exc.value.status_code == 503
