"""员工目录与会话绑定。不保存 Agent 记忆。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS t_nexus_employees (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    domain TEXT NOT NULL,
    description TEXT NOT NULL,
    instructions TEXT NOT NULL,
    avatar TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    knowledge_json TEXT NOT NULL,
    skills_json TEXT NOT NULL,
    mcp_json TEXT NOT NULL DEFAULT '[]',
    tools_json TEXT NOT NULL,
    agentscope_agent_id TEXT,
    mailbox_session_id TEXT,
    capability_json TEXT NOT NULL DEFAULT '{}',
    model_route_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS t_nexus_conversations (
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
CREATE TABLE IF NOT EXISTS t_nexus_messages (
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
CREATE TABLE IF NOT EXISTS t_nexus_channels (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    record_agent_id TEXT NOT NULL DEFAULT '',
    record_session_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS t_nexus_channel_members (
    channel_id TEXT NOT NULL,
    employee_id TEXT NOT NULL,
    agentscope_session_id TEXT,
    last_delivered_message_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (channel_id, employee_id)
);
CREATE TABLE IF NOT EXISTS t_nexus_channel_humans (
    channel_id TEXT NOT NULL,
    user_uid TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (channel_id, user_uid)
);
CREATE TABLE IF NOT EXISTS t_nexus_attachments (
    id TEXT PRIMARY KEY,
    owner_uid TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS t_nexus_im_bots (
    employee_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    bot_id TEXT NOT NULL,
    UNIQUE (provider, bot_id)
);
CREATE TABLE IF NOT EXISTS t_nexus_im_events (
    provider TEXT NOT NULL,
    event_id TEXT NOT NULL,
    employee_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    accepted INTEGER NOT NULL DEFAULT 0,
    claim_token TEXT NOT NULL DEFAULT '',
    claim_until TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (provider, event_id)
);
CREATE TABLE IF NOT EXISTS t_nexus_im_bindings (
    employee_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    external_key TEXT NOT NULL DEFAULT '',
    owner_uid TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL,
    PRIMARY KEY (employee_id, kind, external_key, owner_uid)
);
CREATE TABLE IF NOT EXISTS t_nexus_im_threads (
    employee_id TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    PRIMARY KEY (employee_id, conversation_key)
);
CREATE TABLE IF NOT EXISTS t_nexus_im_outbox (
    delivery_id TEXT PRIMARY KEY,
    employee_id TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    body TEXT NOT NULL,
    state TEXT NOT NULL,
    lease_until TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS t_nexus_peer_deliveries (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    sender_agent_id TEXT NOT NULL,
    target_agent_id TEXT NOT NULL,
    message_key TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS t_nexus_skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    revision INTEGER NOT NULL DEFAULT 1,
    runtime_name TEXT NOT NULL,
    files_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_nexus_skills_name ON t_nexus_skills(name);
CREATE TABLE IF NOT EXISTS t_nexus_skill_versions (
    id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    files_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE (skill_id, version)
);
CREATE TABLE IF NOT EXISTS t_nexus_mcps (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    transport TEXT NOT NULL DEFAULT 'http',
    headers_json TEXT NOT NULL DEFAULT '[]',
    auth_type TEXT NOT NULL DEFAULT 'none',
    auth_header TEXT NOT NULL DEFAULT 'Authorization',
    token_ciphertext TEXT NOT NULL DEFAULT '',
    timeout REAL NOT NULL DEFAULT 30,
    runtime_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_nexus_mcps_name ON t_nexus_mcps(name);
CREATE UNIQUE INDEX IF NOT EXISTS ux_nexus_mcps_runtime_name ON t_nexus_mcps(runtime_name);
CREATE TABLE IF NOT EXISTS t_nexus_resource_sync (
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    employee_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    detail TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (resource_type, resource_id, employee_id, session_id)
);
CREATE TABLE IF NOT EXISTS t_nexus_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


LEGACY_TABLES = {
    "employees": "t_nexus_employees",
    "conversations": "t_nexus_conversations",
    "messages": "t_nexus_messages",
    "channels": "t_nexus_channels",
    "channel_members": "t_nexus_channel_members",
    "channel_humans": "t_nexus_channel_humans",
    "attachments": "t_nexus_attachments",
    "im_bots": "t_nexus_im_bots",
    "im_events": "t_nexus_im_events",
    "im_bindings": "t_nexus_im_bindings",
    "im_threads": "t_nexus_im_threads",
    "im_outbox": "t_nexus_im_outbox",
    "peer_deliveries": "t_nexus_peer_deliveries",
    "nexus_skills": "t_nexus_skills",
    "nexus_skill_versions": "t_nexus_skill_versions",
    "nexus_mcps": "t_nexus_mcps",
    "resource_sync": "t_nexus_resource_sync",
    "nexus_settings": "t_nexus_settings",
}


def _rename_legacy_tables(connection) -> None:
    """One-time rename from the pre-unification table names.

    Remove after every deployment has opened the unified ``nexus.db`` at least
    once; fresh databases only ever see the ``t_nexus_`` names.
    """
    existing = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for old, new in LEGACY_TABLES.items():
        if old in existing and new not in existing:
            connection.execute(f'ALTER TABLE "{old}" RENAME TO "{new}"')


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as connection:
        _rename_legacy_tables(connection)
        connection.executescript(SCHEMA)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(t_nexus_messages)")}
        if "accepted" not in columns:
            connection.execute("ALTER TABLE t_nexus_messages ADD COLUMN accepted INTEGER NOT NULL DEFAULT 0")
        if "speaker_uid" not in columns:
            connection.execute("ALTER TABLE t_nexus_messages ADD COLUMN speaker_uid TEXT NOT NULL DEFAULT ''")
        conversation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(t_nexus_conversations)")
        }
        if "kind" not in conversation_columns:
            connection.execute(
                "ALTER TABLE t_nexus_conversations "
                "ADD COLUMN kind TEXT NOT NULL DEFAULT 'private'"
            )
        if "workspace_id" not in conversation_columns:
            connection.execute(
                "ALTER TABLE t_nexus_conversations "
                "ADD COLUMN workspace_id TEXT NOT NULL DEFAULT ''"
            )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_private_conversation
            ON t_nexus_conversations(owner_id, employee_id, kind)
            """
        )
        attachment_columns = {row[1] for row in connection.execute("PRAGMA table_info(t_nexus_attachments)")}
        if "owner_uid" not in attachment_columns:
            connection.execute(
                "ALTER TABLE t_nexus_attachments ADD COLUMN owner_uid TEXT NOT NULL DEFAULT ''"
            )
        channel_columns = {row[1] for row in connection.execute("PRAGMA table_info(t_nexus_channels)")}
        if "record_session_id" not in channel_columns:
            connection.execute(
                "ALTER TABLE t_nexus_channels ADD COLUMN record_agent_id TEXT NOT NULL DEFAULT ''"
            )
            connection.execute(
                "ALTER TABLE t_nexus_channels ADD COLUMN record_session_id TEXT NOT NULL DEFAULT ''"
            )
        employee_columns = {row[1] for row in connection.execute("PRAGMA table_info(t_nexus_employees)")}
        if "capability_json" not in employee_columns:
            connection.execute(
                "ALTER TABLE t_nexus_employees ADD COLUMN capability_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "model_route_id" not in employee_columns:
            connection.execute(
                "ALTER TABLE t_nexus_employees ADD COLUMN model_route_id TEXT NOT NULL DEFAULT ''"
            )
        if "mcp_json" not in employee_columns:
            connection.execute("ALTER TABLE t_nexus_employees ADD COLUMN mcp_json TEXT NOT NULL DEFAULT '[]'")
        member_columns = {row[1] for row in connection.execute("PRAGMA table_info(t_nexus_channel_members)")}
        if "last_delivered_message_id" not in member_columns:
            connection.execute(
                "ALTER TABLE t_nexus_channel_members "
                "ADD COLUMN last_delivered_message_id TEXT NOT NULL DEFAULT ''"
            )
        event_columns = {row[1] for row in connection.execute("PRAGMA table_info(t_nexus_im_events)")}
        if "claim_token" not in event_columns:
            connection.execute(
                "ALTER TABLE t_nexus_im_events ADD COLUMN claim_token TEXT NOT NULL DEFAULT ''"
            )
        if "claim_until" not in event_columns:
            connection.execute(
                "ALTER TABLE t_nexus_im_events ADD COLUMN claim_until TEXT NOT NULL DEFAULT ''"
            )
        outbox_columns = {row[1] for row in connection.execute("PRAGMA table_info(t_nexus_im_outbox)")}
        if "lease_until" not in outbox_columns:
            connection.execute(
                "ALTER TABLE t_nexus_im_outbox ADD COLUMN lease_until TEXT NOT NULL DEFAULT ''"
            )


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def _object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    data = json.loads(value)
    return data if isinstance(data, dict) else {}


def _loads(value: str) -> list[Any]:
    data = json.loads(value)
    return data if isinstance(data, list) else []


def employee_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "domain": row["domain"],
        "description": row["description"],
        "instructions": row["instructions"],
        "avatar": row["avatar"],
        "enabled": bool(row["enabled"]),
        "knowledge": _loads(row["knowledge_json"]),
        "skills": _loads(row["skills_json"]),
        "mcps": _loads(row["mcp_json"]) if "mcp_json" in row.keys() else [],
        "tools": _loads(row["tools_json"]),
        "agentscope_agent_id": row["agentscope_agent_id"],
        "mailbox_session_id": row["mailbox_session_id"],
        "capability": _object(row["capability_json"]) if "capability_json" in row.keys() else {},
        "model_route_id": row["model_route_id"] if "model_route_id" in row.keys() else "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def message_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "sender": row["sender"],
        "speaker": row["speaker_uid"] if "speaker_uid" in row.keys() else "",
        "text": row["text"],
        "attachments": _loads(row["attachments_json"]),
        "accepted": bool(row["accepted"]),
        "created_at": row["created_at"],
    }
