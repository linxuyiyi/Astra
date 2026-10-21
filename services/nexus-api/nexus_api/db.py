"""员工目录与会话绑定的 SQLAlchemy 接入，不保存 Agent 记忆。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.sql_storage import connect, init_schema  # noqa: F401

from . import db_models  # noqa: F401 - register chat tables in shared metadata
from .sql import insert_record, upsert_record  # noqa: F401


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


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


def employee_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
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
        "mcps": _loads(row["mcp_json"]),
        "tools": _loads(row["tools_json"]),
        "agentscope_agent_id": row["agentscope_agent_id"],
        "mailbox_session_id": row["mailbox_session_id"],
        "capability": _object(row["capability_json"]),
        "model_route_id": row["model_route_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def message_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "sender": row["sender"],
        "speaker": row["speaker_uid"],
        "text": row["text"],
        "attachments": _loads(row["attachments_json"]),
        "accepted": bool(row["accepted"]),
        "created_at": row["created_at"],
    }
