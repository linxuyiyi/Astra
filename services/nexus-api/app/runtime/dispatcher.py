from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain import EmployeeStatus, EnvironmentStatus, TaskStatus
from app.event_bus import event_bus
from app.models import AgentWorker, OpenCodeExecutionEnvironment, Task
from app.runtime.capability import binding_names, dispatch_allowed
from app.runtime.environment import environment_snapshot
from app.runtime.maintenance import active_maintenance_exists
from app.runtime.occupancy import occupying_exists, sync_employee_occupancy
from app.services import TaskService
from app.worker_presentation import worker_display_name


def runtime_assignment_block(environment, task) -> str | None:
    """任务必须有仓库，且和 Worker 的仓库、已应用修订一致。"""

    if not task.repository_id:
        return "任务没有绑定仓库"
    if environment.repository_id != task.repository_id:
        return "任务仓库与 Worker 不一致"
    if environment.runtime_engine == "dsh":
        if not environment.model_name:
            return "DSH 必须明确选择已登记的 Gateway 模型别名"
        if environment.worker_channel != "worker":
            return "DSH 必须使用 Worker Gateway"
        health = (environment.status_detail or {}).get("dsh", {})
        if not health.get("healthy"):
            return "请先通过 DSH 固定版本与装配检查"
        if environment.model_name not in health.get("models", []):
            return "DSH 模型未通过 Gateway 目录检查"
        if set(environment.skill_refs or []) != set(health.get("skill_hashes", {})):
            return "DSH Skill 白名单与 Worker 装配不一致"
        expected = {str(item.get("name")) for item in environment.mcp_refs or []}
        if expected != set(health.get("mcp_names", [])):
            return "DSH MCP 白名单与 Worker 装配不一致"
        return None
    names = binding_names(environment.skill_refs, environment.mcp_refs)
    if not dispatch_allowed(environment.status_detail, bindings=names):
        return "Worker 能力绑定尚未应用"
    return None


def _parent_open(status) -> bool:
    value = status.value if hasattr(status, "value") else status
    return value != TaskStatus.SUCCEEDED.value


class Dispatcher:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def claim_once(self) -> Task | None:
        # Recover a committed assignment if a worker exited before starting its Session.
        assigned = await self.session.scalar(
            select(Task)
            .join(AgentWorker, AgentWorker.id == Task.employee_id)
            .where(
                Task.status == TaskStatus.PENDING,
                Task.claimed_at.is_not(None),
                Task.current_session_id.is_(None),
                Task.pending_message_id.is_(None),
                Task.runtime_error.is_(None),
                AgentWorker.auto_execute.is_(True),
                AgentWorker.enabled.is_(True),
            )
            .order_by(Task.claimed_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if assigned:
            return assigned
        # Discover without locking the whole queue. Each candidate is rechecked
        # with a current locking read in its own short transaction below.
        candidate_ids = (
            await self.session.scalars(
                select(Task.id)
                .where(Task.status == TaskStatus.PENDING, Task.claimed_at.is_(None))
                .order_by(Task.priority_weight.desc(), Task.created_at.asc())
            )
        ).all()
        await self.session.commit()
        for task_id in candidate_ids:
            task = await self.session.scalar(
                select(Task)
                .where(Task.id == task_id, Task.status == TaskStatus.PENDING, Task.claimed_at.is_(None))
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if task is None:
                await self.session.rollback()
                continue
            if not task.repository_id:
                await self.session.rollback()
                continue
            if task.related_task_id:
                parent = await self.session.get(Task, task.related_task_id)
                parent_status = None
                if parent is not None:
                    parent_status = parent.status.value if hasattr(parent.status, "value") else parent.status
                if parent_status == TaskStatus.CANCELED.value:
                    task.current_progress = "父任务已取消，阻塞中"
                    await self.session.commit()
                    continue
                if parent is None or _parent_open(parent.status):
                    await self.session.rollback()
                    continue
            conditions = [
                AgentWorker.status == EmployeeStatus.IDLE,
                AgentWorker.enabled.is_(True),
                ~occupying_exists(AgentWorker.id),
                AgentWorker.execution_environment.has(
                    ~active_maintenance_exists(OpenCodeExecutionEnvironment.id)
                ),
            ]
            if task.employee_id:
                # 指定员工由人工分配，auto_claim 只约束无人认领的任务池。
                conditions.append(AgentWorker.id == task.employee_id)
            else:
                conditions.append(AgentWorker.auto_claim.is_(True))
            employees = (
                (
                    await self.session.execute(
                        select(AgentWorker)
                        .options(selectinload(AgentWorker.execution_environment))
                        .where(*conditions)
                        .order_by(AgentWorker.id)
                    )
                )
                .scalars()
                .all()
            )
            employee = None
            for candidate in employees:
                if (
                    not candidate.execution_environment
                    or candidate.execution_environment.status != EnvironmentStatus.AVAILABLE
                    or runtime_assignment_block(candidate.execution_environment, task) is not None
                    or task.task_type not in candidate.task_types
                    or task.category_id not in candidate.category_ids
                ):
                    continue
                employee = await self.session.scalar(
                    select(AgentWorker)
                    .options(selectinload(AgentWorker.execution_environment))
                    .where(*conditions, AgentWorker.id == candidate.id)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
                if employee is not None:
                    break
            if not employee:
                await self.session.rollback()
                continue
            environment = employee.execution_environment
            from fastapi import HTTPException

            from app.runtime.remote_provisioning import guard_environment_write

            try:
                await guard_environment_write(self.session, environment)
                from app.runtime.maintenance import scheduling_block

                blocked = await scheduling_block(self.session, environment)
                if (
                    blocked
                    or runtime_assignment_block(environment, task) is not None
                    or task.task_type not in employee.task_types
                    or task.category_id not in employee.category_ids
                ):
                    await self.session.rollback()
                    return None
            except HTTPException:
                await self.session.rollback()
                return None
            task.employee_id = employee.id
            # Actual HEAD belongs to the prepared execution workspace; start()
            # already reads it there, outside this assignment transaction.
            task.base_commit = None
            task.execution_environment_snapshot = environment_snapshot(environment)
            task.claimed_at = datetime.now(UTC)
            service = TaskService(self.session)
            await service.add_event(
                task,
                "TASK_ASSIGNED",
                "work",
                f"任务已分配给 {worker_display_name(employee)}",
                {"auto_execute": employee.auto_execute},
                commit=False,
            )
            task.current_progress = f"已分配给 {worker_display_name(employee)}，等待启动"
            await sync_employee_occupancy(self.session, employee.id, preferred=task)
            await self.session.commit()
            await self.session.refresh(task)
            await event_bus.publish({"type": "TASK_ASSIGNED", "task_id": task.id, "status": task.status})
            return task
        await self.session.rollback()
        return None
