"""旧聊天库合并不能覆盖目标库里已经存在的数据。

``migrate_legacy_databases.py`` 的约定是“目标库已存在时保留现有数据”，
但合并曾用 ``INSERT OR REPLACE`` 整行替换同主键行：目标库运行一段时间后
再合并一次，员工与会话会被旧库静默覆盖，旧库缺少的新列回到默认值。
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "services" / "nexus-api" / "scripts" / "migrate_legacy_databases.py"

# v1.4.5 的聊天表结构：无 t_nexus_ 前缀，employees 还没有 mcp_json 列。
LEGACY_SCHEMA = """
CREATE TABLE employees (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    domain TEXT NOT NULL,
    description TEXT NOT NULL,
    instructions TEXT NOT NULL,
    avatar TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    knowledge_json TEXT NOT NULL,
    skills_json TEXT NOT NULL,
    tools_json TEXT NOT NULL,
    agentscope_agent_id TEXT,
    mailbox_session_id TEXT,
    capability_json TEXT NOT NULL DEFAULT '{}',
    model_route_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    employee_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'private',
    name TEXT NOT NULL,
    agentscope_session_id TEXT,
    workspace_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT,
    channel_id TEXT,
    sender TEXT NOT NULL,
    text TEXT NOT NULL,
    attachments_json TEXT NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 0,
    speaker_uid TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def _load_script_module():
    spec = importlib.util.spec_from_file_location("migrate_legacy_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script_module()


def _insert_employee(connection: sqlite3.Connection, table: str, **values) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})', tuple(values.values())
    )


def _insert_conversation(
    connection: sqlite3.Connection,
    table: str,
    conversation_id: str,
    name: str,
    session: str,
    workspace: str,
) -> None:
    connection.execute(
        f'INSERT INTO "{table}" '
        "(id, employee_id, owner_id, kind, name, agentscope_session_id, workspace_id, "
        "created_at, updated_at) VALUES (?, ?, ?, 'private', ?, ?, ?, ?, ?)",
        (
            conversation_id,
            "emp-1" if conversation_id == "conv-1" else "emp-2",
            "owner-1",
            name,
            session,
            workspace,
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )


def _employee_row(prefix: str, **overrides) -> dict[str, object]:
    row = {
        "id": "emp-1",
        "name": f"{prefix}-name",
        "domain": f"{prefix}-domain",
        "description": f"{prefix}-description",
        "instructions": f"{prefix}-instructions",
        "avatar": f"{prefix}-avatar",
        "enabled": 1,
        "knowledge_json": "[]",
        "skills_json": "[]",
        "tools_json": "[]",
        "agentscope_agent_id": f"{prefix}-agent",
        "mailbox_session_id": f"{prefix}-inbox",
        "capability_json": "{}",
        "model_route_id": f"{prefix}-route",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def test_merging_an_old_chat_database_keeps_existing_target_rows(tmp_path, capsys):
    target = tmp_path / "nexus.db"
    legacy = tmp_path / "legacy.sqlite3"

    script.init_db(target)
    with sqlite3.connect(target) as connection:
        _insert_employee(
            connection,
            "t_nexus_employees",
            **_employee_row("current", mcp_json='["mcp_current"]'),
        )
        _insert_conversation(
            connection,
            "t_nexus_conversations",
            "conv-1",
            "current-chat",
            "session_current",
            "workspace_current",
        )
        connection.commit()

    with sqlite3.connect(legacy) as connection:
        connection.executescript(LEGACY_SCHEMA)
        _insert_employee(connection, "employees", **_employee_row("old"))
        _insert_conversation(
            connection, "conversations", "conv-1", "old-chat", "session_old", "workspace_old"
        )
        # 只存在于旧库的行仍应被补齐。
        _insert_employee(connection, "employees", **_employee_row("extra", id="emp-2"))
        _insert_conversation(
            connection, "conversations", "conv-2", "extra-chat", "session_extra", "workspace_extra"
        )
        connection.commit()

    script.copy_chat_tables(legacy, target)

    with sqlite3.connect(target) as connection:
        employee = connection.execute(
            "SELECT name, agentscope_agent_id, mailbox_session_id, model_route_id, mcp_json "
            "FROM t_nexus_employees WHERE id = 'emp-1'"
        ).fetchone()
        conversation = connection.execute(
            "SELECT name, agentscope_session_id, workspace_id FROM t_nexus_conversations WHERE id = 'conv-1'"
        ).fetchone()
        extra = connection.execute(
            "SELECT name, agentscope_agent_id FROM t_nexus_employees WHERE id = 'emp-2'"
        ).fetchone()
        extra_conversation = connection.execute(
            "SELECT name, agentscope_session_id FROM t_nexus_conversations WHERE id = 'conv-2'"
        ).fetchone()

    assert employee == (
        "current-name",
        "current-agent",
        "current-inbox",
        "current-route",
        '["mcp_current"]',
    )
    assert conversation == ("current-chat", "session_current", "workspace_current")
    assert extra == ("extra-name", "extra-agent")
    assert extra_conversation == ("extra-chat", "session_extra")

    output = capsys.readouterr().out
    assert "保留目标已有" in output
