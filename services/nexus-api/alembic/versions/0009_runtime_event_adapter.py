"""Backfill structured OpenCode Task Tool calls as Subagent events."""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision = "0009_runtime_event_adapter"
down_revision = "0008_semantic_workbench"
branch_labels = None
depends_on = None


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _event_type(status: str) -> tuple[str, str]:
    normalized = status.lower()
    if any(value in normalized for value in ("fail", "error", "reject", "cancel")):
        return "SUBAGENT_FAILED", "执行失败"
    if any(value in normalized for value in ("complete", "success", "done", "finished")):
        return "SUBAGENT_COMPLETED", "已完成"
    if any(value in normalized for value in ("progress", "running", "working")):
        return "SUBAGENT_PROGRESS", "正在执行"
    return "SUBAGENT_STARTED", "已启动"


def upgrade() -> None:
    bind = op.get_bind()
    events = sa.table(
        "events",
        sa.column("id", sa.Integer),
        sa.column("task_id", sa.String),
        sa.column("employee_id", sa.String),
        sa.column("run_id", sa.String),
        sa.column("event_type", sa.String),
        sa.column("phase", sa.String),
        sa.column("message", sa.Text),
        sa.column("detail", sa.JSON),
        sa.column("created_at", sa.DateTime),
    )
    existing_source_ids: set[int] = set()
    for row in bind.execute(
        sa.select(events.c.detail).where(events.c.event_type.like("SUBAGENT_%"))
    ).mappings():
        source_id = _json_object(row["detail"]).get("source_event_id")
        if isinstance(source_id, int):
            existing_source_ids.add(source_id)

    rows = bind.execute(
        sa.select(events).where(events.c.event_type == "TOOL_ACTIVITY").order_by(events.c.id)
    ).mappings()
    for row in rows:
        if row["id"] in existing_source_ids:
            continue
        detail = _json_object(row["detail"])
        tool_input = detail.get("input") if isinstance(detail.get("input"), dict) else {}
        if str(detail.get("tool") or "").lower() != "task":
            continue
        agent_name = str(
            tool_input.get("subagent_type") or tool_input.get("subagentType") or tool_input.get("agent") or ""
        ).strip()
        if not agent_name:
            continue
        status = str(detail.get("status") or "started")
        event_type, verb = _event_type(status)
        metadata = detail.get("metadata") if isinstance(detail.get("metadata"), dict) else {}
        title = str(detail.get("title") or tool_input.get("description") or "子任务")
        bind.execute(
            events.insert().values(
                task_id=row["task_id"],
                employee_id=row["employee_id"],
                run_id=row["run_id"],
                event_type=event_type,
                phase="agent",
                message=f"{agent_name} {verb}子任务：{title}",
                detail={
                    "adapter_backfill": revision,
                    "source_event_type": "TOOL_ACTIVITY",
                    "source_event_id": row["id"],
                    "source_tool": "task",
                    "source_call_id": detail.get("call_id"),
                    "status": status,
                    "agent_name": agent_name,
                    "role": agent_name,
                    "description": tool_input.get("description"),
                    "prompt": tool_input.get("prompt"),
                    "input": tool_input,
                    "output": detail.get("output"),
                    "error": detail.get("error"),
                    "parent_session_id": metadata.get("parentSessionId") or metadata.get("parent_session_id"),
                    "child_session_id": metadata.get("sessionId") or metadata.get("session_id"),
                    "model": metadata.get("model"),
                },
                created_at=row["created_at"],
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    events = sa.table(
        "events",
        sa.column("id", sa.Integer),
        sa.column("detail", sa.JSON),
    )
    ids = [
        row["id"]
        for row in bind.execute(sa.select(events.c.id, events.c.detail)).mappings()
        if _json_object(row["detail"]).get("adapter_backfill") == revision
    ]
    if ids:
        bind.execute(events.delete().where(events.c.id.in_(ids)))
