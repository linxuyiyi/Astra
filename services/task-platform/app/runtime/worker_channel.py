"""Maintenance-gated switch to the Worker channel. Snapshots never carry it."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import (
    OpenCodeExecutionEnvironment,
    WorkerInstance,
    WorkerMaintenanceOperation,
)
from app.runtime.maintenance import (
    EXECUTING_STATES,
    MaintenanceCanceled,
    MaintenanceLeaseLost,
    MaintenanceState,
    OperationLease,
    _finish_in_session,
    active_maintenance_exists,
    controller_id,
    lock_environment_row,
)
from app.runtime.occupancy import blocking_session_exists, occupying_exists

Switch = Callable[[], Awaitable[dict[str, Any]]]


async def read_worker_channel(session: AsyncSession, environment_id: int | None) -> str:
    if environment_id is None:
        return "ssh"
    environment = await session.get(OpenCodeExecutionEnvironment, environment_id)
    if environment is None:
        return "ssh"
    return environment.worker_channel if environment.worker_channel in {"ssh", "worker"} else "ssh"


def _owned_operation(operation: WorkerMaintenanceOperation, owner: str, now: datetime):
    return exists(
        select(WorkerMaintenanceOperation.id).where(
            WorkerMaintenanceOperation.id == operation.id,
            WorkerMaintenanceOperation.attempt == operation.attempt,
            WorkerMaintenanceOperation.lease_owner == owner,
            WorkerMaintenanceOperation.state.in_(EXECUTING_STATES),
            WorkerMaintenanceOperation.lease_until.is_not(None),
            WorkerMaintenanceOperation.lease_until > now,
        )
    )


async def _reserve_channel_operation(
    session: AsyncSession, environment: OpenCodeExecutionEnvironment
) -> tuple[WorkerMaintenanceOperation, str]:
    settings = get_settings()
    now = datetime.now(UTC)
    await lock_environment_row(session, environment)
    if await session.scalar(select(1).where(active_maintenance_exists(environment.id))):
        await session.rollback()
        raise HTTPException(409, "环境正在维护，不能切换通道")
    if await session.scalar(select(1).where(occupying_exists(environment.employee_id))):
        await session.rollback()
        raise HTTPException(409, "员工存在占用任务，不能切换通道")
    if await session.scalar(select(1).where(blocking_session_exists(environment.employee_id))):
        await session.rollback()
        raise HTTPException(409, "员工存在未结束 Session，不能切换通道")
    owner = controller_id()
    operation = WorkerMaintenanceOperation(
        id="op_" + uuid4().hex,
        environment_id=environment.id,
        action="switch_channel",
        state=MaintenanceState.RUNNING.value,
        stage="channel",
        requested_by="worker-channel",
        attempt=1,
        lease_owner=owner,
        lease_until=now + timedelta(seconds=settings.worker_maintenance_lease_seconds),
        target_revision={},
        before_snapshot={"worker_channel": environment.worker_channel},
        after_snapshot={},
        active_environment_id=environment.id,
        requested_at=now,
        started_at=now,
    )
    session.add(operation)
    await session.commit()
    await session.refresh(operation)
    return operation, owner


async def mark_worker_channel(
    session: AsyncSession,
    environment_id: int,
    *,
    switch: Switch | None = None,
) -> OpenCodeExecutionEnvironment:
    """Switch to the Worker channel while holding the shared maintenance lock."""

    environment = await session.get(
        OpenCodeExecutionEnvironment, environment_id, populate_existing=True
    )
    if environment is None:
        raise HTTPException(404, "环境不存在")
    await session.refresh(environment)
    operation, owner = await _reserve_channel_operation(session, environment)
    kept_status = environment.status
    workspace = environment.workspace_root
    port = environment.opencode_server_port
    settings = get_settings()
    lease = OperationLease(
        operation.id,
        operation.attempt,
        owner,
        settings.worker_maintenance_lease_seconds,
    )
    renewal_stopped = asyncio.Event()
    lease_lost = asyncio.Event()

    async def renew_loop(target: asyncio.Task) -> None:
        while not renewal_stopped.is_set():
            await asyncio.sleep(max(0.05, settings.worker_maintenance_lease_seconds / 3))
            if renewal_stopped.is_set():
                return
            try:
                renewed = await lease.renew()
            except Exception:
                renewed = False
            if not renewed:
                lease_lost.set()
                target.cancel()
                return

    try:
        if switch is None:
            if not await _worker_online(environment_id):
                await lease.finish(
                    MaintenanceState.FAILED.value,
                    stage="channel",
                    error_code="worker_offline",
                    error_message="Worker 不在线，通道保持 ssh",
                )
                raise HTTPException(409, "Worker 不在线，通道保持 ssh")
            operation_fn: Switch = _worker_switch(environment_id, workspace, port)
        else:
            operation_fn = switch
        await lease.mark_danger()
        switch_task = asyncio.create_task(operation_fn())
        renew_task = asyncio.create_task(renew_loop(switch_task))
        try:
            result = await switch_task
        finally:
            renewal_stopped.set()
            renew_task.cancel()
            await asyncio.gather(renew_task, return_exceptions=True)
        if lease_lost.is_set():
            raise MaintenanceLeaseLost("切换期间维护租约失效")
        mismatch = _mismatch(result)
        if mismatch:
            raise HTTPException(409, mismatch)
        now = datetime.now(UTC)
        written = await session.execute(
            update(OpenCodeExecutionEnvironment)
            .where(
                OpenCodeExecutionEnvironment.id == environment_id,
                ~occupying_exists(environment.employee_id),
                ~blocking_session_exists(environment.employee_id),
                _owned_operation(operation, owner, now),
            )
            .values(worker_channel="worker", status=kept_status)
            .execution_options(synchronize_session=False)
        )
        if written.rowcount != 1:
            await session.rollback()
            raise MaintenanceLeaseLost("切换期间员工被占用、维护租约失效或环境已变化")
        applied = await _finish_in_session(
            session,
            operation.id,
            owner,
            operation.attempt,
            MaintenanceState.SUCCEEDED.value,
            stage="channel",
            error_code=None,
            error_message=None,
            after_snapshot={"worker_channel": "worker"},
            release_active=True,
            environment_id=None,
            environment_values=None,
            event_message=None,
            secrets=(),
        )
        if not applied:
            await session.rollback()
            raise MaintenanceLeaseLost("切换完成但维护终态写入被拒绝")
        await session.commit()
    except asyncio.CancelledError:
        await _keep_interrupted(lease, "通道切换被取消；远端可能已重载")
        raise
    except MaintenanceCanceled:
        await _keep_interrupted(lease, "通道切换已请求取消")
        raise
    except MaintenanceLeaseLost as exc:
        await _keep_interrupted(lease, str(exc))
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        await _keep_interrupted(lease, f"通道切换失败或超时：{exc}")
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(409, f"切换失败，通道保持 ssh：{exc}") from exc
    stored = await session.get(
        OpenCodeExecutionEnvironment, environment_id, populate_existing=True
    )
    if stored is None:
        raise HTTPException(404, "环境不存在")
    await session.refresh(stored)
    return stored


async def _keep_interrupted(lease: OperationLease, reason: str) -> None:
    try:
        await lease.finish(
            MaintenanceState.INTERRUPTED.value,
            stage="interrupted",
            error_code="channel_switch_interrupted",
            error_message=reason,
        )
    except Exception:
        # An expired/fenced lease is recovered by the maintenance executor.
        return


async def complete_worker_mark(
    session: AsyncSession,
    environment_id: int,
    operation_id: str,
    *,
    lease: OperationLease | None = None,
    before_write: Callable[[], Awaitable[None]] | None = None,
) -> OpenCodeExecutionEnvironment:
    """Probe and mark inside the maintenance operation the caller already holds."""

    environment = await session.get(
        OpenCodeExecutionEnvironment, environment_id, populate_existing=True
    )
    if environment is None:
        raise HTTPException(404, "环境不存在")
    await session.refresh(environment)
    operation = await session.get(
        WorkerMaintenanceOperation, operation_id, populate_existing=True
    )
    if operation is None or operation.state not in EXECUTING_STATES:
        raise HTTPException(409, "维护操作不是进行中，不能标记通道")
    if lease is not None:
        if operation.attempt != lease.attempt or operation.lease_owner != lease.owner:
            raise HTTPException(409, "维护租约已失效，通道保持未安装")
        await lease.check_canceled()
        await lease.assert_safe_to_disrupt(environment.id, environment.employee_id)
    workspace = environment.workspace_root
    port = environment.opencode_server_port
    await session.commit()
    try:
        result = await _worker_switch(environment_id, workspace, port)()
    except Exception as exc:
        raise HTTPException(409, f"切换失败，通道保持未安装：{exc}") from exc
    mismatch = _mismatch(result)
    if mismatch:
        raise HTTPException(409, mismatch)
    if before_write is not None:
        await before_write()
    now = datetime.now(UTC)
    owner = lease.owner if lease is not None else operation.lease_owner
    attempt = lease.attempt if lease is not None else operation.attempt
    owned = exists(
        select(WorkerMaintenanceOperation.id).where(
            WorkerMaintenanceOperation.id == operation_id,
            WorkerMaintenanceOperation.attempt == attempt,
            WorkerMaintenanceOperation.lease_owner == owner,
            WorkerMaintenanceOperation.state.in_(EXECUTING_STATES),
            WorkerMaintenanceOperation.lease_until.is_not(None),
            WorkerMaintenanceOperation.lease_until > now,
        )
    )
    written = await session.execute(
        update(OpenCodeExecutionEnvironment)
        .where(
            OpenCodeExecutionEnvironment.id == environment_id,
            ~occupying_exists(environment.employee_id),
            ~blocking_session_exists(environment.employee_id),
            owned,
        )
        .values(worker_channel="worker")
        .execution_options(synchronize_session=False)
    )
    if written.rowcount != 1:
        await session.rollback()
        raise HTTPException(409, "切换期间员工被占用、维护租约失效或环境已变化")
    await session.commit()
    stored = await session.get(
        OpenCodeExecutionEnvironment, environment_id, populate_existing=True
    )
    if stored is None:
        raise HTTPException(404, "环境不存在")
    await session.refresh(stored)
    return stored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把执行环境切换到 Worker 通道")
    parser.add_argument("--environment-id", type=int, required=True)
    args = parser.parse_args(argv)

    async def run() -> int:
        async with SessionLocal() as session:
            try:
                environment = await mark_worker_channel(session, args.environment_id)
            except HTTPException as exc:
                print(exc.detail)
                return 1
        print(environment.worker_channel)
        return 0

    return asyncio.run(run())


def _worker_switch(environment_id: int, workspace: str, port: int) -> Switch:
    async def switch() -> dict[str, Any]:
        from app.runtime.worker_client import WorkerOpenCodeClient

        client = WorkerOpenCodeClient(get_settings(), environment_id=environment_id)
        return await client.run_command("switch_channel", {"workspace": workspace, "port": port})

    return switch


async def _worker_online(environment_id: int) -> bool:
    async with SessionLocal() as session:
        instance = await session.scalar(
            select(WorkerInstance).where(WorkerInstance.environment_id == environment_id)
        )
        return instance is not None and instance.status == "online"


def _mismatch(result: object) -> str | None:
    if not isinstance(result, dict) or result.get("reloaded") is not True:
        return "受控重载没有完成，通道保持 ssh"
    settings = get_settings()
    if not _same_direct_url(result.get("gateway_url"), settings.remote_gateway_local_url):
        return "生效的 Gateway 地址与内网配置不一致，通道保持 ssh"
    if not _same_direct_url(result.get("mcp_url"), settings.mcp_resource_server_url):
        return "生效的 MCP 地址与内网配置不一致，通道保持 ssh"
    return None


def _same_direct_url(actual: object, expected: str) -> bool:
    return isinstance(actual, str) and actual.rstrip("/") == expected.rstrip("/")


if __name__ == "__main__":
    raise SystemExit(main())
