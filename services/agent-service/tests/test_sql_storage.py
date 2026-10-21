"""AgentScope SQL 配置、启动失败和显式初始化契约。"""

import asyncio
import os
from datetime import datetime
from unittest.mock import patch

import pytest
from agentscope_sql import (
    check_schema,
    create_storage,
    database_url,
    initialize_database,
    migration_config,
)


def test_missing_sql_configuration_has_no_fallback(monkeypatch):
    monkeypatch.delenv("AGENTSCOPE_SQL_URL", raising=False)
    with pytest.raises(RuntimeError, match="AGENTSCOPE_SQL_URL"):
        database_url()


@pytest.mark.parametrize(
    "url", ["redis://localhost/1", "mysql://user:secret@localhost/db", "not-a-url"]
)
def test_invalid_configuration_never_echoes_password(monkeypatch, url):
    monkeypatch.setenv("AGENTSCOPE_SQL_URL", url)
    with pytest.raises(RuntimeError) as result:
        database_url()
    assert "secret" not in str(result.value)


def test_service_does_not_create_missing_schema(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "AGENTSCOPE_SQL_URL", f"sqlite+aiosqlite:///{tmp_path / 'service.db'}"
    )
    with pytest.raises(RuntimeError, match="未初始化"):
        asyncio.run(check_schema())


def test_official_storage_initialization_and_restart(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "AGENTSCOPE_SQL_URL", f"sqlite+aiosqlite:///{tmp_path / 'service.db'}"
    )

    async def scenario():
        from agentscope.app.storage import AgentData, AgentRecord, SessionConfig
        from agentscope.message import Msg

        await initialize_database()
        await initialize_database()
        agent = AgentRecord(user_id="nexus", data=AgentData(name="SQL验证"))
        async with create_storage() as storage:
            await storage.upsert_agent("nexus", agent)
            session = await storage.upsert_session(
                "nexus",
                agent.id,
                SessionConfig(workspace_id="sql-test", name="持久化验证"),
            )
            message = Msg(
                name="user",
                content=[{"type": "text", "text": "重启后保留"}],
                role="user",
            )
            await storage.upsert_message("nexus", session.id, message)
        async with create_storage() as restarted:
            assert (await restarted.get_agent("nexus", agent.id)).data.name == "SQL验证"
            saved = await restarted.get_session("nexus", agent.id, session.id)
            assert saved.id == session.id
            messages, _ = await restarted.list_messages("nexus", session.id)
            assert [item.id for item in messages] == [message.id]

    asyncio.run(scenario())


@pytest.mark.skipif(
    not os.environ.get("AGENTSCOPE_TEST_MYSQL_URL"),
    reason="需要独立 AgentScope MySQL 测试库",
)
def test_mysql_official_storage_round_trip(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_SQL_URL", os.environ["AGENTSCOPE_TEST_MYSQL_URL"])

    async def scenario():
        from agentscope.app.storage import AgentData, AgentRecord, SessionConfig
        from agentscope.message import Msg

        await initialize_database()
        agent = AgentRecord(user_id="mysql-test", data=AgentData(name="MySQL验证"))
        async with create_storage() as storage:
            await storage.upsert_agent("mysql-test", agent)
            session = await storage.upsert_session(
                "mysql-test", agent.id, SessionConfig(workspace_id="mysql-test")
            )
            message = Msg(
                name="user",
                content=[{"type": "text", "text": "MySQL重启恢复"}],
                role="user",
            )
            earlier = message.model_copy(update={"id": "z-first"})
            later = message.model_copy(update={"id": "a-second"})
            # 同秒写入、逆字典 ID，真实 MySQL 不得丢微秒后按 ID 误排。
            with patch(
                "agentscope.app.storage._sql._storage._utcnow",
                side_effect=[
                    datetime(2026, 10, 21, 0, 0, 0, 100000),
                    datetime(2026, 10, 21, 0, 0, 0, 200000),
                ],
            ):
                await storage.upsert_message("mysql-test", session.id, earlier)
                await storage.upsert_message("mysql-test", session.id, later)
        async with create_storage() as restarted:
            assert (await restarted.get_agent("mysql-test", agent.id)).id == agent.id
            saved = await restarted.get_session("mysql-test", agent.id, session.id)
            assert saved.id == session.id
            messages, _ = await restarted.list_messages("mysql-test", session.id)
            assert [item.id for item in messages] == [earlier.id, later.id]
            latest, has_more = await restarted.list_messages(
                "mysql-test", session.id, limit=1
            )
            assert [item.id for item in latest] == [later.id] and has_more
            older, has_more = await restarted.list_messages(
                "mysql-test", session.id, before=later.id
            )
            assert [item.id for item in older] == [earlier.id] and not has_more

    asyncio.run(scenario())


def test_encoded_credentials_survive_official_migration_configuration(monkeypatch):
    url = "mysql+asyncmy://user:demo%40pass%2Fword%25@localhost/db"
    monkeypatch.setenv("AGENTSCOPE_SQL_URL", url)
    assert migration_config().get_main_option("sqlalchemy.url") == url


def test_unavailable_database_fails_without_echoing_credentials(monkeypatch):
    monkeypatch.setenv(
        "AGENTSCOPE_SQL_URL",
        "mysql+asyncmy://user:secret%40password@127.0.0.1:1/unavailable",
    )
    with pytest.raises(RuntimeError, match="无法检查") as error:
        asyncio.run(check_schema())
    assert "secret" not in str(error.value)
    assert "password" not in str(error.value)
