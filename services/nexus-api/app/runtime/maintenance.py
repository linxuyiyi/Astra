"""Agent Worker 五状态无死锁维护操作、租约续期与动作执行。

设计边界：
1. 维护操作仅保留活跃态 ``QUEUED | RUNNING`` 与终态 ``SUCCEEDED | FAILED | CANCELED``，
   彻底移除 ``INTERRUPTED / PREFLIGHT / VERIFYING`` 中间阻塞态与 ``reconcile`` 死锁路径。
2. 任何进入终态（成功、失败、取消、租约过期、控制面停机）的操作均立即释放 ``active_environment_id`` 互斥锁；
   任务能否调度完全由 ``evaluate_worker_readiness`` 基于实时观测快照判定，绝不靠锁住维护表阻断恢复。
3. 员工调度开关（``enabled``）通过员工同步接口即时生效，不再走异步维护队列。
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

from anyio import CancelScope
from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from app.config.settings import get_settings
from app.database import SessionLocal
from app.domain import EnvironmentStatus, ExecutionMode, MaintenanceAction, MaintenanceState
from app.models import (
    AgentWorker,
    OpenCodeExecutionEnvironment,
    RuntimeSession,
    Task,
    WorkerInstance,
    WorkerMaintenanceEvent,
    WorkerMaintenanceOperation,
)
from app.runtime.environment import ExecutionEnvironmentRuntime, environment_snapshot
from app.runtime.occupancy import (
    blocking_session_exists,
    occupying_clause,
    occupying_exists,
    release_match_clause,
)
from app.runtime.redaction import redact_data, redact_text
from app.runtime.worker_client import WorkerOpenCodeClient
from app.runtime.worker_health import (
    expected_inspection_payload,
    normalize_probe_result,
    probe_online_worker,
)
from app.runtime.worker_readiness import (
    build_desired_spec,
    capability_spec_revision,
    compute_target_digest,
    evaluate_worker_readiness,
)
from app.runtime.worker_readiness import (
    scheduling_block as readiness_scheduling_block,
)
from app.worker_agent import __version__ as WORKER_AGENT_VERSION
from app.worker_agent.inspection import inspect_runtime
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
    """获取环境行写锁，防止维护入队与任务认领/环境写入并发竞态。"""

    result = await session.execute(
        update(OpenCodeExecutionEnvironment)
        .where(OpenCodeExecutionEnvironment.id == environment.id)
        .values(updated_at=OpenCodeExecutionEnvironment.updated_at)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise HTTPException(404, "Worker 环境不存在")
    await session.refresh(environment, with_for_update=True)


def _validate_action(environment: OpenCodeExecutionEnvironment, action: str) -> None:
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


def _target_revision(environment: OpenCodeExecutionEnvironment) -> dict[str, Any]:
    settings = get_settings()
    return {
        "target_digest": compute_target_digest(environment),
        "opencode_version": settings.remote_opencode_version,
        "worker_agent_version": WORKER_AGENT_VERSION,
        "model_name": environment.model_name,
        "opencode_path": environment.opencode_path,
        "repository_url": environment.repository_url,
        "branch": environment.branch,
        "skills": list(environment.skill_refs or []),
        "mcp": [
            item.get("name") for item in (environment.mcp_refs or []) if isinstance(item, dict)
        ],
        "capability_revision": capability_spec_revision(
            environment.skill_refs, environment.mcp_refs
        ),
    }


async def _occupancy_blockers(session, employee_id: str | None) -> list[str]:
    if not employee_id:
        return []
    blockers: list[str] = []
    if await session.scalar(select(1).where(occupying_exists(employee_id))):
        blockers.append("occupied_task")
    if await session.scalar(select(1).where(blocking_session_exists(employee_id))):
        blockers.append("active_session")
    return blockers


def _initial_execution_route(
    environment: OpenCodeExecutionEnvironment, action: str
) -> tuple[str | None, str | None]:
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
    environment_id: int,
    action: str,
    *,
    requested_by: str = DEFAULT_REQUESTED_BY,
) -> WorkerMaintenanceOperation:
    """原子校验并写入排队维护操作。"""

    environment = await session.scalar(
        select(OpenCodeExecutionEnvironment).where(
            OpenCodeExecutionEnvironment.id == environment_id
        )
    )
    if environment is None:
        raise HTTPException(404, "Worker 环境不存在")
    _validate_action(environment, action)
    await lock_environment_row(session, environment)
    await require_no_active_maintenance(session, environment.id)
    if action in DISRUPTIVE_ACTIONS:
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
    channel, reason = _initial_execution_route(environment, action)
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
                    active_environment_id=None,
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


async def recover_expired(owner: str | None = None) -> int:
    """将租约过期的 RUNNING 操作立即收敛为终态并释放环境锁（无 INTERRUPTED 死锁）。"""

    now = datetime.now(UTC)
    recovered = 0
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
            target_state = (
                MaintenanceState.CANCELED.value
                if operation.cancel_requested
                else MaintenanceState.FAILED.value
            )
            target_stage = "canceled" if operation.cancel_requested else "failed"
            error_code = "canceled" if operation.cancel_requested else "lease_expired"
            error_message = (
                "维护操作已取消，环境锁已释放"
                if operation.cancel_requested
                else "执行进程失联或租约过期，环境锁已自动释放，可直接重新执行"
            )
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
                    state=target_state,
                    stage=target_stage,
                    active_environment_id=None,
                    lease_owner=None,
                    lease_until=None,
                    finished_at=now,
                    error_code=error_code,
                    error_message=error_message,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount:
                recovered += 1
                session.add(
                    WorkerMaintenanceEvent(
                        operation_id=operation.id,
                        attempt=operation.attempt,
                        type="canceled" if operation.cancel_requested else "failed",
                        stage=target_stage,
                        name="已取消" if operation.cancel_requested else "租约过期失败",
                        state=target_state,
                        message=error_message,
                        detail={"reason": error_code, "previous_stage": operation.stage},
                    )
                )
        await session.commit()
    return recovered


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
    environment_id: int | None = None,
    environment_values: dict[str, Any] | None = None,
    employee_id: str | None = None,
    employee_values: dict[str, Any] | None = None,
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
            active_environment_id=None,
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

    if environment_id is not None and environment_values:
        env_patch = dict(environment_values)
        env_patch.setdefault("updated_at", now)
        await session.execute(
            update(OpenCodeExecutionEnvironment)
            .where(OpenCodeExecutionEnvironment.id == environment_id)
            .values(**env_patch)
            .execution_options(synchronize_session=False)
        )

    if employee_id is not None and employee_values:
        emp_patch = dict(employee_values)
        emp_patch.setdefault("updated_at", now)
        await session.execute(
            update(AgentWorker)
            .where(AgentWorker.id == employee_id)
            .values(**emp_patch)
            .execution_options(synchronize_session=False)
        )

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
    """控制面进程停机时将本进程持有的 RUNNING 操作直接置为 FAILED 并释放锁。"""

    now = datetime.now(UTC)
    with CancelScope(shield=True):
        async with SessionLocal() as session:
            owned = (
                await session.scalars(
                    select(WorkerMaintenanceOperation).where(
                        WorkerMaintenanceOperation.lease_owner == owner,
                        WorkerMaintenanceOperation.state.in_(EXECUTING_STATES),
                    )
                )
            ).all()
            count = 0
            for operation in owned:
                result = await session.execute(
                    update(WorkerMaintenanceOperation)
                    .where(
                        WorkerMaintenanceOperation.id == operation.id,
                        WorkerMaintenanceOperation.attempt == operation.attempt,
                        WorkerMaintenanceOperation.lease_owner == owner,
                        WorkerMaintenanceOperation.state.in_(EXECUTING_STATES),
                    )
                    .values(
                        state=MaintenanceState.FAILED.value,
                        stage="failed",
                        active_environment_id=None,
                        lease_owner=None,
                        lease_until=None,
                        finished_at=now,
                        error_code="controller_stopped",
                        error_message=reason,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount:
                    count += 1
                    session.add(
                        WorkerMaintenanceEvent(
                            operation_id=operation.id,
                            attempt=operation.attempt,
                            type="failed",
                            stage="failed",
                            name="控制面重启中断",
                            state=MaintenanceState.FAILED.value,
                            message=reason,
                            detail={"reason": "controller_stopped"},
                        )
                    )
            await session.commit()
            return count


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

    now = datetime.now(UTC)
    lease_until = _utc(operation.lease_until)
    lease_dead = lease_until is not None and lease_until <= now

    if operation.state == MaintenanceState.QUEUED.value or lease_dead:
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
                active_environment_id=None,
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

    environment = await session.get(OpenCodeExecutionEnvironment, operation.environment_id)
    if environment is None:
        raise HTTPException(404, "Worker 环境不存在")
    _validate_action(environment, operation.action)
    await lock_environment_row(session, environment)
    await require_no_active_maintenance(session, environment.id)
    if operation.action in DISRUPTIVE_ACTIONS:
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
            active_environment_id=environment.id,
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

    async def assert_safe_to_disrupt(
        self,
        environment_id: int | None,
        employee_id: str | None,
        *,
        allow_occupied: bool = False,
    ) -> None:
        await self.check_canceled()
        if allow_occupied or not employee_id:
            return
        async with SessionLocal() as session:
            blockers = await _occupancy_blockers(session, employee_id)
        if blockers:
            raise MaintenanceBlocked("员工存在在途任务或活跃会话，已中止破坏性维护动作")

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


def _make_step_emitter(lease: OperationLease):
    async def emit(item: dict[str, Any]) -> None:
        await lease.emit(
            event_type="step",
            stage="running",
            name=str(item.get("name") or "维护步骤"),
            state=str(item.get("state") or "running"),
            message=str(item.get("message") or ""),
            detail=item.get("detail") if isinstance(item.get("detail"), dict) else {},
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


async def _execute_check(
    environment: OpenCodeExecutionEnvironment,
    lease: OperationLease,
    emit,
) -> None:
    settings = get_settings()
    await lease.check_canceled()
    now = datetime.now(UTC)

    if environment.mode == ExecutionMode.LOCAL:
        await lease.set_execution_route("local", "本机开发运行环境深度检查")
        await emit({"name": "探测本机运行环境", "state": "running", "message": ""})
        async with SessionLocal() as db:
            desired = await build_desired_spec(db, environment, settings)
        raw = await asyncio.to_thread(
            inspect_runtime,
            f"http://127.0.0.1:{environment.opencode_server_port}",
            environment.workspace_root,
            **expected_inspection_payload(desired, deep=True),
        )
        observation = normalize_probe_result(raw, None, source="local")
        detail = (
            dict(environment.status_detail)
            if isinstance(environment.status_detail, dict)
            else {}
        )
        detail["local_observation"] = observation
        environment.status_detail = detail
        environment.status = (
            EnvironmentStatus.AVAILABLE
            if (observation.get("opencode") or {}).get("status") == "healthy"
            else EnvironmentStatus.UNAVAILABLE
        )
        environment.last_checked_at = now
        evaluated = evaluate_worker_readiness(desired, environment, None)
        rows = evaluated["runtime_health"]["equipment"]
        await _emit_equipment_steps(emit, rows)
        readiness = str(evaluated["readiness"])
        env_status = EnvironmentStatus(evaluated["runtime_health"]["status"])
        blockers = evaluated.get("blocking") or []
        message = str(blockers[0]["message"]) if blockers else ""
        success = readiness == "READY"
        await lease.finish(
            MaintenanceState.SUCCEEDED.value if success else MaintenanceState.FAILED.value,
            stage="completed" if success else "failed",
            error_code=None if success else (blockers[0]["code"] if blockers else "check_failed"),
            error_message=None if success else (message or "本机环境检查未通过"),
            after_snapshot={
                "readiness": readiness,
                "probe_status": env_status.value,
                "equipment": rows,
                "repository": evaluated.get("repository"),
            },
            environment_id=environment.id,
            environment_values={
                "status": env_status,
                "status_detail": detail,
                "last_checked_at": now,
            },
            event_message="本机环境检查通过" if success else (message or "本机环境检查未通过"),
        )
        return

    async with SessionLocal() as db:
        desired = await build_desired_spec(db, environment, settings)
        instance = await db.scalar(
            select(WorkerInstance).where(WorkerInstance.environment_id == environment.id)
        )

    if instance is None:
        raise _ActionError("not_deployed", "远程环境尚未完成一键部署")
    if compute_target_digest(environment) != str(instance.target_digest or ""):
        await lease.finish(
            MaintenanceState.FAILED.value,
            stage="failed",
            error_code="target_changed",
            error_message="目标服务器或路径已变更，请重新执行一键部署",
            after_snapshot={"readiness": "DRIFTED", "probe_status": EnvironmentStatus.DRIFTED.value},
            environment_id=environment.id,
            environment_values={
                "status": EnvironmentStatus.DRIFTED,
                "last_checked_at": now,
            },
            event_message="目标服务器或路径已变更，请重新执行一键部署",
        )
        return
    if instance.status != "online":
        await lease.finish(
            MaintenanceState.FAILED.value,
            stage="failed",
            error_code="worker_offline",
            error_message="Worker 通道离线，请先执行重启唤醒或一键部署",
            after_snapshot={
                "readiness": "DEGRADED",
                "probe_status": EnvironmentStatus.UNAVAILABLE.value,
            },
            environment_id=environment.id,
            environment_values={
                "status": EnvironmentStatus.UNAVAILABLE,
                "last_checked_at": now,
            },
            event_message="Worker 通道离线，请先执行重启唤醒或一键部署",
        )
        return

    await lease.set_execution_route(
        "worker", "通过在线 Worker Gateway 执行深度诊断（含远端仓库与网关连通性）"
    )
    await emit({"name": "执行节点深度诊断", "state": "running", "message": ""})
    observation = await probe_online_worker(
        environment,
        desired,
        settings,
        worker_instance=instance,
        deep=True,
    )
    gateway = get_gateway()
    async with SessionLocal() as db:
        await gateway.update_observed_snapshot(
            db,
            worker_instance_id=instance.id,
            epoch=instance.epoch,
            snapshot=observation,
        )
        refreshed = await db.get(WorkerInstance, instance.id)
        if refreshed is not None:
            db.expunge(refreshed)

    evaluated = evaluate_worker_readiness(desired, environment, refreshed or instance)
    rows = evaluated["runtime_health"]["equipment"]
    await _emit_equipment_steps(emit, rows)
    readiness = str(evaluated["readiness"])
    env_status = EnvironmentStatus(evaluated["runtime_health"]["status"])
    blockers = evaluated.get("blocking") or []
    message = str(blockers[0]["message"]) if blockers else ""
    success = readiness == "READY"
    await lease.finish(
        MaintenanceState.SUCCEEDED.value if success else MaintenanceState.FAILED.value,
        stage="completed" if success else "failed",
        error_code=None if success else (blockers[0]["code"] if blockers else "check_failed"),
        error_message=None if success else (message or "健康检查未通过"),
        after_snapshot={
            "readiness": readiness,
            "probe_status": env_status.value,
            "equipment": rows,
            "repository": evaluated.get("repository"),
        },
        environment_id=environment.id,
        environment_values={
            "status": env_status,
            "last_checked_at": now,
        },
        event_message="深度健康检查通过" if success else (message or "深度健康检查未通过"),
    )


async def _execute_restart(
    environment: OpenCodeExecutionEnvironment,
    lease: OperationLease,
    emit,
) -> None:
    from app.runtime.remote_provisioning import worker_ctl_path

    settings = get_settings()
    await lease.assert_safe_to_disrupt(environment.id, environment.employee_id)
    async with SessionLocal() as db:
        desired = await build_desired_spec(db, environment, settings)
        instance = await db.scalar(
            select(WorkerInstance).where(WorkerInstance.environment_id == environment.id)
        )

    if instance is None:
        raise _ActionError("not_deployed", "远端环境尚未完成首次部署，请先执行一键部署")
    if compute_target_digest(environment) != str(instance.target_digest or ""):
        raise _ActionError("target_changed", "目标服务器或路径已变更，请重新执行一键部署")

    now = datetime.now(UTC)
    if instance.status == "online":
        await lease.set_execution_route("worker", "通过在线 Worker Gateway 重启受管 OpenCode")
        await emit({"name": "重启受管 OpenCode", "state": "running", "message": ""})
        client = WorkerOpenCodeClient(settings, environment_id=environment.id)
        result = await client.run_command(
            "restart_opencode",
            {
                "workspace": environment.workspace_root,
                "port": int(environment.opencode_server_port),
            },
        )
        raw_snapshot = result.get("snapshot") if isinstance(result, dict) else None
        refreshed = instance
        if isinstance(raw_snapshot, dict):
            normalized = normalize_probe_result(raw_snapshot, instance)
            gateway = get_gateway()
            async with SessionLocal() as db:
                await gateway.update_observed_snapshot(
                    db,
                    worker_instance_id=instance.id,
                    epoch=instance.epoch,
                    snapshot=normalized,
                )
                refreshed = await db.get(WorkerInstance, instance.id) or instance
                if refreshed in db:
                    db.expunge(refreshed)
        opencode_ok = bool(
            isinstance(result, dict)
            and result.get("restarted")
            and ((raw_snapshot or {}).get("opencode") or {}).get("status") == "healthy"
        )
        if not opencode_ok:
            err = (
                str(((raw_snapshot or {}).get("opencode") or {}).get("last_error") or "")
                or str(((raw_snapshot or {}).get("opencode") or {}).get("error") or "")
                or "受管 OpenCode 重启后健康探活未通过"
            )
            await emit({"name": "重启受管 OpenCode", "state": "failed", "message": err})
            raise _ActionError("restart_failed", err)
        await emit(
            {"name": "重启受管 OpenCode", "state": "passed", "message": "本地健康探活已恢复"}
        )
        evaluated = evaluate_worker_readiness(desired, environment, refreshed)
        env_status = EnvironmentStatus(evaluated["runtime_health"]["status"])
        await lease.finish(
            MaintenanceState.SUCCEEDED.value,
            stage="completed",
            after_snapshot={
                "readiness": evaluated["readiness"],
                "probe_status": env_status.value,
                "equipment": evaluated["runtime_health"]["equipment"],
            },
            environment_id=environment.id,
            environment_values={
                "status": env_status,
                "last_checked_at": now,
            },
            event_message="受管 OpenCode 重启完成",
        )
        return

    if not environment.host or not environment.username or not environment.ssh_password:
        raise _ActionError(
            "ssh_credentials_missing",
            "节点当前离线且缺少远程 SSH 凭据，请补充凭据后重试或执行一键部署",
        )

    await lease.set_execution_route("ssh", "节点离线，通过轻量 SSH 调用标准启动入口快速唤醒")
    await emit({"name": "SSH 快速唤醒守护进程", "state": "running", "message": ""})
    ctl = worker_ctl_path(environment.id)
    wake_started_at = datetime.now(UTC)
    ssh = await ExecutionEnvironmentRuntime(settings).open_session(environment)
    try:
        rc, _, _ = await asyncio.wait_for(ssh.run_command(f"test -x {shlex.quote(ctl)}"), 15)
        if rc != 0:
            await emit(
                {
                    "name": "SSH 快速唤醒守护进程",
                    "state": "failed",
                    "message": "远端标准启动入口缺失",
                }
            )
            raise _ActionError("ctl_missing", "远端尚未安装标准启动入口，请先执行一键部署")
        rc, out, err = await asyncio.wait_for(ssh.run_command(f"{shlex.quote(ctl)} start"), 30)
        if rc != 0:
            detail = (err or out or "").strip() or f"exit {rc}"
            await emit({"name": "SSH 快速唤醒守护进程", "state": "failed", "message": detail})
            raise _ActionError("wake_failed", f"远端守护唤醒失败：{detail}")
    finally:
        await ssh.close()

    await emit({"name": "SSH 快速唤醒守护进程", "state": "passed", "message": ctl})
    await emit({"name": "等待网关重连与就绪快照", "state": "running", "message": ""})

    wake_timeout = min(20.0, max(5.0, float(settings.remote_deploy_timeout_seconds)))
    deadline = asyncio.get_running_loop().time() + wake_timeout
    woken_instance: WorkerInstance | None = None
    while asyncio.get_running_loop().time() < deadline:
        await lease.check_canceled()
        async with SessionLocal() as db:
            candidate = await db.scalar(
                select(WorkerInstance).where(WorkerInstance.environment_id == environment.id)
            )
            if candidate is not None:
                db.expunge(candidate)
        if candidate is not None and candidate.status == "online":
            obs_at = _utc(candidate.observed_at)
            snap = candidate.observed_snapshot if isinstance(candidate.observed_snapshot, dict) else None
            if (
                snap is not None
                and (obs_at is None or obs_at >= wake_started_at - timedelta(seconds=1))
                and (snap.get("opencode") or {}).get("status") == "healthy"
            ):
                woken_instance = candidate
                break
        await asyncio.sleep(0.3)

    if woken_instance is None:
        async with SessionLocal() as db:
            woken_instance = await db.scalar(
                select(WorkerInstance).where(WorkerInstance.environment_id == environment.id)
            )
            if woken_instance is not None:
                db.expunge(woken_instance)
        if woken_instance is None or woken_instance.status != "online":
            await emit(
                {
                    "name": "等待网关重连与就绪快照",
                    "state": "failed",
                    "message": "守护未在超时时间内连入网关",
                }
            )
            raise _ActionError(
                "wake_timeout", "守护已触发唤醒，但未在超时时间内重新连入网关，请执行一键部署排查"
            )
        snap = (
            woken_instance.observed_snapshot
            if isinstance(woken_instance.observed_snapshot, dict)
            else {}
        )
        if (snap.get("opencode") or {}).get("status") != "healthy":
            err = (
                str((snap.get("opencode") or {}).get("last_error") or "")
                or "守护已重连，但受管 OpenCode 未通过健康探活"
            )
            await emit({"name": "等待网关重连与就绪快照", "state": "failed", "message": err})
            raise _ActionError("opencode_unhealthy", err)

    await emit({"name": "等待网关重连与就绪快照", "state": "passed", "message": "节点已恢复在线"})
    evaluated = evaluate_worker_readiness(desired, environment, woken_instance)
    env_status = EnvironmentStatus(evaluated["runtime_health"]["status"])
    await lease.finish(
        MaintenanceState.SUCCEEDED.value,
        stage="completed",
        after_snapshot={
            "readiness": evaluated["readiness"],
            "probe_status": env_status.value,
            "equipment": evaluated["runtime_health"]["equipment"],
        },
        environment_id=environment.id,
        environment_values={
            "status": env_status,
            "last_checked_at": datetime.now(UTC),
        },
        event_message="离线节点已快速唤醒并恢复连接",
    )


async def _execute_update(
    environment: OpenCodeExecutionEnvironment,
    lease: OperationLease,
    emit,
) -> None:
    settings = get_settings()
    await lease.assert_safe_to_disrupt(environment.id, environment.employee_id)
    async with SessionLocal() as db:
        desired = await build_desired_spec(db, environment, settings)
        instance = await db.scalar(
            select(WorkerInstance).where(WorkerInstance.environment_id == environment.id)
        )

    if instance is None:
        raise _ActionError("not_deployed", "远端环境尚未完成首次部署，请先执行一键部署")
    if compute_target_digest(environment) != str(instance.target_digest or ""):
        raise _ActionError("target_changed", "目标服务器或路径已变更，请重新执行一键部署")
    if instance.status != "online":
        raise _ActionError(
            "worker_offline", "Worker 当前离线，请先执行重启唤醒或一键部署后再更新代码"
        )

    repo_spec = desired.get("repository") if isinstance(desired.get("repository"), dict) else None
    if not repo_spec:
        raise _ActionError("repository_missing", "更新需要先绑定工程仓库")
    target_branch = str(repo_spec.get("target_branch") or "").strip()

    await lease.set_execution_route("worker", "通过在线 Worker Gateway 更新工作区代码")
    await emit(
        {
            "name": "更新远端工作区代码",
            "state": "running",
            "message": f"目标分支 {target_branch}",
        }
    )
    client = WorkerOpenCodeClient(settings, environment_id=environment.id)
    result = await client.run_command(
        "update_workspace",
        {
            "workspace": environment.workspace_root,
            "port": int(environment.opencode_server_port),
            "branch": target_branch,
        },
    )
    raw_snapshot = result.get("snapshot") if isinstance(result, dict) else None
    refreshed = instance
    if isinstance(raw_snapshot, dict):
        normalized = normalize_probe_result(raw_snapshot, instance)
        gateway = get_gateway()
        async with SessionLocal() as db:
            await gateway.update_observed_snapshot(
                db,
                worker_instance_id=instance.id,
                epoch=instance.epoch,
                snapshot=normalized,
            )
            refreshed = await db.get(WorkerInstance, instance.id) or instance
            if refreshed in db:
                db.expunge(refreshed)

    commit = str((result or {}).get("commit") or "")[:12] if isinstance(result, dict) else ""
    await emit(
        {
            "name": "更新远端工作区代码",
            "state": "passed",
            "message": f"{target_branch} @ {commit}" if commit else target_branch,
        }
    )
    evaluated = evaluate_worker_readiness(desired, environment, refreshed)
    env_status = EnvironmentStatus(evaluated["runtime_health"]["status"])
    await lease.finish(
        MaintenanceState.SUCCEEDED.value,
        stage="completed",
        after_snapshot={
            "readiness": evaluated["readiness"],
            "probe_status": env_status.value,
            "commit": (result or {}).get("commit") if isinstance(result, dict) else None,
            "branch": target_branch,
            "equipment": evaluated["runtime_health"]["equipment"],
        },
        environment_id=environment.id,
        environment_values={
            "status": env_status,
            "last_checked_at": datetime.now(UTC),
        },
        event_message="工作区代码已更新",
    )


async def _execute_operation(operation_id: str, owner: str) -> None:
    from app.runtime.remote_provisioning import RemoteProvisioner

    settings = get_settings()
    async with SessionLocal() as session:
        operation = await session.get(WorkerMaintenanceOperation, operation_id)
        if (
            operation is None
            or operation.lease_owner != owner
            or operation.state not in EXECUTING_STATES
        ):
            return
        environment = await session.get(OpenCodeExecutionEnvironment, operation.environment_id)
        attempt = operation.attempt
        action = operation.action
        if operation is not None:
            session.expunge(operation)
        if environment is not None:
            session.expunge(environment)

    secrets = tuple(
        secret
        for secret in (environment.ssh_password if environment is not None else None,)
        if secret
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
            environment_id=environment.id,
            environment_values={
                "last_checked_at": datetime.now(UTC),
            },
            event_message=exc.message,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("maintenance execution failed for %s", operation_id)
        message = redact_text(str(exc) or exc.__class__.__name__, secrets)
        await lease.finish(
            MaintenanceState.FAILED.value,
            stage="failed",
            error_code="execution_error",
            error_message=message,
            environment_id=environment.id,
            environment_values={
                "last_checked_at": datetime.now(UTC),
            },
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


async def scheduling_block(session, environment: OpenCodeExecutionEnvironment | None) -> str | None:
    """与列表和详情共享同一就绪判定的任务调度门禁。"""

    return await readiness_scheduling_block(session, environment)


async def environment_health(session, environment_id: int) -> dict[str, Any] | None:
    """返回单个 Worker 环境的就绪健康快照（与列表内嵌 worker_health 同源同构）。"""

    environment = await session.get(OpenCodeExecutionEnvironment, environment_id)
    if environment is None:
        return None
    employee = (
        await session.get(AgentWorker, environment.employee_id)
        if environment.employee_id
        else None
    )
    instance = await session.scalar(
        select(WorkerInstance).where(WorkerInstance.environment_id == environment.id)
    )
    active_or_latest = await active_operation(session, environment.id) or await latest_operation(
        session, environment.id
    )
    occupying_task_ids: list[str] = []
    active_session_ids: list[str] = []
    if environment.employee_id:
        occupying_task_ids = list(
            (
                await session.scalars(
                    select(Task.id).where(occupying_clause(environment.employee_id)).limit(5)
                )
            ).all()
        )
        released_task = (
            select(Task.id)
            .where(Task.id == RuntimeSession.task_id, release_match_clause())
            .exists()
        )
        active_session_ids = list(
            (
                await session.scalars(
                    select(RuntimeSession.id)
                    .where(
                        RuntimeSession.employee_id == environment.employee_id,
                        RuntimeSession.status.in_(("RUNNING", "WAITING", "UNKNOWN")),
                        ~released_task,
                    )
                    .limit(5)
                )
            ).all()
        )
    desired = await build_desired_spec(session, environment)
    return evaluate_worker_readiness(
        desired,
        environment,
        instance,
        employee_enabled=bool(employee.enabled) if employee is not None else False,
        occupying_task_ids=occupying_task_ids,
        active_session_ids=active_session_ids,
        latest_operation=active_or_latest,
    )
