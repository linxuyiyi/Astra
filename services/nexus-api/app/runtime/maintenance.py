"""Persistent Agent Worker maintenance operations, leases, and execution.

HTTP only submits and observes. The process lifespan owns a database-driven
executor; a disconnect never cancels a running operation. The operations table
is the single fact source for the maintenance lock.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from anyio import CancelScope
from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from app.config.settings import get_settings
from app.database import SessionLocal
from app.domain import EnvironmentStatus, ExecutionMode, MaintenanceAction, MaintenanceState
from app.models import (
    Event,
    OpenCodeExecutionEnvironment,
    WorkerInstance,
    WorkerMaintenanceEvent,
    WorkerMaintenanceOperation,
)
from app.runtime.capability import binding_names, dispatch_allowed
from app.runtime.environment import environment_snapshot
from app.runtime.occupancy import blocking_session_exists, occupying_exists
from app.worker_agent import __version__ as WORKER_AGENT_VERSION

logger = logging.getLogger(__name__)

EXECUTING_STATES = (
    MaintenanceState.PREFLIGHT.value,
    MaintenanceState.RUNNING.value,
    MaintenanceState.VERIFYING.value,
)
ACTIVE_STATES = (
    MaintenanceState.QUEUED.value,
    *EXECUTING_STATES,
    MaintenanceState.INTERRUPTED.value,
)
TERMINAL_STATES = (
    MaintenanceState.SUCCEEDED.value,
    MaintenanceState.FAILED.value,
    MaintenanceState.CANCELED.value,
)
RELEASING_STATES = (
    MaintenanceState.SUCCEEDED.value,
    MaintenanceState.FAILED.value,
    MaintenanceState.CANCELED.value,
)
DEFAULT_REQUESTED_BY = "api"
PUBLIC_ACTIONS = frozenset(item.value for item in MaintenanceAction)


def controller_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:10]}"


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def operation_payload(operation: WorkerMaintenanceOperation) -> dict[str, Any]:
    return {
        "id": operation.id,
        "environment_id": operation.environment_id,
        "action": operation.action,
        "state": operation.state,
        "stage": operation.stage,
        "attempt": operation.attempt,
        "requested_by": operation.requested_by,
        "requested_at": _iso(operation.requested_at),
        "started_at": _iso(operation.started_at),
        "finished_at": _iso(operation.finished_at),
        "lease_owner": operation.lease_owner,
        "lease_until": _iso(operation.lease_until),
        "cancel_requested": operation.cancel_requested,
        "danger_dispatched": operation.danger_dispatched,
        "reconcile_required": operation.reconcile_required,
        "reconcile_only": operation.reconcile_only,
        "execution_channel": operation.execution_channel,
        "execution_reason": operation.execution_reason,
        "retry_of_attempt": operation.retry_of_attempt,
        "target_revision": operation.target_revision or {},
        "before_snapshot": operation.before_snapshot or {},
        "after_snapshot": operation.after_snapshot or {},
        "error_code": operation.error_code,
        "error_message": operation.error_message,
    }


def event_payload(event: WorkerMaintenanceEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "operation_id": event.operation_id,
        "attempt": event.attempt,
        "type": event.type,
        "stage": event.stage,
        "name": event.name,
        "state": event.state,
        "message": event.message,
        "detail": event.detail or {},
        "created_at": _iso(event.created_at),
    }


def active_maintenance_exists(environment_id: int):
    return (
        select(WorkerMaintenanceOperation.id)
        .where(
            WorkerMaintenanceOperation.environment_id == environment_id,
            WorkerMaintenanceOperation.state.in_(ACTIVE_STATES),
        )
        .exists()
    )


async def active_operation(session, environment_id: int) -> WorkerMaintenanceOperation | None:
    return await session.scalar(
        select(WorkerMaintenanceOperation)
        .where(
            WorkerMaintenanceOperation.environment_id == environment_id,
            WorkerMaintenanceOperation.state.in_(ACTIVE_STATES),
        )
        .order_by(
            WorkerMaintenanceOperation.updated_at.desc(),
            WorkerMaintenanceOperation.id.desc(),
        )
        .limit(1)
    )


async def latest_operation(session, environment_id: int) -> WorkerMaintenanceOperation | None:
    return await session.scalar(
        select(WorkerMaintenanceOperation)
        .where(WorkerMaintenanceOperation.environment_id == environment_id)
        .order_by(
            WorkerMaintenanceOperation.updated_at.desc(),
            WorkerMaintenanceOperation.id.desc(),
        )
        .limit(1)
    )


async def require_no_active_maintenance(session, environment_id: int | None) -> None:
    if environment_id is None:
        return
    active = await session.scalar(
        select(WorkerMaintenanceOperation.id)
        .where(
            WorkerMaintenanceOperation.environment_id == environment_id,
            WorkerMaintenanceOperation.state.in_(ACTIVE_STATES),
        )
        .with_for_update()
        .limit(1)
    )
    if active is not None:
        raise HTTPException(409, "远程环境正在维护，请等待完成或先取消当前操作")


async def lock_environment_row(session, environment: OpenCodeExecutionEnvironment) -> None:
    """Take the shared environment write lock without consulting maintenance."""

    await session.refresh(environment)
    result = await session.execute(
        update(OpenCodeExecutionEnvironment)
        .where(
            OpenCodeExecutionEnvironment.id == environment.id,
            OpenCodeExecutionEnvironment.status_detail == environment.status_detail,
            OpenCodeExecutionEnvironment.status == environment.status,
        )
        .values(updated_at=OpenCodeExecutionEnvironment.updated_at)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.refresh(environment)
        raise HTTPException(409, "环境状态已变化，请刷新后重试")


def _validate_action(environment: OpenCodeExecutionEnvironment, action: str) -> None:
    settings = get_settings()
    if action not in {item.value for item in MaintenanceAction}:
        raise HTTPException(422, "不支持的维护动作")
    remote_actions = {
        MaintenanceAction.DEPLOY.value,
        MaintenanceAction.UPDATE.value,
        MaintenanceAction.RESTART.value,
    }
    if action in remote_actions and environment.mode != ExecutionMode.LINUX_REMOTE:
        raise HTTPException(409, "仅 Linux 远程员工支持部署、更新和重启")
    if action in remote_actions and not environment.workspace_root.startswith("/"):
        raise HTTPException(409, "工程目录必须是 Linux 绝对路径")
    if action in {MaintenanceAction.DEPLOY.value, MaintenanceAction.UPDATE.value}:
        if not environment.host or not environment.username or not environment.ssh_password:
            raise HTTPException(409, "请先保存远程 IP、用户名和 SSH 密码")
        if settings.mcp_transport != "streamable-http":
            raise HTTPException(
                409,
                "请先配置 NEXUS_MCP_TRANSPORT=streamable-http 并启动数字图书馆 HTTP 服务",
            )
    if action == MaintenanceAction.UPDATE.value and not environment.repository_id:
        raise HTTPException(409, "更新需要先绑定工程仓库")


def _target_revision(environment: OpenCodeExecutionEnvironment) -> dict[str, Any]:
    settings = get_settings()
    detail = environment.status_detail if isinstance(environment.status_detail, dict) else {}
    capability = detail.get("capability") if isinstance(detail.get("capability"), dict) else {}
    return {
        "opencode_version": settings.remote_opencode_version,
        "worker_agent_version": WORKER_AGENT_VERSION,
        "model_name": environment.model_name,
        "opencode_path": environment.opencode_path,
        "repository_id": environment.repository_id,
        "branch": environment.branch,
        "repository_commit": None,
        "skills": list(environment.skill_refs or []),
        "mcp": [item.get("name") for item in (environment.mcp_refs or []) if isinstance(item, dict)],
        "capability_revision": capability.get("desired_revision") or "",
    }


async def _occupancy_blockers(session, employee_id: str) -> list[str]:
    blockers: list[str] = []
    if await session.scalar(select(1).where(occupying_exists(employee_id))):
        blockers.append("occupied_task")
    if await session.scalar(select(1).where(blocking_session_exists(employee_id))):
        blockers.append("active_session")
    return blockers


def _execution_route(action: str) -> tuple[str | None, str | None]:
    if action in {MaintenanceAction.DISABLE.value, MaintenanceAction.RESTORE.value}:
        return "db", "仅修改调度开关或执行只读健康核对"
    if action in {MaintenanceAction.DEPLOY.value, MaintenanceAction.UPDATE.value}:
        return "ssh", "安装系统依赖与 per-environment 运行时需要 SSH 管理通道"
    return None, None


async def submit_operation(
    session,
    environment_id: int,
    action: str,
    *,
    requested_by: str = DEFAULT_REQUESTED_BY,
) -> WorkerMaintenanceOperation:
    """Atomically reserve the environment and persist a queued operation."""

    environment = await session.scalar(
        select(OpenCodeExecutionEnvironment)
        .where(OpenCodeExecutionEnvironment.id == environment_id)
        .with_for_update()
    )
    if environment is None:
        raise HTTPException(404, "Worker 环境不存在")
    _validate_action(environment, action)
    await lock_environment_row(session, environment)
    await require_no_active_maintenance(session, environment.id)
    if action not in {MaintenanceAction.CHECK.value, MaintenanceAction.DISABLE.value}:
        blockers = await _occupancy_blockers(session, environment.employee_id)
        if blockers:
            await session.rollback()
            raise HTTPException(
                409,
                "员工存在占用或未结束 Session，不能部署或重启"
                if len(blockers) > 1
                else "员工正在执行任务或等待人工确认，不能部署或重启",
            )
    now = datetime.now(UTC)
    snapshot = environment_snapshot(environment)
    channel, reason = _execution_route(action)
    operation = WorkerMaintenanceOperation(
        id="op_" + uuid4().hex,
        environment_id=environment.id,
        action=action,
        state=MaintenanceState.QUEUED.value,
        stage="queued",
        requested_by=requested_by or DEFAULT_REQUESTED_BY,
        attempt=1,
        execution_channel=channel,
        execution_reason=reason,
        target_revision=_target_revision(environment),
        before_snapshot=snapshot,
        after_snapshot={},
        active_environment_id=environment.id,
        requested_at=now,
    )
    session.add(operation)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "该环境已有维护操作，请等待完成或先取消") from exc
    except OperationalError as exc:
        await session.rollback()
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise HTTPException(409, "环境正在被其它事务更新，请稍后重试") from exc
        raise
    await session.refresh(operation)
    return operation


async def claim_next(owner: str) -> str | None:
    """Cancel queued cancellations, then claim one queued operation."""

    settings = get_settings()
    now = datetime.now(UTC)
    lease_until = now + timedelta(seconds=settings.worker_maintenance_lease_seconds)
    async with SessionLocal() as session:
        queued_cancels = (
            await session.scalars(
                select(WorkerMaintenanceOperation).where(
                    WorkerMaintenanceOperation.state == MaintenanceState.QUEUED.value,
                    WorkerMaintenanceOperation.cancel_requested.is_(True),
                )
            )
        ).all()
        for operation in queued_cancels:
            if operation.reconcile_required or operation.reconcile_only:
                await session.execute(
                    update(WorkerMaintenanceOperation)
                    .where(
                        WorkerMaintenanceOperation.id == operation.id,
                        WorkerMaintenanceOperation.state == MaintenanceState.QUEUED.value,
                        WorkerMaintenanceOperation.attempt == operation.attempt,
                    )
                    .values(
                        state=MaintenanceState.INTERRUPTED.value,
                        stage="interrupted",
                        active_environment_id=WorkerMaintenanceOperation.environment_id,
                        finished_at=now,
                        updated_at=now,
                        error_code="cancel_requires_reconcile",
                        error_message="远端可能仍存在晚到操作；请先只读核对再决定",
                    )
                    .execution_options(synchronize_session=False)
                )
                session.add(
                    WorkerMaintenanceEvent(
                        operation_id=operation.id,
                        attempt=operation.attempt,
                        type="interrupted",
                        stage="interrupted",
                        name="取消待核对",
                        state=MaintenanceState.INTERRUPTED.value,
                        message="远端可能仍存在晚到操作；请先只读核对再决定",
                        detail={"reason": "cancel_requires_reconcile"},
                    )
                )
            else:
                await session.execute(
                    update(WorkerMaintenanceOperation)
                    .where(
                        WorkerMaintenanceOperation.id == operation.id,
                        WorkerMaintenanceOperation.state == MaintenanceState.QUEUED.value,
                        WorkerMaintenanceOperation.attempt == operation.attempt,
                    )
                    .values(
                        state=MaintenanceState.CANCELED.value,
                        stage="canceled",
                        active_environment_id=None,
                        finished_at=now,
                        updated_at=now,
                        error_code="canceled",
                        error_message="操作在执行前被取消",
                    )
                    .execution_options(synchronize_session=False)
                )
        await session.commit()

        candidate = await session.scalar(
            select(WorkerMaintenanceOperation)
            .where(
                WorkerMaintenanceOperation.state == MaintenanceState.QUEUED.value,
                WorkerMaintenanceOperation.cancel_requested.is_(False),
                or_(
                    WorkerMaintenanceOperation.lease_until.is_(None),
                    WorkerMaintenanceOperation.lease_until <= now,
                ),
            )
            .order_by(WorkerMaintenanceOperation.requested_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if candidate is None:
            await session.rollback()
            return None
        result = await session.execute(
            update(WorkerMaintenanceOperation)
            .where(
                WorkerMaintenanceOperation.id == candidate.id,
                WorkerMaintenanceOperation.state == MaintenanceState.QUEUED.value,
                WorkerMaintenanceOperation.attempt == candidate.attempt,
                WorkerMaintenanceOperation.cancel_requested.is_(False),
                or_(
                    WorkerMaintenanceOperation.lease_until.is_(None),
                    WorkerMaintenanceOperation.lease_until <= now,
                ),
            )
            .values(
                state=MaintenanceState.PREFLIGHT.value,
                stage="preflight",
                lease_owner=owner,
                lease_until=lease_until,
                started_at=func.coalesce(WorkerMaintenanceOperation.started_at, now),
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await session.rollback()
            return None
        await session.commit()
        return candidate.id


async def recover_expired(owner: str | None = None) -> int:
    """Mark only genuinely expired executing leases INTERRUPTED."""

    now = datetime.now(UTC)
    async with SessionLocal() as session:
        expired = (
            await session.scalars(
                select(WorkerMaintenanceOperation).where(
                    WorkerMaintenanceOperation.state.in_(EXECUTING_STATES),
                    WorkerMaintenanceOperation.lease_until.is_not(None),
                    WorkerMaintenanceOperation.lease_until <= now,
                    or_(owner is None, WorkerMaintenanceOperation.lease_owner != owner),
                )
            )
        ).all()
        for operation in expired:
            result = await session.execute(
                update(WorkerMaintenanceOperation)
                .where(
                    WorkerMaintenanceOperation.id == operation.id,
                    WorkerMaintenanceOperation.attempt == operation.attempt,
                    WorkerMaintenanceOperation.state.in_(EXECUTING_STATES),
                    WorkerMaintenanceOperation.lease_until.is_not(None),
                    WorkerMaintenanceOperation.lease_until <= now,
                )
                .values(
                    state=MaintenanceState.INTERRUPTED.value,
                    stage="interrupted",
                    lease_owner=None,
                    lease_until=None,
                    finished_at=now,
                    error_code="lease_expired",
                    error_message="执行进程失联或租约过期；请先核对远端状态再重试",
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount:
                session.add(
                    WorkerMaintenanceEvent(
                        operation_id=operation.id,
                        attempt=operation.attempt,
                        type="interrupted",
                        stage="interrupted",
                        name="执行中断",
                        state=MaintenanceState.INTERRUPTED.value,
                        message="执行进程失联或租约过期；请先核对远端状态再重试",
                        detail={"previous_stage": operation.stage, "previous_state": operation.state},
                    )
                )
        await session.commit()
        return len(expired)


async def _finish_in_session(
    session,
    operation_id: str,
    owner: str,
    attempt: int,
    state: str,
    *,
    stage: str | None,
    error_code: str | None,
    error_message: str | None,
    after_snapshot: dict[str, Any] | None,
    release_active: bool | None,
    environment_id: int | None,
    environment_values: dict[str, Any] | None,
    employee_id: str | None = None,
    employee_values: dict[str, Any] | None = None,
    event_message: str | None = None,
    secrets: tuple[str, ...] | list[str] = (),
) -> bool:
    from app.runtime.redaction import redact_data, redact_text

    if state not in {*TERMINAL_STATES, MaintenanceState.INTERRUPTED.value}:
        raise ValueError("invalid terminal maintenance state")
    now = datetime.now(UTC)
    if release_active is None:
        release_active = state != MaintenanceState.INTERRUPTED.value
    result = await session.execute(
        update(WorkerMaintenanceOperation)
        .where(
            WorkerMaintenanceOperation.id == operation_id,
            WorkerMaintenanceOperation.attempt == attempt,
            WorkerMaintenanceOperation.lease_owner == owner,
            WorkerMaintenanceOperation.state.in_(EXECUTING_STATES),
            WorkerMaintenanceOperation.lease_until.is_not(None),
            WorkerMaintenanceOperation.lease_until > now,
        )
        .values(
            state=state,
            stage=stage or ("finished" if state == MaintenanceState.SUCCEEDED.value else "failed"),
            lease_owner=None,
            lease_until=None,
            finished_at=now,
            error_code=error_code,
            error_message=redact_text(error_message, secrets) if error_message else None,
            after_snapshot=redact_data(after_snapshot, secrets) if after_snapshot is not None else {},
            reconcile_required=False,
            reconcile_only=False,
            active_environment_id=None if release_active else WorkerMaintenanceOperation.environment_id,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False
    if environment_id is not None and environment_values is not None:
        await session.execute(
            update(OpenCodeExecutionEnvironment)
            .where(OpenCodeExecutionEnvironment.id == environment_id)
            .values(**redact_data(environment_values, secrets))
        )
    if employee_id is not None and employee_values is not None:
        from app.models import AgentWorker

        await session.execute(
            update(AgentWorker)
            .where(AgentWorker.id == employee_id)
            .values(**redact_data(employee_values, secrets))
        )
    if event_message:
        employee_id = None
        if environment_id is not None:
            environment = await session.get(OpenCodeExecutionEnvironment, environment_id)
            if environment is not None:
                employee_id = environment.employee_id
        session.add(
            Event(
                employee_id=employee_id,
                event_type="ENVIRONMENT_MAINTAINED",
                phase="environment",
                message=redact_text(event_message, secrets),
                detail={
                    "operation_id": operation_id,
                    "attempt": attempt,
                    "state": state,
                },
            )
        )
    if state in TERMINAL_STATES:
        checks = (after_snapshot or {}).get("checks", []) if isinstance(after_snapshot, dict) else []
        session.add(
            WorkerMaintenanceEvent(
                operation_id=operation_id,
                attempt=attempt,
                type="result",
                stage=stage,
                name="result",
                state=state,
                message=redact_text(
                    error_message or ("维护完成" if state == MaintenanceState.SUCCEEDED.value else state),
                    secrets,
                ),
                detail={
                    "success": state == MaintenanceState.SUCCEEDED.value,
                    "state": state,
                    "checks": checks,
                },
            )
        )
    return True


async def finish_operation(
    operation_id: str,
    owner: str,
    attempt: int,
    state: str,
    *,
    stage: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    after_snapshot: dict[str, Any] | None = None,
    release_active: bool | None = None,
    environment_id: int | None = None,
    environment_values: dict[str, Any] | None = None,
    employee_id: str | None = None,
    employee_values: dict[str, Any] | None = None,
    event_message: str | None = None,
    secrets: tuple[str, ...] | list[str] = (),
) -> bool:
    async with SessionLocal() as session:
        applied = await _finish_in_session(
            session,
            operation_id,
            owner,
            attempt,
            state,
            stage=stage,
            error_code=error_code,
            error_message=error_message,
            after_snapshot=after_snapshot,
            release_active=release_active,
            environment_id=environment_id,
            environment_values=environment_values,
            employee_id=employee_id,
            employee_values=employee_values,
            event_message=event_message,
            secrets=secrets,
        )
        await session.commit()
        return applied


async def interrupt_owned(owner: str, reason: str) -> int:
    """Persist owned in-flight work as INTERRUPTED when the process stops."""

    now = datetime.now(UTC)
    with CancelScope(shield=True):
        async with SessionLocal() as session:
            result = await session.execute(
                update(WorkerMaintenanceOperation)
                .where(
                    WorkerMaintenanceOperation.lease_owner == owner,
                    WorkerMaintenanceOperation.state.in_(EXECUTING_STATES),
                )
                .values(
                    state=MaintenanceState.INTERRUPTED.value,
                    stage="interrupted",
                    lease_owner=None,
                    lease_until=None,
                    finished_at=now,
                    error_code="controller_stopped",
                    error_message=reason,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            return result.rowcount


async def append_event(
    session,
    operation_id: str,
    attempt: int,
    *,
    event_type: str = "step",
    stage: str | None = None,
    name: str | None = None,
    state: str | None = None,
    message: str = "",
    detail: dict[str, Any] | None = None,
    secrets: tuple[str, ...] | list[str] = (),
) -> WorkerMaintenanceEvent:
    from app.runtime.redaction import redact_data, redact_text

    event = WorkerMaintenanceEvent(
        operation_id=operation_id,
        attempt=attempt,
        type=event_type,
        stage=stage,
        name=redact_text(name, secrets) if name else name,
        state=state,
        message=redact_text(message, secrets),
        detail=redact_data(detail or {}, secrets),
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event


async def events_after(
    session, operation_id: str, after_id: int, *, limit: int = 200
) -> list[WorkerMaintenanceEvent]:
    return list(
        (
            await session.scalars(
                select(WorkerMaintenanceEvent)
                .where(
                    WorkerMaintenanceEvent.operation_id == operation_id,
                    WorkerMaintenanceEvent.id > after_id,
                )
                .order_by(WorkerMaintenanceEvent.id)
                .limit(limit)
            )
        ).all()
    )


async def operation_detail(session, operation_id: str) -> dict[str, Any] | None:
    operation = await session.get(WorkerMaintenanceOperation, operation_id)
    if operation is None:
        return None
    events = list(
        (
            await session.scalars(
                select(WorkerMaintenanceEvent)
                .where(WorkerMaintenanceEvent.operation_id == operation_id)
                .order_by(WorkerMaintenanceEvent.id.desc())
                .limit(100)
            )
        ).all()
    )
    payload = operation_payload(operation)
    payload["events"] = [event_payload(item) for item in reversed(events)]
    return payload


async def list_operations(
    session, environment_id: int, *, limit: int = 50, before_id: str | None = None
) -> list[dict[str, Any]]:
    query = select(WorkerMaintenanceOperation).where(
        WorkerMaintenanceOperation.environment_id == environment_id
    )
    if before_id:
        cursor = await session.get(WorkerMaintenanceOperation, before_id)
        if cursor is not None:
            query = query.where(
                or_(
                    WorkerMaintenanceOperation.updated_at < cursor.updated_at,
                    and_(
                        WorkerMaintenanceOperation.updated_at == cursor.updated_at,
                        WorkerMaintenanceOperation.id < cursor.id,
                    ),
                )
            )
    rows = await session.scalars(
        query.order_by(
            WorkerMaintenanceOperation.updated_at.desc(),
            WorkerMaintenanceOperation.id.desc(),
        ).limit(limit)
    )
    return [operation_payload(item) for item in rows]


async def cancel_operation(
    session, operation_id: str, *, actor: str | None = None
) -> WorkerMaintenanceOperation:
    operation = await session.get(WorkerMaintenanceOperation, operation_id)
    if operation is None:
        raise HTTPException(404, "维护操作不存在")
    if operation.state in TERMINAL_STATES:
        return operation
    if operation.state == MaintenanceState.INTERRUPTED.value:
        if operation.danger_dispatched or operation.reconcile_required:
            raise HTTPException(
                409,
                "远端可能仍存在晚到操作，请先调用 reconcile 做只读核对，不能直接取消释放",
            )
        return await _cancel_queued(session, operation, release=True, actor=actor)
    if operation.state == MaintenanceState.QUEUED.value:
        if operation.reconcile_required or operation.reconcile_only:
            return await _cancel_queued(session, operation, release=False, actor=actor)
        return await _cancel_queued(session, operation, release=True, actor=actor)
    return await _request_cancel(session, operation, actor=actor)


async def _cancel_queued(
    session,
    operation: WorkerMaintenanceOperation,
    *,
    release: bool,
    actor: str | None = None,
) -> WorkerMaintenanceOperation:
    now = datetime.now(UTC)
    state = MaintenanceState.CANCELED.value if release else MaintenanceState.INTERRUPTED.value
    error_code = "canceled" if release else "cancel_requires_reconcile"
    message = "操作已由用户取消" if release else "远端可能仍存在晚到操作；请先只读核对再决定"
    result = await session.execute(
        update(WorkerMaintenanceOperation)
        .where(
            WorkerMaintenanceOperation.id == operation.id,
            WorkerMaintenanceOperation.state == operation.state,
            WorkerMaintenanceOperation.attempt == operation.attempt,
        )
        .values(
            state=state,
            stage="canceled" if release else "interrupted",
            cancel_requested=True,
            active_environment_id=None if release else WorkerMaintenanceOperation.environment_id,
            finished_at=now,
            error_code=error_code,
            error_message=message,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.rollback()
        raise HTTPException(409, "操作状态已变化，请刷新后重试")
    session.add(
        WorkerMaintenanceEvent(
            operation_id=operation.id,
            attempt=operation.attempt,
            type="canceled" if release else "interrupted",
            stage="canceled" if release else "interrupted",
            name="已取消" if release else "取消待核对",
            state=state,
            message=message,
            detail={"reason": error_code, **({"requested_by": actor} if actor else {})},
        )
    )
    await session.commit()
    await session.refresh(operation)
    return operation


async def _request_cancel(
    session, operation: WorkerMaintenanceOperation, *, actor: str | None = None
) -> WorkerMaintenanceOperation:
    now = datetime.now(UTC)
    result = await session.execute(
        update(WorkerMaintenanceOperation)
        .where(
            WorkerMaintenanceOperation.id == operation.id,
            WorkerMaintenanceOperation.state == operation.state,
            WorkerMaintenanceOperation.attempt == operation.attempt,
        )
        .values(cancel_requested=True, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.rollback()
        raise HTTPException(409, "操作状态已变化，请刷新后重试")
    session.add(
        WorkerMaintenanceEvent(
            operation_id=operation.id,
            attempt=operation.attempt,
            type="cancel_requested",
            stage=operation.stage,
            name="请求取消",
            state=operation.state,
            message="将在下一个安全检查点取消",
            detail={"requested_by": actor} if actor else {},
        )
    )
    await session.commit()
    await session.refresh(operation)
    return operation


async def retry_operation(
    session,
    operation_id: str,
    *,
    reconcile_only: bool = False,
    actor: str | None = None,
) -> WorkerMaintenanceOperation:
    operation = await session.get(WorkerMaintenanceOperation, operation_id)
    if operation is None:
        raise HTTPException(404, "维护操作不存在")
    if operation.state not in {
        MaintenanceState.FAILED.value,
        MaintenanceState.INTERRUPTED.value,
        MaintenanceState.CANCELED.value,
    }:
        raise HTTPException(409, "只有失败、中断或已取消的操作可以重试")
    if operation.action not in PUBLIC_ACTIONS and not reconcile_only:
        raise HTTPException(409, "内部通道切换操作不支持重试；请先只读核对后释放")
    if reconcile_only and operation.state != MaintenanceState.INTERRUPTED.value:
        raise HTTPException(409, "只有中断的操作可以只读核对")
    environment = await session.scalar(
        select(OpenCodeExecutionEnvironment)
        .where(OpenCodeExecutionEnvironment.id == operation.environment_id)
        .with_for_update()
    )
    if environment is None:
        raise HTTPException(404, "Worker 环境不存在")
    await lock_environment_row(session, environment)
    if await session.scalar(
        select(WorkerMaintenanceOperation.id)
        .where(
            WorkerMaintenanceOperation.environment_id == operation.environment_id,
            WorkerMaintenanceOperation.state.in_(ACTIVE_STATES),
            WorkerMaintenanceOperation.id != operation.id,
        )
        .with_for_update()
    ):
        await session.rollback()
        raise HTTPException(409, "该环境已有进行中的维护操作")
    if (
        operation.action not in {MaintenanceAction.CHECK.value, MaintenanceAction.DISABLE.value}
        and not reconcile_only
    ):
        blockers = await _occupancy_blockers(session, environment.employee_id)
        if blockers:
            await session.rollback()
            raise HTTPException(409, "员工存在占用任务或未结束 Session，不能重试维护")
    previous_attempt = operation.attempt
    previous_state = operation.state
    reconcile_required = (
        reconcile_only
        or previous_state == MaintenanceState.INTERRUPTED.value
        or operation.danger_dispatched
        or bool(operation.reconcile_required)
    )
    now = datetime.now(UTC)
    if reconcile_only:
        next_target = operation.target_revision or {}
        next_before = operation.before_snapshot or {}
    else:
        next_target = _target_revision(environment)
        next_before = environment_snapshot(environment)
    result = await session.execute(
        update(WorkerMaintenanceOperation)
        .where(
            WorkerMaintenanceOperation.id == operation.id,
            WorkerMaintenanceOperation.attempt == previous_attempt,
            WorkerMaintenanceOperation.state == previous_state,
        )
        .values(
            state=MaintenanceState.QUEUED.value,
            stage="reconcile" if reconcile_required else "queued",
            attempt=previous_attempt + 1,
            retry_of_attempt=previous_attempt,
            lease_owner=None,
            lease_until=None,
            cancel_requested=False,
            danger_dispatched=False,
            reconcile_required=reconcile_required,
            reconcile_only=reconcile_only,
            error_code=None,
            error_message=None,
            target_revision=next_target,
            before_snapshot=next_before,
            after_snapshot={},
            active_environment_id=operation.environment_id,
            started_at=None,
            finished_at=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.rollback()
        raise HTTPException(409, "操作状态已变化，请刷新后重试")
    session.add(
        WorkerMaintenanceEvent(
            operation_id=operation.id,
            attempt=previous_attempt,
            type="attempt",
            stage=operation.stage,
            name="尝试结果",
            state=previous_state,
            message=operation.error_message or previous_state,
            detail={
                "target_revision": operation.target_revision or {},
                "before_snapshot": operation.before_snapshot or {},
                "after_snapshot": operation.after_snapshot or {},
                "error_code": operation.error_code,
                "error_message": operation.error_message,
            },
        )
    )
    session.add(
        WorkerMaintenanceEvent(
            operation_id=operation.id,
            attempt=previous_attempt + 1,
            type="retry",
            stage="reconcile" if reconcile_required else "queued",
            name="只读核对" if reconcile_only else "重试",
            state=MaintenanceState.QUEUED.value,
            message=("只读核对远端是否存在晚到安装或重启" if reconcile_only else "重新预检远端事实后继续"),
            detail={
                "previous_state": previous_state,
                "reconcile_only": reconcile_only,
                **({"requested_by": actor} if actor else {}),
            },
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "该环境已有进行中的维护操作") from exc
    except OperationalError as exc:
        await session.rollback()
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise HTTPException(409, "环境正在被其它事务更新，请稍后重试") from exc
        raise
    await session.refresh(operation)
    return operation


async def reconcile_operation(
    session, operation_id: str, *, actor: str | None = None
) -> WorkerMaintenanceOperation:
    return await retry_operation(
        session, operation_id, reconcile_only=True, actor=actor
    )


class MaintenanceLeaseLost(RuntimeError):
    pass


class MaintenanceBlocked(RuntimeError):
    def __init__(self, message: str, code: str = "occupied") -> None:
        self.code = code
        super().__init__(message)


class MaintenanceCanceled(RuntimeError):
    pass


class OperationLease:
    """Fenced access to one operation attempt for the running executor.

    Ownership always includes a live ``lease_until``; an expired lease cannot
    renew, mark danger, write events, or finish. Recovery owns expiry handling.
    """

    def __init__(
        self,
        operation_id: str,
        attempt: int,
        owner: str,
        lease_seconds: int,
        secrets: tuple[str, ...] | list[str] = (),
    ):
        self.operation_id = operation_id
        self.attempt = attempt
        self.owner = owner
        self.lease_seconds = lease_seconds
        self.secrets = tuple(secrets)

    def _owned(self, now: datetime):
        return (
            WorkerMaintenanceOperation.id == self.operation_id,
            WorkerMaintenanceOperation.attempt == self.attempt,
            WorkerMaintenanceOperation.lease_owner == self.owner,
            WorkerMaintenanceOperation.state.in_(EXECUTING_STATES),
            WorkerMaintenanceOperation.lease_until.is_not(None),
            WorkerMaintenanceOperation.lease_until > now,
        )

    async def renew(self) -> bool:
        now = datetime.now(UTC)
        async with SessionLocal() as session:
            result = await session.execute(
                update(WorkerMaintenanceOperation)
                .where(*self._owned(now))
                .values(
                    lease_until=now + timedelta(seconds=self.lease_seconds),
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            return result.rowcount == 1

    async def snapshot(self) -> WorkerMaintenanceOperation | None:
        async with SessionLocal() as session:
            operation = await session.get(WorkerMaintenanceOperation, self.operation_id)
            if operation is None:
                return None
            session.expunge(operation)
            return operation

    async def set_stage(self, stage: str, *, danger: bool = False) -> None:
        now = datetime.now(UTC)
        async with SessionLocal() as session:
            values: dict[str, Any] = {"stage": stage, "updated_at": now}
            if danger:
                values["danger_dispatched"] = True
            result = await session.execute(
                update(WorkerMaintenanceOperation)
                .where(*self._owned(now))
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            if result.rowcount != 1:
                raise MaintenanceLeaseLost("维护租约已失效或已过期")

    async def set_execution_route(self, channel: str, reason: str) -> None:
        now = datetime.now(UTC)
        async with SessionLocal() as session:
            result = await session.execute(
                update(WorkerMaintenanceOperation)
                .where(*self._owned(now))
                .values(execution_channel=channel, execution_reason=reason, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            if result.rowcount != 1:
                raise MaintenanceLeaseLost("维护租约已失效或已过期")

    async def set_target_revision(self, values: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        async with SessionLocal() as session:
            result = await session.execute(
                update(WorkerMaintenanceOperation)
                .where(*self._owned(now))
                .values(target_revision=values, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            if result.rowcount != 1:
                raise MaintenanceLeaseLost("维护租约已失效或已过期")

    async def mark_danger(self) -> None:
        now = datetime.now(UTC)
        async with SessionLocal() as session:
            result = await session.execute(
                update(WorkerMaintenanceOperation)
                .where(*self._owned(now))
                .values(danger_dispatched=True, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            if result.rowcount != 1:
                raise MaintenanceLeaseLost("维护租约已失效或已过期")

    async def clear_reconcile_required(self) -> None:
        now = datetime.now(UTC)
        async with SessionLocal() as session:
            result = await session.execute(
                update(WorkerMaintenanceOperation)
                .where(*self._owned(now))
                .values(reconcile_required=False, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            if result.rowcount != 1:
                raise MaintenanceLeaseLost("维护租约已失效或已过期")

    async def event(
        self,
        *,
        event_type: str = "step",
        stage: str | None = None,
        name: str | None = None,
        state: str | None = None,
        message: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(UTC)
        async with SessionLocal() as session:
            operation = await session.get(WorkerMaintenanceOperation, self.operation_id)
            if operation is None or operation.attempt != self.attempt:
                raise MaintenanceLeaseLost("维护 attempt 已变化，拒绝写入陈旧事件")
            active = (
                operation.lease_owner == self.owner
                and operation.state in EXECUTING_STATES
                and operation.lease_until is not None
                and operation.lease_until > now
            )
            finished = operation.lease_owner is None and operation.state in {
                *TERMINAL_STATES,
                MaintenanceState.INTERRUPTED.value,
            }
            if not (active or finished):
                raise MaintenanceLeaseLost("维护租约已失效，拒绝写入陈旧事件")
            await append_event(
                session,
                self.operation_id,
                self.attempt,
                event_type=event_type,
                stage=stage,
                name=name,
                state=state,
                message=message,
                detail=detail,
                secrets=self.secrets,
            )

    async def emit_step(
        self,
        *,
        state: str,
        stage: str,
        danger: bool,
        event_type: str,
        event_state: str | None,
        name: str | None,
        message: str,
        detail: dict[str, Any],
    ) -> None:
        """Advance state/stage and append the step event in one fenced transaction."""

        from app.runtime.redaction import redact_data, redact_text

        now = datetime.now(UTC)
        async with SessionLocal() as session:
            operation = await session.get(WorkerMaintenanceOperation, self.operation_id)
            if operation is None or operation.attempt != self.attempt:
                raise MaintenanceLeaseLost("维护 attempt 已变化，拒绝写入陈旧阶段")
            if operation.state not in EXECUTING_STATES:
                raise MaintenanceLeaseLost("维护操作已结束，拒绝写入陈旧阶段")
            target_state = operation.state
            if _STATE_RANK.get(state, 0) > _STATE_RANK.get(operation.state, 0):
                target_state = state
            target_stage = stage
            if target_stage == "preflight" and operation.stage != "preflight":
                target_stage = operation.stage
            values: dict[str, Any] = {
                "state": target_state,
                "stage": target_stage,
                "updated_at": now,
            }
            if danger:
                values["danger_dispatched"] = True
            result = await session.execute(
                update(WorkerMaintenanceOperation)
                .where(*self._owned(now))
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await session.rollback()
                raise MaintenanceLeaseLost("维护租约已失效或已过期")
            session.add(
                WorkerMaintenanceEvent(
                    operation_id=self.operation_id,
                    attempt=self.attempt,
                    type=event_type,
                    stage=target_stage,
                    name=redact_text(name, self.secrets) if name else name,
                    state=event_state,
                    message=redact_text(message, self.secrets),
                    detail=redact_data(detail, self.secrets),
                )
            )
            await session.commit()

    async def check_canceled(self) -> None:
        now = datetime.now(UTC)
        operation = await self.snapshot()
        if operation is None or operation.attempt != self.attempt:
            raise MaintenanceLeaseLost("维护租约已失效")
        if operation.cancel_requested:
            raise MaintenanceCanceled("操作已请求取消")
        if (
            operation.lease_owner != self.owner
            or operation.state not in EXECUTING_STATES
            or operation.lease_until is None
            or operation.lease_until <= now
        ):
            raise MaintenanceLeaseLost("维护租约已失效或已过期")

    async def assert_safe_to_disrupt(
        self, environment_id: int, employee_id: str, *, allow_occupied: bool = False
    ) -> None:
        """Fenced DB recheck immediately before a dangerous remote step."""

        now = datetime.now(UTC)
        async with SessionLocal() as session:
            result = await session.execute(
                update(WorkerMaintenanceOperation)
                .where(*self._owned(now))
                .values(lease_until=now + timedelta(seconds=self.lease_seconds), updated_at=now)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await session.rollback()
                raise MaintenanceLeaseLost("维护租约已失效、已过期或已被接管")
            operation = await session.get(WorkerMaintenanceOperation, self.operation_id)
            if operation is not None and operation.cancel_requested:
                await session.rollback()
                raise MaintenanceCanceled("操作已请求取消")
            if not allow_occupied:
                if await session.scalar(select(1).where(occupying_exists(employee_id))):
                    await session.rollback()
                    raise MaintenanceBlocked("员工存在占用任务，已停止危险维护", "occupied_task")
                if await session.scalar(select(1).where(blocking_session_exists(employee_id))):
                    await session.rollback()
                    raise MaintenanceBlocked("员工存在未结束 Session，已停止危险维护", "active_session")
            await session.commit()

    async def finish(
        self,
        state: str,
        *,
        stage: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        after_snapshot: dict[str, Any] | None = None,
        environment_id: int | None = None,
        environment_values: dict[str, Any] | None = None,
        employee_id: str | None = None,
        employee_values: dict[str, Any] | None = None,
        event_message: str | None = None,
    ) -> bool:
        return await finish_operation(
            self.operation_id,
            self.owner,
            self.attempt,
            state,
            stage=stage,
            error_code=error_code,
            error_message=error_message,
            after_snapshot=after_snapshot,
            environment_id=environment_id,
            environment_values=environment_values,
            employee_id=employee_id,
            employee_values=employee_values,
            event_message=event_message,
            secrets=self.secrets,
        )


async def _remote_json_via_ssh(runtime, environment, url: str):
    import json as jsonlib
    import shlex

    session = await runtime.open_session(environment)
    script = f"import urllib.request\nprint(urllib.request.urlopen({url!r}, timeout=3).read().decode())\n"
    try:
        rc, out, err = await session.run_command(f"python3 -c {shlex.quote(script)}")
    finally:
        await session.close()
    if rc:
        raise RuntimeError(err.strip() or out.strip() or f"远程读取失败：{rc}")
    return jsonlib.loads(out)


async def _installer_locks_free(runtime, environment) -> bool | None:
    import hashlib
    import shlex

    session = await runtime.open_session(environment)
    try:
        rc, out, _err = await session.run_command(
            f"python3 -c {shlex.quote('import os,sys; print(os.path.realpath(sys.argv[1]))')} "
            f"{shlex.quote(environment.workspace_root)}"
        )
        if rc:
            return None
        workspace_real = out.strip().splitlines()[-1] if out.strip() else ""
        if not workspace_real.startswith("/"):
            return None
        workspace_hash = hashlib.sha256(workspace_real.encode()).hexdigest()[:24]
        script = (
            'cache="$HOME/.cache/nexus-provision"; mkdir -p "$cache"; '
            f'flock -n "$cache/environment-{environment.id}.lock" true && '
            f'flock -n "$cache/workspace-{workspace_hash}.lock" true'
        )
        rc, _out, _err = await session.run_command(script)
    finally:
        await session.close()
    if rc == 0:
        return True
    if rc in {1, 2}:
        return False
    return None


async def _run_reconcile(lease: OperationLease, operation: WorkerMaintenanceOperation) -> bool:
    """Read-only check for late installer/restart facts before retry or release.

    Returns True when a retry may continue with its normal action. Returns False
    when the operation was finished (released or kept INTERRUPTED).
    """

    from app.models import WorkerCommand
    from app.runtime.environment import ExecutionEnvironmentRuntime

    await lease.set_stage("reconcile")
    await lease.event(
        event_type="step",
        stage="reconcile",
        name="只读核对",
        state="running",
        message="核对远端 installer 锁、Worker 重启指令与真实健康",
    )
    async with SessionLocal() as session:
        environment = await session.get(OpenCodeExecutionEnvironment, operation.environment_id)
        command_attempt = operation.retry_of_attempt or operation.attempt
        command = await session.scalar(
            select(WorkerCommand).where(WorkerCommand.request_id == f"maint:{operation.id}:{command_attempt}")
        )
        command_status = (
            command.status
            if command is not None
            else ("none" if operation.action == "restart" and not operation.danger_dispatched else "none")
        )
        command_pending = bool(
            command is not None and command.status in {"pending", "accepted", "dispatched"}
        )
        late_control_commands = list(
            (
                await session.scalars(
                    select(WorkerCommand)
                    .where(
                        WorkerCommand.environment_id == operation.environment_id,
                        WorkerCommand.name.in_(("restart_opencode", "switch_channel")),
                        WorkerCommand.status.in_(("pending", "accepted", "dispatched")),
                    )
                    .order_by(WorkerCommand.id)
                )
            ).all()
        )
        if environment is not None:
            session.expunge(environment)
    if environment is None:
        await lease.finish(
            MaintenanceState.INTERRUPTED.value,
            stage="reconcile",
            error_code="environment_missing",
            error_message="Worker 环境不存在，无法核对",
        )
        return False

    installer_relevant = operation.action in {
        MaintenanceAction.DEPLOY.value,
        MaintenanceAction.UPDATE.value,
    }
    details: dict[str, Any] = {
        "installer_lock": "not_applicable" if not installer_relevant else "unknown",
        "worker_command": command_status,
        "worker_delivery": None,
        "worker_result": None,
        "late_worker_commands": [
            {
                "request_id": item.request_id,
                "name": item.name,
                "status": item.status,
            }
            for item in late_control_commands
        ],
        "health": "unknown",
        "native": "unknown",
        "reasons": [],
    }
    if command is not None:
        details["worker_delivery"] = command.delivery_state
        details["worker_result"] = command.result
    runtime = ExecutionEnvironmentRuntime(get_settings())

    if installer_relevant:
        if environment.host and environment.username and environment.ssh_password:
            try:
                free = await _installer_locks_free(runtime, environment)
                details["installer_lock"] = "free" if free is True else "busy" if free is False else "unknown"
                if free is not True:
                    details["reasons"].append("installer_lock_not_free")
            except Exception as exc:
                details["reasons"].append(f"installer_lock_unknown: {exc}")
        elif operation.danger_dispatched:
            details["reasons"].append("ssh_credentials_missing")

    if command_pending:
        details["reasons"].append("worker_restart_result_pending")

    health_status_value = EnvironmentStatus.UNKNOWN
    probe = None
    try:
        probe = await observe_environment(environment.id, persist=False)
    except Exception as exc:
        details["reasons"].append(f"full_probe_failed: {exc}")
    if probe is not None:
        health_status_value = probe["status"]
        observation = probe["observation"] if isinstance(probe["observation"], dict) else {}
        if probe["stale"] or observation.get("error"):
            health_status_value = EnvironmentStatus.UNKNOWN
            details["reasons"].append("probe_stale" if probe["stale"] else "probe_error")
        details["equipment"] = probe["equipment"]
        details["repository"] = probe["repository"]
        details["observed"] = probe["observation"]
        details["observed_at"] = probe["checked_at"].isoformat()

    native_status = "unknown"
    try:
        states = None
        client = None
        if environment.mode == ExecutionMode.LINUX_REMOTE:
            async with SessionLocal() as session:
                worker = await session.scalar(
                    select(WorkerInstance).where(WorkerInstance.environment_id == environment.id)
                )
            if worker is not None and worker.status == "online":
                from app.runtime.worker_client import WorkerOpenCodeClient

                client = WorkerOpenCodeClient(get_settings(), environment_id=environment.id)
        if client is not None:
            states = await client.request(
                "GET",
                "/session/status",
                directory=Path(environment.workspace_root),
            )
        elif environment.host and environment.username and environment.ssh_password:
            from urllib.parse import urlencode

            port = int(environment.opencode_server_port)
            query = urlencode({"directory": environment.workspace_root})
            states = await _remote_json_via_ssh(
                runtime,
                environment,
                f"http://127.0.0.1:{port}/session/status?{query}",
            )
        if isinstance(states, dict) and any(
            isinstance(item, dict) and item.get("type") in {"busy", "retry"}
            for item in states.values()
        ):
            native_status = "busy"
        from app.runtime.remote_provisioning import RemoteProvisioner

        RemoteProvisioner._assert_native_idle(states)
        native_status = "idle"
    except Exception as exc:
        if native_status != "busy":
            native_status = "unknown"
        details["reasons"].append(f"native_not_idle: {exc}")
    details["native"] = native_status
    if native_status == "busy":
        health_status_value = EnvironmentStatus.UNAVAILABLE
        details["reasons"].append("native_busy")
    elif native_status != "idle" and health_status_value == EnvironmentStatus.AVAILABLE:
        health_status_value = EnvironmentStatus.UNKNOWN
        details["reasons"].append("native_unknown")
    details["probe_status"] = health_status_value.value
    if health_status_value == EnvironmentStatus.AVAILABLE:
        details["health"] = "ok"
    elif health_status_value == EnvironmentStatus.UNAVAILABLE:
        details["health"] = "unhealthy"
        if probe is not None and probe["status"] == EnvironmentStatus.UNAVAILABLE:
            details["reasons"].append("equipment_verification_failed")
    else:
        details["health"] = "unknown"

    now = datetime.now(UTC)
    if probe is not None:
        environment_values = dict(probe["values"])
        environment_values["status"] = health_status_value
    else:
        previous_detail = (
            dict(environment.status_detail) if isinstance(environment.status_detail, dict) else {}
        )
        previous_detail["equipment"] = []
        previous_detail["observed_at"] = now.isoformat()
        capability = (
            dict(previous_detail.get("capability"))
            if isinstance(previous_detail.get("capability"), dict)
            else {}
        )
        capability.update({"runtime_status": "error", "checked_at": now.isoformat()})
        previous_detail["capability"] = capability
        environment_values = {
            "status": EnvironmentStatus.UNKNOWN,
            "last_checked_at": now,
            "status_detail": previous_detail,
        }

    safety_reasons = []
    if installer_relevant:
        if details["installer_lock"] != "free":
            safety_reasons.append("installer_lock_not_free")
    if command_pending:
        safety_reasons.append("worker_restart_result_pending")
    if late_control_commands:
        safety_reasons.append("worker_control_command_pending")
    safe = not safety_reasons
    details["safety_reasons"] = safety_reasons
    if not safe:
        details["reasons"] = details["reasons"] or safety_reasons
        reason = "; ".join(str(item) for item in details["reasons"])[-1000:]
        await lease.event(
            event_type="reconcile",
            stage="reconcile",
            name="核对未通过",
            state=MaintenanceState.INTERRUPTED.value,
            message=reason,
            detail=details,
        )
        await lease.finish(
            MaintenanceState.INTERRUPTED.value,
            stage="interrupted",
            error_code="reconcile_unknown",
            error_message=f"远端状态无法确认：{reason}",
            after_snapshot=details,
            environment_id=environment.id,
            environment_values=environment_values,
        )
        return False

    if operation.reconcile_only or operation.cancel_requested:
        state = (
            MaintenanceState.CANCELED.value if operation.cancel_requested else MaintenanceState.FAILED.value
        )
        message = (
            "远端无晚到危险操作，环境健康"
            if health_status_value == EnvironmentStatus.AVAILABLE
            else "远端无晚到危险操作，但运行时健康未通过；可显式重试部署或重启"
        )
        await lease.event(
            event_type="reconcile",
            stage="reconcile",
            name="核对完成",
            state=state,
            message=message,
            detail=details,
        )
        await lease.finish(
            state,
            stage="reconciled",
            error_code="reconciled_canceled" if state == MaintenanceState.CANCELED.value else "reconciled",
            error_message=message,
            after_snapshot=details,
            environment_id=environment.id,
            environment_values=environment_values,
            event_message="维护中断核对完成",
        )
        return False

    await lease.clear_reconcile_required()
    await lease.event(
        event_type="reconcile",
        stage="reconcile",
        name="核对完成",
        state="passed",
        message="远端无晚到危险操作，继续重试",
        detail=details,
    )
    return True


def _deployed_from_desired(desired: dict[str, Any]) -> dict[str, Any]:
    return {
        "opencode": {"version": (desired.get("opencode") or {}).get("version")},
        "worker_agent": {"version": (desired.get("worker_agent") or {}).get("version")},
        "model": {"name": (desired.get("model") or {}).get("name")},
        "mcp": {"revision": (desired.get("mcp") or {}).get("revision")},
        "skills": {"revision": (desired.get("skills") or {}).get("revision")},
        "repository": (
            dict(desired["repository"])
            if isinstance(desired.get("repository"), dict)
            else None
        ),
    }


async def observe_environment(
    environment_id: int,
    *,
    persist: bool = False,
    apply_desired: bool = False,
    mark_deployed: bool = False,
    deployed_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read-only probe followed by a short, revision-fenced persistence step.

    No DB transaction is held across network I/O. A fresh session re-reads the
    Worker epoch and environment revision before a conditional write. Callers
    that pass ``persist=False`` must apply ``values`` in their own fenced
    operation transaction (``lease.finish``); this function never writes then.
    """

    from app.runtime.worker_health import (
        desired_equipment,
        equipment_rows,
        health_status,
        probe_equipment,
        repository_payload,
    )

    settings = get_settings()
    async with SessionLocal() as session:
        environment = await session.get(OpenCodeExecutionEnvironment, environment_id)
        if environment is None:
            raise HTTPException(404, "Worker 环境不存在")
        worker = await session.scalar(
            select(WorkerInstance).where(WorkerInstance.environment_id == environment_id)
        )
        desired = await desired_equipment(session, environment, settings)
        previous = dict(environment.status_detail or {})
        previous_status = environment.status
        snapshot_epoch = worker.epoch if worker is not None else None
        snapshot_online = bool(worker is not None and worker.status == "online")
        session.expunge(environment)
        if worker is not None:
            session.expunge(worker)

    # Network probe outside any transaction.
    observation = await probe_equipment(
        environment,
        desired,
        settings,
        worker_instance=worker,
        observed_epoch=snapshot_epoch,
    )
    now = datetime.now(UTC)
    status = health_status(observation, desired)
    remote = environment.mode == ExecutionMode.LINUX_REMOTE

    # Fresh current read after the network call. The same entity identity must
    # never be treated as current just because it is still in an identity map.
    async with SessionLocal() as session:
        current_worker = await session.scalar(
            select(WorkerInstance).where(WorkerInstance.environment_id == environment_id).with_for_update()
        )
        current_epoch = current_worker.epoch if current_worker is not None else None
        current_online = bool(current_worker is not None and current_worker.status == "online")
    stale = remote and (
        not snapshot_online or not current_online or snapshot_epoch is None or current_epoch != snapshot_epoch
    )
    if stale:
        status = EnvironmentStatus.UNKNOWN
    elif remote and status == EnvironmentStatus.AVAILABLE and not current_online:
        status = EnvironmentStatus.UNKNOWN
        stale = True

    if mark_deployed and not isinstance(deployed_snapshot, dict):
        deployed_snapshot = _deployed_from_desired(desired)
    deployed = deployed_snapshot if isinstance(deployed_snapshot, dict) else previous.get("deployed")
    equipment = equipment_rows(desired, observation, deployed)
    repository = repository_payload(desired, observation)
    capability = dict(previous.get("capability")) if isinstance(previous.get("capability"), dict) else {}
    desired_revision = str(desired.get("revision") or "")
    previous_applied = str(capability.get("applied_revision") or "")
    applied = (
        desired_revision if apply_desired and status == EnvironmentStatus.AVAILABLE else previous_applied
    )
    pending = bool(desired_revision) and applied != desired_revision
    capability.update(
        {
            "desired_revision": desired_revision,
            "applied_revision": applied,
            "runtime_status": (
                "applied"
                if status == EnvironmentStatus.AVAILABLE and not pending
                else "pending"
                if status == EnvironmentStatus.AVAILABLE
                else "error"
            ),
            "error": "" if status == EnvironmentStatus.AVAILABLE else str(observation.get("error") or ""),
            "checked_at": now.isoformat(),
        }
    )
    detail = dict(previous)
    detail.update(
        {
            "observed": observation,
            "observed_at": now.isoformat(),
            "observed_epoch": current_epoch,
            "observed_online": current_online,
            "equipment": equipment,
            "repository": repository,
            "capability": capability,
        }
    )
    if isinstance(deployed_snapshot, dict):
        detail["deployed"] = deployed_snapshot
    values = {
        "status": status,
        "last_checked_at": now,
        "status_detail": detail,
    }
    result = {
        "stale": stale,
        "status": status,
        "desired": desired,
        "observation": observation,
        "equipment": equipment,
        "repository": repository,
        "checked_at": now,
        "values": values,
        "observed_epoch": current_epoch,
    }
    if not persist:
        return result

    if stale:
        return result
    async with SessionLocal() as session:
        current_environment = await session.scalar(
            select(OpenCodeExecutionEnvironment)
            .where(OpenCodeExecutionEnvironment.id == environment_id)
            .with_for_update()
        )
        if current_environment is None:
            return result
        active = await session.scalar(
            select(WorkerMaintenanceOperation.id)
            .where(
                WorkerMaintenanceOperation.environment_id == environment_id,
                WorkerMaintenanceOperation.state.in_(ACTIVE_STATES),
            )
            .with_for_update()
            .limit(1)
        )
        if active is not None:
            await session.rollback()
            result["stale"] = True
            return result
        current_worker = await session.scalar(
            select(WorkerInstance).where(WorkerInstance.environment_id == environment_id).with_for_update()
        )
        if remote and (
            current_worker is None
            or current_worker.status != "online"
            or current_worker.epoch != current_epoch
        ):
            await session.rollback()
            result["stale"] = True
            return result
        conditions = [
            OpenCodeExecutionEnvironment.id == environment_id,
            OpenCodeExecutionEnvironment.status == previous_status,
            OpenCodeExecutionEnvironment.status_detail == previous,
            ~active_maintenance_exists(environment_id),
        ]
        if remote and current_epoch is not None:
            conditions.append(
                select(WorkerInstance.id)
                .where(
                    WorkerInstance.environment_id == environment_id,
                    WorkerInstance.status == "online",
                    WorkerInstance.epoch == current_epoch,
                )
                .exists()
            )
        written = await session.execute(
            update(OpenCodeExecutionEnvironment)
            .where(*conditions)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if written.rowcount != 1:
            await session.rollback()
            result["stale"] = True
            return result
        await session.commit()
    return result


def health_is_fresh(environment, status_detail: dict[str, Any] | None, worker_epoch: int | None) -> bool:
    """Only a full observed projection with the current online Worker epoch is fresh."""

    if environment.mode != ExecutionMode.LINUX_REMOTE:
        return True
    detail = status_detail if isinstance(status_detail, dict) else {}
    raw = detail.get("observed_at")
    if not raw:
        return False
    try:
        observed = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    max_age = get_settings().worker_health_max_age_seconds
    if (datetime.now(UTC) - observed).total_seconds() > max_age:
        return False
    observed_epoch = detail.get("observed_epoch")
    if observed_epoch is None or worker_epoch is None or observed_epoch != worker_epoch:
        return False
    return bool(detail.get("observed_online", True))


async def health_observer() -> None:
    settings = get_settings()
    if settings.worker_health_interval_seconds <= 0:
        return
    await asyncio.sleep(settings.worker_health_interval_seconds)
    while True:
        try:
            async with SessionLocal() as session:
                ids = list(
                    (
                        await session.scalars(
                            select(OpenCodeExecutionEnvironment.id).where(
                                OpenCodeExecutionEnvironment.mode == ExecutionMode.LINUX_REMOTE,
                                ~active_maintenance_exists(OpenCodeExecutionEnvironment.id),
                            )
                        )
                    ).all()
                )
            for environment_id in ids:
                try:
                    await observe_environment(environment_id, persist=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("worker health probe failed for %s", environment_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("worker health observer cycle failed")
        await asyncio.sleep(settings.worker_health_interval_seconds)


async def _run_disable(lease: OperationLease, operation: WorkerMaintenanceOperation, owner: str) -> None:
    async with SessionLocal() as session:
        environment = await session.get(OpenCodeExecutionEnvironment, operation.environment_id)
        if environment is None:
            await lease.finish(
                MaintenanceState.FAILED.value,
                error_code="environment_missing",
                error_message="Worker 环境不存在",
            )
            return
        employee_id = environment.employee_id
    await lease.set_stage("disable")
    await finish_operation(
        operation.id,
        owner,
        operation.attempt,
        MaintenanceState.SUCCEEDED.value,
        stage="disabled",
        error_code=None,
        error_message=None,
        after_snapshot={"enabled": False},
        employee_id=employee_id,
        employee_values={"enabled": False},
        environment_id=operation.environment_id,
        environment_values={},
        event_message="Worker 已暂停新调度，现有任务继续执行",
        secrets=lease.secrets,
    )


async def _run_restore(lease: OperationLease, operation: WorkerMaintenanceOperation, owner: str) -> None:
    from app.models import AgentWorker

    await lease.set_stage("restore")
    probe = await observe_environment(operation.environment_id, persist=False)
    async with SessionLocal() as session:
        environment = await session.get(OpenCodeExecutionEnvironment, operation.environment_id)
        if environment is None:
            await lease.finish(
                MaintenanceState.FAILED.value,
                error_code="environment_missing",
                error_message="Worker 环境不存在",
            )
            return
        employee_id = environment.employee_id
        employee = await session.get(AgentWorker, employee_id)
    healthy = not probe["stale"] and probe["status"] == EnvironmentStatus.AVAILABLE
    if healthy:
        await finish_operation(
            operation.id,
            owner,
            operation.attempt,
            MaintenanceState.SUCCEEDED.value,
            stage="restored",
            after_snapshot={
                "enabled": True,
                "equipment": probe["equipment"],
                "repository": probe["repository"],
            },
            employee_id=employee_id,
            employee_values={"enabled": True},
            environment_id=operation.environment_id,
            environment_values=probe["values"],
            event_message="Worker 健康核验通过，已恢复调度",
        )
        return
    message = (
        "健康观测已过期，请等待下一轮健康检查"
        if probe["stale"]
        else "真实健康核验未通过；请先执行 restart 后重试恢复"
    )
    await lease.finish(
        MaintenanceState.FAILED.value,
        stage="restore_failed",
        error_code="health_not_available",
        error_message=message,
        after_snapshot={
            "enabled": bool(employee.enabled) if employee is not None else None,
            "equipment": probe["equipment"],
            "repository": probe["repository"],
        },
        environment_id=operation.environment_id,
        environment_values=probe["values"],
    )


async def _run_check(lease: OperationLease, operation: WorkerMaintenanceOperation) -> None:
    await lease.set_stage("checking")
    async with SessionLocal() as session:
        environment = await session.get(OpenCodeExecutionEnvironment, operation.environment_id)
        worker = await session.scalar(
            select(WorkerInstance).where(
                WorkerInstance.environment_id == operation.environment_id
            )
        )
    if worker is not None and worker.status == "online":
        await lease.set_execution_route("worker", "读取在线 Worker 的真实装备与原生状态")
    elif (
        environment is not None
        and environment.host
        and environment.username
        and environment.ssh_password
    ):
        await lease.set_execution_route("ssh", "Worker 不在线，使用 SSH 只读探测真实装备")
    else:
        await lease.set_execution_route("none", "Worker 不在线且未配置 SSH，无法探测")
    await lease.event(
        event_type="step",
        stage="checking",
        name="只读检查",
        state="running",
        message="正在真实探测 OpenCode、模型、MCP、Skill 与 Git",
    )
    probe = await observe_environment(operation.environment_id, persist=False)
    for row in probe["equipment"]:
        await lease.event(
            event_type="step",
            stage="checking",
            name=str(row.get("label") or row.get("key")),
            state="passed" if row.get("verified") else "failed",
            message=str(row.get("error") or ""),
            detail=row,
        )
    healthy = not probe["stale"] and probe["status"] == EnvironmentStatus.AVAILABLE
    await lease.finish(
        MaintenanceState.SUCCEEDED.value if healthy else MaintenanceState.FAILED.value,
        stage="checking",
        error_code=None if healthy else "health_check_failed",
        error_message=None
        if healthy
        else ("健康观测 epoch 已变化，结果未应用" if probe["stale"] else "环境装备核验未通过"),
        after_snapshot={
            "status": probe["status"].value if hasattr(probe["status"], "value") else probe["status"],
            "equipment": probe["equipment"],
            "repository": probe["repository"],
            "stale": probe["stale"],
        },
        environment_id=None if probe["stale"] else operation.environment_id,
        environment_values=None if probe["stale"] else probe["values"],
        event_message="环境检查完成" if healthy else "环境检查未通过",
    )


_STATE_RANK = {
    MaintenanceState.PREFLIGHT.value: 1,
    MaintenanceState.RUNNING.value: 2,
    MaintenanceState.VERIFYING.value: 3,
}


def _step_stage(name: str, current_stage: str, current_state: str) -> tuple[str, str, bool]:
    """Map a real provisioner/installer step to operation state, stage, danger."""

    preflight = {"SSH 连接与执行检查", "运行状态检查"}
    verify = {"模型中转连接", "图书馆 MCP"}
    stages = {
        "系统依赖": "system",
        "Node.js 与 npm": "runtime",
        "Git 工作树": "repository",
        "OpenCode 安装": "opencode",
        "写入前状态检查": "write_check",
        "工程目录": "repository",
        "全局 OpenCode 配置": "configure",
        "部署配置": "configure",
        "Python 环境": "system",
        "工程仓库": "repository",
        "模型配置": "configure",
        "安装 Worker Agent": "install",
        "切换到 Worker": "channel",
        "重启 OpenCode Serve": "restart",
    }
    dangerous = {
        "系统依赖",
        "Node.js 与 npm",
        "Git 工作树",
        "OpenCode 安装",
        "工程目录",
        "全局 OpenCode 配置",
        "部署配置",
        "Python 环境",
        "工程仓库",
        "模型配置",
        "安装 Worker Agent",
        "切换到 Worker",
        "重启 OpenCode Serve",
    }
    if name in preflight:
        state, stage = MaintenanceState.PREFLIGHT.value, "preflight"
    elif name in verify:
        state, stage = MaintenanceState.VERIFYING.value, "verify"
    else:
        state = MaintenanceState.RUNNING.value
        stage = stages.get(name, current_stage)
    if _STATE_RANK.get(state, 0) < _STATE_RANK.get(current_state, 0):
        state = current_state
    if stage == "preflight" and current_stage != "preflight":
        stage = current_stage
    return state, stage, name in dangerous


async def _emit_event(lease: OperationLease, event: dict[str, Any], current_stage: str) -> str:
    event_type = str(event.get("type") or "step")
    name = str(event.get("name") or "") or None
    event_state = str(event.get("state") or "") or None
    message = str(event.get("detail") or event.get("message") or "")
    detail = {
        key: value
        for key, value in event.items()
        if key not in {"type", "name", "state", "message", "detail"}
    }
    operation = await lease.snapshot()
    if operation is None or operation.attempt != lease.attempt:
        raise MaintenanceLeaseLost("维护 attempt 已变化，拒绝写入陈旧阶段")
    state, stage, dangerous = _step_stage(name or "", current_stage, operation.state)
    if event_type != "step" or not name:
        state, stage, dangerous = operation.state, current_stage, False
    await lease.emit_step(
        state=state,
        stage=stage,
        danger=dangerous and event_state == "running",
        event_type=event_type,
        event_state=event_state,
        name=name,
        message=message,
        detail=detail,
    )
    return stage


async def _execute_operation(operation_id: str, owner: str) -> None:
    from app.runtime.remote_provisioning import RemoteProvisioner

    settings = get_settings()
    async with SessionLocal() as session:
        operation = await session.get(WorkerMaintenanceOperation, operation_id)
        if operation is None:
            return
        if operation.lease_owner != owner or operation.state not in EXECUTING_STATES:
            return
        environment = await session.get(OpenCodeExecutionEnvironment, operation.environment_id)
        if environment is None:
            await finish_operation(
                operation_id,
                owner,
                operation.attempt,
                MaintenanceState.FAILED.value,
                error_code="environment_missing",
                error_message="Worker 环境不存在",
                secrets=(settings.mcp_api_key or "",),
            )
            return
        attempt = operation.attempt
        action = operation.action
        reconcile_required = operation.reconcile_required
        session.expunge(operation)
        session.expunge(environment)
    secrets = tuple(item for item in (environment.ssh_password, settings.mcp_api_key) if item)
    lease = OperationLease(
        operation_id,
        attempt,
        owner,
        settings.worker_maintenance_lease_seconds,
        secrets=secrets,
    )
    try:
        await lease.set_stage("preflight")
        await lease.event(
            event_type="started",
            stage="preflight",
            name="开始维护",
            state=MaintenanceState.PREFLIGHT.value,
            message=f"开始执行 {action}",
            detail={"attempt": attempt, "reconcile_required": reconcile_required},
        )
        if reconcile_required:
            proceed = await _run_reconcile(lease, operation)
            if not proceed:
                return
            current = await lease.snapshot()
            if current is None or current.state not in EXECUTING_STATES:
                return
            action = current.action
            await lease.set_stage("preflight")
        if action not in PUBLIC_ACTIONS:
            await lease.finish(
                MaintenanceState.INTERRUPTED.value,
                stage="interrupted",
                error_code="unsupported_action",
                error_message="内部维护动作不允许由维护执行器重放；请先只读核对",
            )
            return
        if action == MaintenanceAction.CHECK:
            await _run_check(lease, operation)
            return
        if action == MaintenanceAction.DISABLE:
            await _run_disable(lease, operation, owner)
            return
        if action == MaintenanceAction.RESTORE:
            await _run_restore(lease, operation, owner)
            return

        persist_stage = "preflight"

        async def emit(event: dict[str, Any]) -> None:
            nonlocal persist_stage
            if event.get("type") == "result":
                return
            persist_stage = await _emit_event(lease, event, persist_stage)

        provisioner = RemoteProvisioner(environment, operation, lease=lease)
        await provisioner.run(emit)

        current = await lease.snapshot()
        if current is not None and current.state in {
            *TERMINAL_STATES,
            MaintenanceState.INTERRUPTED.value,
        }:
            return
        await lease.finish(
            MaintenanceState.INTERRUPTED.value,
            stage="interrupted",
            error_code="operation_not_finished",
            error_message="执行器结束但操作没有写入终态，请核对远端状态",
        )
    except MaintenanceCanceled:
        current = await lease.snapshot()
        danger = bool(current and current.danger_dispatched)
        state = MaintenanceState.INTERRUPTED.value if danger else MaintenanceState.CANCELED.value
        await lease.event(
            event_type="canceled",
            stage="interrupted" if danger else "canceled",
            name="已取消",
            state=state,
            message=("取消时远端可能已变更，请先核对" if danger else "操作在安全检查点取消"),
        )
        await lease.finish(
            state,
            stage="interrupted" if danger else "canceled",
            error_code="canceled_after_remote_change" if danger else "canceled",
            error_message=("危险步骤已发出，取消后需核对远端状态" if danger else "操作已取消"),
        )
    except MaintenanceLeaseLost:
        logger.warning("maintenance lease lost for %s attempt %s", operation_id, attempt)
    except MaintenanceBlocked as exc:
        current = await lease.snapshot()
        danger = bool(current and current.danger_dispatched)
        state = MaintenanceState.INTERRUPTED.value if danger else MaintenanceState.FAILED.value
        await lease.event(
            event_type="blocked",
            stage="interrupted" if danger else "blocked",
            name="维护被拒绝",
            state=state,
            message=str(exc),
            detail={"code": exc.code},
        )
        await lease.finish(
            state,
            stage="interrupted" if danger else "blocked",
            error_code=exc.code,
            error_message=str(exc),
        )
    except asyncio.CancelledError:
        try:
            await lease.finish(
                MaintenanceState.INTERRUPTED.value,
                stage="interrupted",
                error_code="controller_stopped",
                error_message="执行进程停止；请先核对远端状态再重试",
            )
        except Exception:
            logger.exception("failed to persist interrupted maintenance %s", operation_id)
        raise
    except Exception as exc:
        logger.exception("maintenance execution failed for %s", operation_id)
        current = await lease.snapshot()
        danger = bool(current and current.danger_dispatched)
        state = MaintenanceState.INTERRUPTED.value if danger else MaintenanceState.FAILED.value
        await lease.finish(
            state,
            stage="interrupted" if danger else "failed",
            error_code="executor_error" if not danger else "remote_state_unknown",
            error_message=str(exc)[-1500:],
        )


async def run_maintenance_once(owner: str | None = None) -> bool:
    """Claim and execute one operation. Used by the lifespan loop and tests."""

    owner = owner or controller_id()
    await recover_expired()
    operation_id = await claim_next(owner)
    if operation_id is None:
        return False
    settings = get_settings()
    async with SessionLocal() as session:
        operation = await session.get(WorkerMaintenanceOperation, operation_id)
        if operation is None:
            return True
        attempt = operation.attempt
    lease = OperationLease(operation_id, attempt, owner, settings.worker_maintenance_lease_seconds)
    lost = asyncio.Event()
    run_task = asyncio.create_task(_execute_operation(operation_id, owner))

    async def renew_loop() -> None:
        while not run_task.done():
            await asyncio.sleep(max(0.05, settings.worker_maintenance_lease_seconds / 3))
            if run_task.done():
                return
            try:
                async with SessionLocal() as session:
                    current = await session.get(WorkerMaintenanceOperation, operation_id)
                if current is None or current.attempt != attempt:
                    lost.set()
                    run_task.cancel()
                    return
                if current.state not in EXECUTING_STATES or current.lease_owner != owner:
                    return
                renewed = await lease.renew()
            except Exception:
                logger.exception("maintenance lease renewal failed for %s", operation_id)
                lost.set()
                run_task.cancel()
                return
            if not renewed:
                lost.set()
                run_task.cancel()
                return

    renew_task = asyncio.create_task(renew_loop())
    try:
        await run_task
    except asyncio.CancelledError:
        if lost.is_set():
            logger.warning("maintenance %s canceled after lease loss", operation_id)
        else:
            raise
    finally:
        renew_task.cancel()
        await asyncio.gather(renew_task, return_exceptions=True)
    return True


async def maintenance_executor(owner: str | None = None) -> None:
    owner = owner or controller_id()
    settings = get_settings()
    delay = settings.worker_maintenance_poll_seconds
    logger.info("worker maintenance executor started as %s", owner)
    try:
        while True:
            try:
                worked = await run_maintenance_once(owner)
                delay = settings.worker_maintenance_poll_seconds
                if not worked:
                    await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker maintenance executor cycle failed")
                await asyncio.sleep(min(max(delay, 0.5), 30))
                delay = min(max(delay * 2, 1), 30)
    finally:
        await interrupt_owned(owner, "API 进程停止；请先核对远端状态再重试")
        logger.info("worker maintenance executor stopped as %s", owner)


async def event_stream(operation_id: str, after_id: int) -> AsyncIterator[dict[str, Any]]:
    """Yield persisted events until the operation is terminal and drained."""

    cursor = max(0, after_id)
    idle_seconds = 0.0
    while True:
        async with SessionLocal() as session:
            operation = await session.get(WorkerMaintenanceOperation, operation_id)
            if operation is None:
                return
            events = await events_after(session, operation_id, cursor)
            finished = operation.state in {
                *TERMINAL_STATES,
                MaintenanceState.INTERRUPTED.value,
            }
        for event in events:
            cursor = event.id
            yield event_payload(event)
        if finished:
            # A terminal write can race with the result event. Drain every page
            # so subscribers never lose events beyond the first 200.
            while True:
                async with SessionLocal() as session:
                    remaining = await events_after(session, operation_id, cursor, limit=200)
                for event in remaining:
                    cursor = event.id
                    yield event_payload(event)
                if len(remaining) < 200:
                    return
        await asyncio.sleep(0.25)
        idle_seconds += 0.25
        if idle_seconds >= 15:
            idle_seconds = 0.0
            yield {"type": "keepalive"}


async def _schedulability_blockers(session, environment) -> list[dict[str, Any]]:
    """Single fact source for health blockers and pre-dispatch refusal."""

    from app.models import AgentWorker, WorkerInstance

    if environment is None:
        return [{"code": "environment_missing", "message": "员工尚未配置运行环境"}]
    blockers: list[dict[str, Any]] = []
    employee = (
        await session.get(AgentWorker, environment.employee_id) if environment.employee_id else None
    )
    if employee is None or not employee.enabled:
        blockers.append({"code": "disabled", "message": "员工已停用"})
    if await session.scalar(select(1).where(active_maintenance_exists(environment.id))):
        blockers.append({"code": "maintenance_active", "message": "环境正在维护"})
    worker = await session.scalar(
        select(WorkerInstance).where(WorkerInstance.environment_id == environment.id)
    )
    if environment.mode == ExecutionMode.LINUX_REMOTE:
        if worker is None or worker.status != "online":
            blockers.append({"code": "worker_offline", "message": "Worker 通道离线"})
    fresh = health_is_fresh(
        environment,
        environment.status_detail,
        worker.epoch if worker is not None else None,
    )
    if environment.status != EnvironmentStatus.AVAILABLE:
        blockers.append(
            {
                "code": f"runtime_{environment.status.lower()}",
                "message": f"环境状态为 {environment.status}，禁止调度",
            }
        )
    elif not fresh:
        blockers.append(
            {
                "code": "health_stale",
                "message": "健康观测缺失或已过期，禁止调度，等待下一轮探测",
            }
        )
    detail = environment.status_detail if isinstance(environment.status_detail, dict) else {}
    capability = detail.get("capability") if isinstance(detail.get("capability"), dict) else {}
    if capability.get("runtime_status") not in {None, "applied"}:
        blockers.append({"code": "equipment_pending", "message": "装备配置等待部署生效"})
    for row in detail.get("equipment") or []:
        if not isinstance(row, dict) or not row.get("configured"):
            continue
        if row.get("deployed") is not True or row.get("verified") is not True:
            blockers.append(
                {
                    "code": "equipment_pending",
                    "message": (f"装备未完成部署或核验：{row.get('label') or row.get('key')}"),
                }
            )
    names = binding_names(environment.skill_refs, environment.mcp_refs)
    if not dispatch_allowed(detail, bindings=names):
        blockers.append({"code": "capability_not_applied", "message": "装备绑定尚未应用"})
    return blockers


async def scheduling_block(session, environment) -> str | None:
    """Return the first canonical blocker message, or None when schedulable."""

    blockers = await _schedulability_blockers(session, environment)
    return blockers[0]["message"] if blockers else None


async def environment_health(session, environment_id: int) -> dict[str, Any] | None:
    from app.models import AgentWorker, RuntimeSession, Task
    from app.runtime.occupancy import occupying_clause, release_match_clause

    environment = await session.get(OpenCodeExecutionEnvironment, environment_id)
    if environment is None:
        return None
    worker = await session.scalar(
        select(WorkerInstance).where(WorkerInstance.environment_id == environment_id)
    )
    employee = (
        await session.get(AgentWorker, environment.employee_id) if environment.employee_id else None
    )
    active = await active_operation(session, environment_id)
    latest = active or await latest_operation(session, environment_id)
    detail = environment.status_detail if isinstance(environment.status_detail, dict) else {}
    blockers = await _schedulability_blockers(session, environment)
    fresh = health_is_fresh(environment, detail, worker.epoch if worker is not None else None)
    runtime_status = environment.status
    if environment.status == EnvironmentStatus.AVAILABLE and not fresh:
        runtime_status = EnvironmentStatus.UNKNOWN.value
    if environment.employee_id:
        task_ids = list(
            (
                await session.scalars(
                    select(Task.id).where(occupying_clause(environment.employee_id)).limit(5)
                )
            ).all()
        )
        for task_id in task_ids:
            blockers.append(
                {
                    "code": "occupied_task",
                    "message": "员工存在占用任务",
                    "task_id": task_id,
                }
            )
        released_task = (
            select(Task.id).where(Task.id == RuntimeSession.task_id, release_match_clause()).exists()
        )
        session_ids = list(
            (
                await session.scalars(
                    select(RuntimeSession.id)
                    .where(
                        RuntimeSession.employee_id == environment.employee_id,
                        RuntimeSession.status.in_(["RUNNING", "WAITING", "UNKNOWN"]),
                        ~released_task,
                    )
                    .limit(5)
                )
            ).all()
        )
        for session_id in session_ids:
            blockers.append(
                {
                    "code": "active_session",
                    "message": "员工存在未结束 Session",
                    "session_id": session_id,
                }
            )
    capability = detail.get("capability") if isinstance(detail.get("capability"), dict) else {}
    return {
        "environment_id": environment.id,
        "employee_id": environment.employee_id,
        "enabled": bool(employee.enabled) if employee is not None else False,
        "connection": {
            "channel": environment.worker_channel,
            "state": (worker.status if worker is not None else "offline")
            if environment.mode == ExecutionMode.LINUX_REMOTE
            else "local",
            "last_heartbeat_at": _iso(worker.last_heartbeat_at) if worker is not None else None,
            "epoch": worker.epoch if worker is not None else None,
        },
        "maintenance": (
            {
                "state": latest.state,
                "operation_id": latest.id,
                "stage": latest.stage,
                "attempt": latest.attempt,
            }
            if latest is not None
            else None
        ),
        "runtime_health": {
            "status": runtime_status,
            "last_checked_at": _iso(environment.last_checked_at),
            "capability": capability,
            "equipment": list(detail.get("equipment") or []),
        },
        "repository": detail.get("repository"),
        "schedulable": not blockers,
        "blocking": blockers,
    }
