"""Worker 维护队列、租约、互斥占用与操作事件。

确认执行已停止后才释放 active_worker_id。租约过期或控制面停机时，
远端进程可能仍在运行，保留占用并等待人工确认停止。
Worker enabled 仅控制任务调度，不进入维护队列。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from app.config.settings import get_settings
from app.database import SessionLocal
from app.domain import ExecutionMode, MaintenanceAction, MaintenanceState
from app.executors.errors import OpenCodeServerUnavailable
from app.models import (
    AgentWorker,
    Task,
    WorkerMaintenanceEvent,
    WorkerMaintenanceOperation,
)
from app.runtime.environment import ExecutionEnvironmentRuntime, environment_snapshot
from app.runtime.occupancy import (
    occupying_clause,
    occupying_exists,
)
from app.runtime.redaction import redact_data, redact_text
from app.runtime.worker_client import WorkerOpenCodeClient
from app.runtime.worker_connection import worker_connections
from app.runtime.worker_health import (
    probe_online_worker,
)
from app.runtime.worker_readiness import (
    build_desired_spec,
    capability_spec_revision,
    compute_target_digest,
    evaluate_worker_readiness,
)
from app.runtime.worker_transport import WorkerCommandError
from app.worker_agent import __version__ as WORKER_AGENT_VERSION
from app.worker_gateway.service import get_gateway

logger = logging.getLogger(__name__)

EXECUTING_STATES = (MaintenanceState.RUNNING.value,)
ACTIVE_STATES = (
    MaintenanceState.QUEUED.value,
    MaintenanceState.RUNNING.value,
)
TERMINAL_STATES = (
    MaintenanceState.SUCCEEDED.value,
    MaintenanceState.FAILED.value,
    MaintenanceState.CANCELED.value,
)
RELEASING_STATES = TERMINAL_STATES
DEFAULT_REQUESTED_BY = "api"
PUBLIC_ACTIONS = frozenset(item.value for item in MaintenanceAction)
DISRUPTIVE_ACTIONS = frozenset(
    {
        MaintenanceAction.DEPLOY.value,
        MaintenanceAction.UPDATE.value,
        MaintenanceAction.RESTART.value,
    }
)


class MaintenanceCanceled(RuntimeError):
    """操作收到取消请求并安全退出。"""


class MaintenanceLeaseLost(RuntimeError):
    """当前执行器失去操作租约。"""


class MaintenanceBlocked(RuntimeError):
    """执行前二次占用检查拦截破坏性操作。"""


class _ActionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def controller_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:10]}"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def operation_payload(operation: WorkerMaintenanceOperation) -> dict[str, Any]:
    return {
        "id": operation.id,
        "agent_worker_id": operation.agent_worker_id,
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


def active_maintenance_exists(agent_worker_id: str):
    return (
        select(WorkerMaintenanceOperation.id)
        .where(
            WorkerMaintenanceOperation.agent_worker_id == agent_worker_id,
            WorkerMaintenanceOperation.active_worker_id.is_not(None),
        )
        .exists()
    )


async def active_operation(session, agent_worker_id: str) -> WorkerMaintenanceOperation | None:
    return await session.scalar(
        select(WorkerMaintenanceOperation)
        .where(
            WorkerMaintenanceOperation.agent_worker_id == agent_worker_id,
            WorkerMaintenanceOperation.active_worker_id.is_not(None),
        )
        .order_by(
            WorkerMaintenanceOperation.updated_at.desc(),
            WorkerMaintenanceOperation.id.desc(),
        )
        .limit(1)
    )


async def latest_operation(session, agent_worker_id: str) -> WorkerMaintenanceOperation | None:
    return await session.scalar(
        select(WorkerMaintenanceOperation)
        .where(WorkerMaintenanceOperation.agent_worker_id == agent_worker_id)
        .order_by(
            WorkerMaintenanceOperation.updated_at.desc(),
            WorkerMaintenanceOperation.id.desc(),
        )
        .limit(1)
    )


async def require_no_active_maintenance(session, agent_worker_id: str | None) -> None:
    if agent_worker_id is None:
        return
    active = await session.scalar(
        select(WorkerMaintenanceOperation.id)
        .where(
            WorkerMaintenanceOperation.agent_worker_id == agent_worker_id,
            WorkerMaintenanceOperation.active_worker_id.is_not(None),
        )
        .with_for_update()
        .limit(1)
    )
    if active is not None:
        raise HTTPException(409, "远程环境正在维护，请等待完成或先取消当前操作")


async def lock_environment_row(session, environment: AgentWorker) -> None:
    """获取环境行写锁，防止维护入队与任务认领/环境写入并发竞态。"""

    result = await session.execute(
        update(AgentWorker)
        .where(AgentWorker.id == environment.id)
        .values(updated_at=AgentWorker.updated_at)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise HTTPException(404, "Worker 环境不存在")
    await session.refresh(environment, with_for_update=True)


def _validate_action(environment: AgentWorker, action: str) -> None:
    settings = get_settings()
    if action not in PUBLIC_ACTIONS:
        raise HTTPException(422, "不支持的维护动作")
    if action in DISRUPTIVE_ACTIONS and environment.mode != ExecutionMode.LINUX_REMOTE:
        raise HTTPException(409, "仅 Linux 远程员工支持部署、更新和重启")
    if action in DISRUPTIVE_ACTIONS and not (environment.workspace_root or "").startswith("/"):
        raise HTTPException(409, "工程目录必须是 Linux 绝对路径")
    if action == MaintenanceAction.DEPLOY.value:
        if not environment.host or not environment.username or not environment.ssh_password:
            raise HTTPException(409, "一键部署需要远程 IP、用户名和 SSH 密码")
        if settings.mcp_transport != "streamable-http":
            raise HTTPException(
                409,
                "请先配置 NEXUS_MCP_TRANSPORT=streamable-http 并启动数字图书馆 HTTP 服务",
            )
    if action == MaintenanceAction.UPDATE.value and not environment.repository_url:
        raise HTTPException(409, "更新需要先绑定工程仓库")


def _target_revision(environment: AgentWorker) -> dict[str, Any]:
    settings = get_settings()
    return {
        "target_digest": compute_target_digest(environment),
        "opencode_version": settings.remote_opencode_version,
        "worker_agent_version": WORKER_AGENT_VERSION,
        "model_name": environment.model_name,
        "repository_url": environment.repository_url,
        "branch": environment.branch,
        "skills": list(environment.skill_refs or []),
        "mcp": [item.get("name") for item in (environment.mcp_refs or []) if isinstance(item, dict)],
        "capability_revision": capability_spec_revision(environment.skill_refs, environment.mcp_refs),
    }


async def _occupancy_blockers(session, employee_id: str | None) -> list[str]:
    if not employee_id:
        return []
    blockers: list[str] = []
    if await session.scalar(select(1).where(occupying_exists(employee_id))):
        blockers.append("occupied_task")
    return blockers


def _initial_execution_route(environment: AgentWorker, action: str) -> tuple[str | None, str | None]:
    if action == MaintenanceAction.DEPLOY.value:
        return "ssh", "通过 SSH 执行一键部署与守护拉起"
    if action == MaintenanceAction.UPDATE.value:
        return "worker", "通过在线 Worker Gateway 更新分支代码"
    if action == MaintenanceAction.CHECK.value:
        if environment.mode == ExecutionMode.LOCAL:
            return "local", "检查本机开发运行环境"
        return "worker", "通过在线 Worker Gateway 执行深度健康检查"
    return None, None


async def submit_operation(
    session,
    agent_worker_id: str,
    action: str,
    *,
    requested_by: str = DEFAULT_REQUESTED_BY,
) -> WorkerMaintenanceOperation:
    """原子校验并写入排队维护操作。"""

    environment = await session.scalar(select(AgentWorker).where(AgentWorker.id == agent_worker_id))
    if environment is None:
        raise HTTPException(404, "Worker 环境不存在")
    _validate_action(environment, action)
    await lock_environment_row(session, environment)
    await require_no_active_maintenance(session, environment.id)
    if action in DISRUPTIVE_ACTIONS:
        blockers = await _occupancy_blockers(session, environment.id)
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
    channel, reason = _initial_execution_route(environment, action)
    operation = WorkerMaintenanceOperation(
        id="op_" + uuid4().hex,
        agent_worker_id=environment.id,
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
        active_worker_id=environment.id,
        requested_at=now,
    )
    session.add(operation)
    try:
        await session.flush()
        session.add(
            WorkerMaintenanceEvent(
                operation_id=operation.id,
                attempt=1,
                type="queued",
                stage="queued",
                name="操作已入队",
                state=MaintenanceState.QUEUED.value,
                message=f"维护动作 {action} 已入队",
                detail={"requested_by": operation.requested_by, "action": action},
            )
        )
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
    """清理排队取消项后，认领一条 QUEUED 操作进入 RUNNING。"""

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
            result = await session.execute(
                update(WorkerMaintenanceOperation)
                .where(
                    WorkerMaintenanceOperation.id == operation.id,
                    WorkerMaintenanceOperation.state == MaintenanceState.QUEUED.value,
                    WorkerMaintenanceOperation.attempt == operation.attempt,
                )
                .values(
                    state=MaintenanceState.CANCELED.value,
                    stage="canceled",
                    active_worker_id=None,
                    lease_owner=None,
                    lease_until=None,
                    finished_at=now,
                    updated_at=now,
                    error_code="canceled",
                    error_message="操作在执行前被取消",
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount:
                session.add(
                    WorkerMaintenanceEvent(
                        operation_id=operation.id,
                        attempt=operation.attempt,
                        type="canceled",
                        stage="canceled",
                        name="已取消",
                        state=MaintenanceState.CANCELED.value,
                        message="操作在执行前被取消",
                        detail={"reason": "canceled"},
                    )
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
                state=MaintenanceState.RUNNING.value,
                stage="running",
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
        session.add(
            WorkerMaintenanceEvent(
                operation_id=candidate.id,
                attempt=candidate.attempt,
                type="started",
                stage="running",
                name="开始执行",
                state=MaintenanceState.RUNNING.value,
                message=f"维护执行器已认领 {candidate.action}",
                detail={"lease_owner": owner},
            )
        )
        await session.commit()
        return candidate.id


async def recover_expired(owner=None):
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        operations = (
            await session.scalars(
                select(WorkerMaintenanceOperation)
                .where(
                    WorkerMaintenanceOperation.state == "RUNNING",
                    WorkerMaintenanceOperation.lease_until <= now,
                    *([WorkerMaintenanceOperation.lease_owner != owner] if owner else []),
                )
                .with_for_update()
            )
        ).all()
        for operation in operations:
            operation.state = "FAILED"
            operation.stage = "stop_unconfirmed"
            operation.error_code = "lease_expired"
            operation.error_message = "控制进程失联；请确认远端维护进程已停止后释放维护占用"
            operation.lease_owner = None
            operation.lease_until = None
            operation.finished_at = now
            session.add(
                WorkerMaintenanceEvent(
                    operation_id=operation.id,
                    stage="stop_unconfirmed",
                    name="维护租约过期",
                    state="FAILED",
                    message=operation.error_message,
                )
            )
        await session.commit()
        return len(operations)


async def confirm_maintenance_stopped(session, operation_id, actor):
    operation = await session.get(WorkerMaintenanceOperation, operation_id)
    if not operation or operation.stage != "stop_unconfirmed":
        raise HTTPException(409, "操作无需人工释放")
    operation.active_worker_id = None
    operation.stage = "stopped_confirmed"
    session.add(
        WorkerMaintenanceEvent(
            operation_id=operation.id,
            attempt=operation.attempt,
            type="stop_confirmed",
            message="操作者已确认远端维护进程停止",
            detail={"actor": actor},
        )
    )
    await session.commit()
    return operation


async def _finish_in_session(
    session,
    operation_id: str,
    owner: str,
    attempt: int,
    state: str,
    *,
    stage: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    after_snapshot: dict[str, Any] | None = None,
    event_message: str | None = None,
    secrets: tuple[str, ...] | list[str] = (),
) -> bool:
    if state not in TERMINAL_STATES:
        raise ValueError(f"invalid terminal maintenance state: {state}")
    now = datetime.now(UTC)
    resolved_stage = stage or state.lower()
    redacted_error = redact_text(error_message, secrets) if error_message else None
    redacted_snapshot = redact_data(after_snapshot or {}, secrets)

    result = await session.execute(
        update(WorkerMaintenanceOperation)
        .where(
            WorkerMaintenanceOperation.id == operation_id,
            WorkerMaintenanceOperation.attempt == attempt,
            WorkerMaintenanceOperation.lease_owner == owner,
            WorkerMaintenanceOperation.state.in_(EXECUTING_STATES),
        )
        .values(
            state=state,
            stage=resolved_stage,
            active_worker_id=WorkerMaintenanceOperation.active_worker_id
            if resolved_stage == "stop_unconfirmed"
            else None,
            lease_owner=None,
            lease_until=None,
            finished_at=now,
            error_code=error_code,
            error_message=redacted_error,
            after_snapshot=redacted_snapshot,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False

    event_type = (
        "completed"
        if state == MaintenanceState.SUCCEEDED.value
        else ("canceled" if state == MaintenanceState.CANCELED.value else "failed")
    )
    session.add(
        WorkerMaintenanceEvent(
            operation_id=operation_id,
            attempt=attempt,
            type=event_type,
            stage=resolved_stage,
            name="执行完成" if state == MaintenanceState.SUCCEEDED.value else "执行结束",
            state=state,
            message=redact_text(event_message or redacted_error or state, secrets),
            detail={"error_code": error_code} if error_code else {},
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
            event_message=event_message,
            secrets=secrets,
        )
        await session.commit()
        return applied


async def interrupt_owned(owner, reason):
    async with SessionLocal() as session:
        result = await session.execute(
            update(WorkerMaintenanceOperation)
            .where(
                WorkerMaintenanceOperation.lease_owner == owner,
                WorkerMaintenanceOperation.state == "RUNNING",
            )
            .values(
                state="FAILED",
                stage="stop_unconfirmed",
                error_code="controller_stopped",
                error_message=reason,
                lease_owner=None,
                lease_until=None,
                finished_at=datetime.now(UTC),
            )
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


async def events_before(session, operation_id: str, before_id: int | None, *, limit: int):
    query = select(WorkerMaintenanceEvent).where(WorkerMaintenanceEvent.operation_id == operation_id)
    if before_id is not None:
        query = query.where(WorkerMaintenanceEvent.id < before_id)
    rows = (await session.scalars(query.order_by(WorkerMaintenanceEvent.id.desc()).limit(limit))).all()
    return list(reversed(rows))


async def operation_detail(session, operation_id: str) -> dict[str, Any] | None:
    operation = await session.get(WorkerMaintenanceOperation, operation_id)
    if operation is None:
        return None
    events = await events_before(session, operation_id, None, limit=100)
    payload = operation_payload(operation)
    payload["events"] = [event_payload(item) for item in events]
    return payload


async def list_operations(
    session, agent_worker_id: str, *, limit: int, offset: int,
    action: str | None = None, state: str | None = None,
) -> dict[str, Any]:
    query = select(WorkerMaintenanceOperation).where(
        WorkerMaintenanceOperation.agent_worker_id == agent_worker_id
    )
    if action:
        query = query.where(WorkerMaintenanceOperation.action == action)
    if state:
        query = query.where(WorkerMaintenanceOperation.state == state)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.scalars(
        query.order_by(
            WorkerMaintenanceOperation.requested_at.desc(),
            WorkerMaintenanceOperation.id.desc(),
        ).offset(offset).limit(limit)
    )
    return {"items": [operation_payload(item) for item in rows], "total": total}


async def cancel_operation(
    session, operation_id: str, *, actor: str | None = None
) -> WorkerMaintenanceOperation:
    operation = await session.get(WorkerMaintenanceOperation, operation_id)
    if operation is None:
        raise HTTPException(404, "维护操作不存在")
    if operation.state in TERMINAL_STATES:
        return operation

    now = datetime.now(UTC)

    if operation.state == MaintenanceState.QUEUED.value:
        result = await session.execute(
            update(WorkerMaintenanceOperation)
            .where(
                WorkerMaintenanceOperation.id == operation.id,
                WorkerMaintenanceOperation.state == operation.state,
                WorkerMaintenanceOperation.attempt == operation.attempt,
            )
            .values(
                state=MaintenanceState.CANCELED.value,
                stage="canceled",
                cancel_requested=True,
                active_worker_id=None,
                lease_owner=None,
                lease_until=None,
                finished_at=now,
                error_code="canceled",
                error_message="维护操作已取消",
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount:
            detail: dict[str, Any] = {"reason": "canceled"}
            if actor:
                detail["requested_by"] = actor
            session.add(
                WorkerMaintenanceEvent(
                    operation_id=operation.id,
                    attempt=operation.attempt,
                    type="canceled",
                    stage="canceled",
                    name="已取消",
                    state=MaintenanceState.CANCELED.value,
                    message="维护操作已取消",
                    detail=detail,
                )
            )
        await session.commit()
        await session.refresh(operation)
        return operation

    result = await session.execute(
        update(WorkerMaintenanceOperation)
        .where(
            WorkerMaintenanceOperation.id == operation.id,
            WorkerMaintenanceOperation.state.in_(EXECUTING_STATES),
            WorkerMaintenanceOperation.attempt == operation.attempt,
        )
        .values(cancel_requested=True, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount:
        detail = {"reason": "cancel_requested"}
        if actor:
            detail["requested_by"] = actor
        session.add(
            WorkerMaintenanceEvent(
                operation_id=operation.id,
                attempt=operation.attempt,
                type="cancel_requested",
                stage=operation.stage,
                name="收到取消请求",
                state=operation.state,
                message="已记录取消请求，正在安全终止当前步骤并释放环境锁",
                detail=detail,
            )
        )
    await session.commit()
    await session.refresh(operation)
    return operation


async def retry_operation(
    session, operation_id: str, *, actor: str | None = None
) -> WorkerMaintenanceOperation:
    operation = await session.get(WorkerMaintenanceOperation, operation_id)
    if operation is None:
        raise HTTPException(404, "维护操作不存在")
    if operation.state not in {MaintenanceState.FAILED.value, MaintenanceState.CANCELED.value}:
        raise HTTPException(409, "仅失败或已取消的维护操作支持重试")

    environment = await session.get(AgentWorker, operation.agent_worker_id)
    if environment is None:
        raise HTTPException(404, "Worker 环境不存在")
    _validate_action(environment, operation.action)
    await lock_environment_row(session, environment)
    await require_no_active_maintenance(session, environment.id)
    if operation.action in DISRUPTIVE_ACTIONS:
        blockers = await _occupancy_blockers(session, environment.id)
        if blockers:
            await session.rollback()
            raise HTTPException(
                409,
                "员工存在占用或未结束 Session，不能部署或重启"
                if len(blockers) > 1
                else "员工正在执行任务或等待人工确认，不能部署或重启",
            )

    now = datetime.now(UTC)
    previous_attempt = operation.attempt
    next_attempt = previous_attempt + 1
    channel, reason = _initial_execution_route(environment, operation.action)
    result = await session.execute(
        update(WorkerMaintenanceOperation)
        .where(
            WorkerMaintenanceOperation.id == operation.id,
            WorkerMaintenanceOperation.attempt == previous_attempt,
            WorkerMaintenanceOperation.state.in_(
                (MaintenanceState.FAILED.value, MaintenanceState.CANCELED.value)
            ),
        )
        .values(
            state=MaintenanceState.QUEUED.value,
            stage="queued",
            attempt=next_attempt,
            retry_of_attempt=previous_attempt,
            active_worker_id=environment.id,
            lease_owner=None,
            lease_until=None,
            cancel_requested=False,
            execution_channel=channel,
            execution_reason=reason,
            target_revision=_target_revision(environment),
            before_snapshot=environment_snapshot(environment),
            after_snapshot={},
            started_at=None,
            finished_at=None,
            error_code=None,
            error_message=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.rollback()
        raise HTTPException(409, "维护操作状态已变更，请刷新后重试")

    detail: dict[str, Any] = {"retry_of_attempt": previous_attempt}
    if actor:
        detail["requested_by"] = actor
    session.add(
        WorkerMaintenanceEvent(
            operation_id=operation.id,
            attempt=next_attempt,
            type="retry",
            stage="queued",
            name="重新排队",
            state=MaintenanceState.QUEUED.value,
            message=f"第 {next_attempt} 次尝试已重新入队",
            detail=detail,
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "该环境已有维护操作，请等待完成或先取消") from exc
    await session.refresh(operation)
    return operation


class OperationLease:
    """运行中维护操作的租约管理与状态写回封装。"""

    def __init__(
        self,
        operation_id: str,
        attempt: int,
        owner: str,
        lease_seconds: int,
        *,
        secrets: tuple[str, ...] = (),
    ):
        self.operation_id = operation_id
        self.attempt = attempt
        self.owner = owner
        self.lease_seconds = lease_seconds
        self.secrets = secrets

    async def snapshot(self) -> WorkerMaintenanceOperation | None:
        async with SessionLocal() as session:
            operation = await session.get(WorkerMaintenanceOperation, self.operation_id)
            if operation is not None:
                session.expunge(operation)
            return operation

    async def renew(self) -> bool:
        now = datetime.now(UTC)
        lease_until = now + timedelta(seconds=self.lease_seconds)
        async with SessionLocal() as session:
            result = await session.execute(
                update(WorkerMaintenanceOperation)
                .where(
                    WorkerMaintenanceOperation.id == self.operation_id,
                    WorkerMaintenanceOperation.attempt == self.attempt,
                    WorkerMaintenanceOperation.lease_owner == self.owner,
                    WorkerMaintenanceOperation.state.in_(EXECUTING_STATES),
                )
                .values(lease_until=lease_until, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            return result.rowcount == 1

    async def check_canceled(self) -> None:
        current = await self.snapshot()
        if (
            current is None
            or current.attempt != self.attempt
            or current.lease_owner != self.owner
            or current.state not in EXECUTING_STATES
        ):
            raise MaintenanceLeaseLost("维护租约已失效")
        if current.cancel_requested:
            raise MaintenanceCanceled("维护操作已取消")

    async def assert_safe_to_disrupt(self, agent_worker_id: str) -> None:
        await self.check_canceled()
        async with SessionLocal() as session:
            blockers = await _occupancy_blockers(session, agent_worker_id)
        if blockers:
            raise MaintenanceBlocked("Worker 有未释放任务，已中止破坏性维护动作")

    async def set_execution_route(self, channel: str, reason: str) -> None:
        now = datetime.now(UTC)
        async with SessionLocal() as session:
            await session.execute(
                update(WorkerMaintenanceOperation)
                .where(
                    WorkerMaintenanceOperation.id == self.operation_id,
                    WorkerMaintenanceOperation.attempt == self.attempt,
                    WorkerMaintenanceOperation.lease_owner == self.owner,
                    WorkerMaintenanceOperation.state.in_(EXECUTING_STATES),
                )
                .values(execution_channel=channel, execution_reason=reason, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            await session.commit()

    async def emit(
        self,
        *,
        event_type: str = "step",
        stage: str | None = "running",
        name: str | None = None,
        state: str | None = None,
        message: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        async with SessionLocal() as session:
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

    async def finish(
        self,
        state: str,
        *,
        stage: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        after_snapshot: dict[str, Any] | None = None,
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
            event_message=event_message,
            secrets=self.secrets,
        )


def _make_step_emitter(lease: OperationLease):
    async def emit(item: dict[str, Any]) -> None:
        detail = item.get("detail")
        await lease.emit(
            event_type="step",
            stage="running",
            name=str(item.get("name") or "维护步骤"),
            state=str(item.get("state") or "running"),
            message=str(item.get("message") or (detail if isinstance(detail, str) else "")),
            detail=detail if isinstance(detail, dict) else {},
        )

    return emit


async def _emit_equipment_steps(emit, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        if not row.get("configured"):
            continue
        row_state = str(row.get("state") or "unknown")
        step_state = "passed" if row_state == "ready" else "failed"
        label = str(row.get("label") or row.get("key") or "装备检查")
        msg = (
            str(row.get("error") or "")
            or "; ".join(str(d) for d in (row.get("diff") or []) if d)
            or str(row.get("detail") or row_state)
        )
        await emit({"name": f"核验 {label}", "state": step_state, "message": msg})


async def _execute_check(environment, lease, emit):
    await lease.check_canceled()
    if environment.mode == ExecutionMode.LOCAL and get_settings().executor_type == "opencode_server":
        from app.runtime.session_connection import connect

        async with connect(environment) as client:
            health = await client.health()
        if health.get("healthy") is not True:
            raise _ActionError("native_unhealthy", "本机 OpenCode 原生服务未通过健康检查")
        await emit({"name": "本机原生服务", "state": "passed", "message": "原生服务健康及版本检查通过"})
        await lease.finish("SUCCEEDED", after_snapshot={"scope": "local_opencode_health", "opencode": health})
        return
    async with SessionLocal() as db:
        desired = await build_desired_spec(db, environment)
    observation = await probe_online_worker(environment, desired, deep=True)
    evaluated = evaluate_worker_readiness(desired, environment, observation=observation)
    await _emit_equipment_steps(emit, evaluated["runtime_health"]["equipment"])
    ok = evaluated["readiness"] == "READY"
    async with SessionLocal() as db:
        await get_gateway().update_observed_snapshot(db, environment.id, environment.epoch, observation)
    await lease.finish(
        "SUCCEEDED" if ok else "FAILED",
        after_snapshot=evaluated,
        error_message=None if ok else "运行环境检查未通过",
    )


async def _execute_restart(environment, lease, emit):
    await lease.assert_safe_to_disrupt(environment.id)
    if environment.status in {"IDLE", "WORKING", "PAUSED"}:
        await lease.set_execution_route("worker", "通过 Worker 重启原生 OpenCode")
        result = await WorkerOpenCodeClient(agent_worker_id=environment.id).run_command(
            "restart_opencode", {"workspace": environment.workspace_root}
        )
        if not result.get("restarted"):
            raise _ActionError("restart_failed", "Worker 未确认重启")
    else:
        from app.runtime.remote_provisioning import worker_ctl_path

        await lease.set_execution_route("ssh", "通过受管启动入口唤醒 Worker")
        ssh = await ExecutionEnvironmentRuntime().open_session(environment)
        try:
            code, _, error = await ssh.run_command(shlex.join([worker_ctl_path(environment.id), "start"]))
            if code:
                raise _ActionError("restart_failed", redact_text(error))
        finally:
            await ssh.close()
        for _ in range(60):
            await lease.check_canceled()
            async with SessionLocal() as db:
                fresh = await db.get(AgentWorker, environment.id)
            if fresh.status in {"IDLE", "WORKING", "PAUSED"} and fresh.epoch > environment.epoch:
                break
            await asyncio.sleep(0.5)
        else:
            raise _ActionError("worker_offline", "启动后 Worker 未连接网关")
        result = {"connected": True}
    await lease.finish("SUCCEEDED", after_snapshot=redact_data(result))


async def _execute_update(environment, lease, emit):
    await lease.assert_safe_to_disrupt(environment.id)
    result = await WorkerOpenCodeClient(agent_worker_id=environment.id).run_command(
        "update_workspace", {"workspace": environment.workspace_root, "branch": environment.branch or "main"}
    )
    await lease.finish("SUCCEEDED", after_snapshot=redact_data(result))


async def _execute_operation(operation_id: str, owner: str) -> None:
    from app.runtime.remote_provisioning import RemoteProvisioner

    settings = get_settings()
    async with SessionLocal() as session:
        operation = await session.get(WorkerMaintenanceOperation, operation_id)
        if operation is None or operation.lease_owner != owner or operation.state not in EXECUTING_STATES:
            return
        environment = await session.get(AgentWorker, operation.agent_worker_id)
        attempt = operation.attempt
        action = operation.action
        if operation is not None:
            session.expunge(operation)
        if environment is not None:
            session.expunge(environment)

    secrets = tuple(
        secret for secret in (environment.ssh_password if environment is not None else None,) if secret
    )
    lease = OperationLease(
        operation_id,
        attempt,
        owner,
        settings.worker_maintenance_lease_seconds,
        secrets=secrets,
    )
    if environment is None:
        await lease.finish(
            MaintenanceState.FAILED.value,
            stage="failed",
            error_code="environment_missing",
            error_message="Worker 环境已不存在",
        )
        return

    emit = _make_step_emitter(lease)
    try:
        await lease.check_canceled()
        if action == MaintenanceAction.DEPLOY.value:
            await RemoteProvisioner(environment, operation, lease=lease).run(emit)
        elif action == MaintenanceAction.RESTART.value:
            await _execute_restart(environment, lease, emit)
        elif action == MaintenanceAction.UPDATE.value:
            await _execute_update(environment, lease, emit)
        elif action == MaintenanceAction.CHECK.value:
            await _execute_check(environment, lease, emit)
        else:
            raise _ActionError("unsupported_action", f"不支持的维护动作：{action}")
    except MaintenanceLeaseLost:
        logger.warning("maintenance lease lost for %s attempt %s", operation_id, attempt)
    except MaintenanceCanceled as exc:
        await lease.finish(
            MaintenanceState.CANCELED.value,
            stage="canceled",
            error_code="canceled",
            error_message=str(exc) or "维护操作已取消",
            event_message="维护操作已取消，环境锁已释放",
        )
    except MaintenanceBlocked as exc:
        await lease.finish(
            MaintenanceState.FAILED.value,
            stage="failed",
            error_code="occupied",
            error_message=str(exc),
            event_message=str(exc),
        )
    except _ActionError as exc:
        await lease.finish(
            MaintenanceState.FAILED.value,
            stage="failed",
            error_code=exc.code,
            error_message=exc.message,
            event_message=exc.message,
        )
    except (WorkerCommandError, OpenCodeServerUnavailable) as exc:
        await lease.finish(
            "FAILED",
            stage="failed",
            error_code=getattr(exc, "code", "worker_offline"),
            error_message=redact_text(str(exc), secrets),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("maintenance execution failed for %s", operation_id)
        message = redact_text(str(exc) or exc.__class__.__name__, secrets)
        await lease.finish(
            MaintenanceState.FAILED.value,
            stage="stop_unconfirmed" if action in DISRUPTIVE_ACTIONS else "failed",
            error_code="execution_unknown" if action in DISRUPTIVE_ACTIONS else "execution_error",
            error_message=message,
            event_message=message,
        )


async def run_maintenance_once(owner: str | None = None) -> bool:
    """认领并执行单条维护操作（供后台循环与测试直接调用）。"""

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
        await interrupt_owned(owner, "控制面进程停止，环境维护锁已自动释放")
        logger.info("worker maintenance executor stopped as %s", owner)


async def event_stream(operation_id: str, after_id: int) -> AsyncIterator[dict[str, Any]]:
    """按游标推送维护事件，直至操作进入终态并排空事件。"""

    cursor = max(0, after_id)
    idle_rounds = 0
    while True:
        async with SessionLocal() as session:
            operation = await session.get(WorkerMaintenanceOperation, operation_id)
            events = await events_after(session, operation_id, cursor, limit=100)
            terminal = operation is None or operation.state in TERMINAL_STATES
        for item in events:
            cursor = max(cursor, item.id)
            yield event_payload(item)
        if terminal and not events:
            return
        if events:
            idle_rounds = 0
            continue
        idle_rounds += 1
        if idle_rounds >= 20:
            idle_rounds = 0
            yield {"type": "keepalive"}
        await asyncio.sleep(0.5)


async def environment_health(session, agent_worker_id):
    worker = await session.get(AgentWorker, agent_worker_id)
    if worker is None:
        return None
    operation = await active_operation(session, worker.id) or await latest_operation(session, worker.id)
    tasks = (await session.scalars(select(Task.id).where(occupying_clause(worker.id)))).all()
    desired = await build_desired_spec(session, worker)
    gateway = get_gateway()
    online = (await worker_connections([worker], bus=gateway.bus))[worker.id]
    observation = await gateway.observed_snapshot(worker) if online else None
    health = evaluate_worker_readiness(
        desired,
        worker,
        observation=observation,
        occupying_task_ids=tasks,
        latest_operation=operation,
        online=online,
    )
    from app.runtime.experience_review.coordinator import source_worker_in_use

    if await source_worker_in_use(session, worker.id):
        health["blocking"].append({"code": "review_active", "message": "Worker 仍是未完成经验复盘的来源"})
    return health
