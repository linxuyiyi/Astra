"""Backfill durable conversations and canonical interactive statuses."""

from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "0008_semantic_workbench"
down_revision = "0007_task_workbench"
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


def _as_datetime(value, fallback):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    return fallback


def _session_status(task_status: str) -> str:
    return {
        "WAIT_HUMAN": "WAITING_INPUT",
        "SUSPENDED": "PAUSED",
        "COMPLETED": "COMPLETED",
        "FAILED": "FAILED",
        "CANCELLED": "CANCELLED",
    }.get(task_status, "ACTIVE")


def upgrade() -> None:
    bind = op.get_bind()
    task_sessions = sa.table(
        "task_sessions",
        sa.column("id", sa.String),
        sa.column("task_id", sa.String),
        sa.column("status", sa.String),
        sa.column("current_run_id", sa.String),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )
    session_messages = sa.table(
        "session_messages",
        sa.column("id", sa.String),
        sa.column("session_id", sa.String),
        sa.column("run_id", sa.String),
        sa.column("role", sa.String),
        sa.column("content", sa.Text),
        sa.column("status", sa.String),
        sa.column("created_at", sa.DateTime),
    )
    run_inquiries = sa.table(
        "run_inquiries",
        sa.column("id", sa.String),
        sa.column("session_id", sa.String),
        sa.column("run_id", sa.String),
        sa.column("agent_name", sa.String),
        sa.column("question", sa.Text),
        sa.column("description", sa.Text),
        sa.column("options", sa.JSON),
        sa.column("answer", sa.Text),
        sa.column("status", sa.String),
        sa.column("created_at", sa.DateTime),
        sa.column("answered_at", sa.DateTime),
    )

    bind.execute(run_inquiries.update().where(run_inquiries.c.status == "PENDING").values(status="OPEN"))
    bind.execute(
        sa.text(
            "UPDATE runs SET status = 'WAITING_INPUT' "
            "WHERE status = 'BLOCKED' AND task_id IN "
            "(SELECT id FROM tasks WHERE status = 'WAIT_HUMAN')"
        )
    )

    tasks = bind.execute(
        sa.text(
            "SELECT id, title, description, status, employee_id, human_intervention, "
            "created_at, updated_at FROM tasks ORDER BY created_at"
        )
    ).mappings()
    for task in tasks:
        created_at = _as_datetime(task["created_at"], datetime.now())
        updated_at = _as_datetime(task["updated_at"], created_at)
        latest_run = (
            bind.execute(
                sa.text("SELECT id FROM runs WHERE task_id = :task_id ORDER BY run_number DESC LIMIT 1"),
                {"task_id": task["id"]},
            )
            .mappings()
            .first()
        )
        work_session = (
            bind.execute(
                sa.text("SELECT id, current_run_id FROM task_sessions WHERE task_id = :task_id"),
                {"task_id": task["id"]},
            )
            .mappings()
            .first()
        )
        if work_session:
            session_id = work_session["id"]
            bind.execute(
                task_sessions.update()
                .where(task_sessions.c.id == session_id)
                .values(
                    status=_session_status(task["status"]),
                    current_run_id=work_session["current_run_id"]
                    or (latest_run["id"] if latest_run else None),
                )
            )
        else:
            session_id = f"SES-{uuid4().hex[:12].upper()}"
            bind.execute(
                task_sessions.insert().values(
                    id=session_id,
                    task_id=task["id"],
                    status=_session_status(task["status"]),
                    current_run_id=latest_run["id"] if latest_run else None,
                    created_at=created_at,
                    updated_at=updated_at,
                )
            )

        messages = (
            bind.execute(
                sa.text("SELECT content FROM session_messages WHERE session_id = :session_id"),
                {"session_id": session_id},
            )
            .scalars()
            .all()
        )
        initial_content = str(task["description"] or task["title"] or "").strip()
        if initial_content and not messages:
            bind.execute(
                session_messages.insert().values(
                    id=f"MSG-{uuid4().hex[:12].upper()}",
                    session_id=session_id,
                    run_id=None,
                    role="user",
                    content=initial_content,
                    status="APPLIED",
                    created_at=created_at,
                )
            )
            messages = [initial_content]

        progress_events = bind.execute(
            sa.text(
                "SELECT run_id, message, created_at FROM events "
                "WHERE task_id = :task_id AND event_type = 'WORK_PROGRESS' ORDER BY id"
            ),
            {"task_id": task["id"]},
        ).mappings()
        for progress_event in progress_events:
            content = str(progress_event["message"] or "").strip()
            is_runtime_noise = any(
                marker in content.lower() for marker in ("模型用量", "token usage", "usage updated")
            )
            if not content or is_runtime_noise or content in messages:
                continue
            bind.execute(
                session_messages.insert().values(
                    id=f"MSG-{uuid4().hex[:12].upper()}",
                    session_id=session_id,
                    run_id=progress_event["run_id"],
                    role="agent",
                    content=content,
                    status="APPLIED",
                    created_at=_as_datetime(progress_event["created_at"], updated_at),
                )
            )
            messages.append(content)

        intervention = _json_object(task["human_intervention"])
        question = str(intervention.get("requested_input") or "").strip()
        if not question:
            continue
        wait_event = (
            bind.execute(
                sa.text(
                    "SELECT run_id, created_at FROM events WHERE task_id = :task_id "
                    "AND event_type = 'WAIT_HUMAN' ORDER BY id LIMIT 1"
                ),
                {"task_id": task["id"]},
            )
            .mappings()
            .first()
        )
        answer = str(intervention.get("response") or "").strip() or None
        existing_inquiry = (
            bind.execute(
                sa.text("SELECT id FROM run_inquiries WHERE session_id = :session_id LIMIT 1"),
                {"session_id": session_id},
            )
            .mappings()
            .first()
        )
        inquiry_created_at = _as_datetime(
            wait_event["created_at"] if wait_event else task["updated_at"],
            updated_at,
        )
        if not existing_inquiry:
            bind.execute(
                run_inquiries.insert().values(
                    id=f"INQ-{uuid4().hex[:12].upper()}",
                    session_id=session_id,
                    run_id=wait_event["run_id"] if wait_event else None,
                    agent_name=task["employee_id"],
                    question=question,
                    description=str(intervention.get("blocking_reason") or ""),
                    options=intervention.get("options")
                    if isinstance(intervention.get("options"), list)
                    else [],
                    answer=answer,
                    status="ANSWERED"
                    if answer
                    else "OPEN"
                    if task["status"] == "WAIT_HUMAN"
                    else "CANCELLED",
                    created_at=inquiry_created_at,
                    answered_at=_as_datetime(intervention.get("responded_at"), updated_at)
                    if answer
                    else None,
                )
            )
        if answer and answer not in messages:
            bind.execute(
                session_messages.insert().values(
                    id=f"MSG-{uuid4().hex[:12].upper()}",
                    session_id=session_id,
                    run_id=wait_event["run_id"] if wait_event else None,
                    role="user",
                    content=answer,
                    status="ANSWERED",
                    created_at=_as_datetime(intervention.get("responded_at"), updated_at),
                )
            )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE run_inquiries SET status = 'PENDING' WHERE status = 'OPEN'"))
    bind.execute(sa.text("UPDATE runs SET status = 'BLOCKED' WHERE status = 'WAITING_INPUT'"))
