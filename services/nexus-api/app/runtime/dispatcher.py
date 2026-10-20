from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain import EmployeeStatus, EnvironmentStatus, TaskStatus
from app.event_bus import event_bus
from app.models import AgentWorker, Task, WorkerMaintenanceOperation
from app.runtime.capability import binding_names, dispatch_allowed
from app.runtime.environment import environment_snapshot
from app.runtime.maintenance import ACTIVE_STATES
from app.runtime.occupancy import occupying_exists, sync_employee_occupancy
from app.runtime.repository import read_base_commit
from app.runtime.worker_files import environment_uses_worker
from app.runtime.workspace_changes import workspace_head_commit
from app.services import TaskService


def runtime_assignment_block(environment, task) -> str | None:
    """任务必须有仓库，且和 Worker 的仓库、已应用修订一致。"""

    if not task.repository_id:
        return "任务没有绑定仓库"
    if environment.repository_id != task.repository_id:
        return "任务仓库与 Worker 不一致"
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
        maintained_environment_ids = set(
            (
                await self.session.scalars(
                    select(WorkerMaintenanceOperation.environment_id).where(
                        WorkerMaintenanceOperation.state.in_(ACTIVE_STATES)
                    )
                )
            ).all()
        )
        candidates = (
            (
                await self.session.execute(
                    select(Task)
                    .where(Task.status == TaskStatus.PENDING, Task.claimed_at.is_(None))
                    .order_by(Task.priority_weight.desc(), Task.created_at.asc())
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for task in candidates:
            if not task.repository_id:
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
                    continue
            conditions = [
                AgentWorker.status == EmployeeStatus.IDLE,
                AgentWorker.enabled.is_(True),
                ~occupying_exists(AgentWorker.id),
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
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            compatible = [
                employee
                for employee in employees
                if employee.execution_environment
                and employee.execution_environment.id not in maintained_environment_ids
                and employee.execution_environment.status == EnvironmentStatus.AVAILABLE
                and runtime_assignment_block(employee.execution_environment, task) is None
                and task.task_type in employee.task_types
                and task.category_id in employee.category_ids
            ]
            employee = compatible[0] if compatible else None
            if not employee:
                continue
            environment = employee.execution_environment
            # Read the workspace before taking the shared maintenance lock so a
            # remote Worker call never runs inside the environment-row lock.
            snapshot = environment_snapshot(environment)
            if await environment_uses_worker(environment):
                head = await workspace_head_commit(environment, environment.workspace_root)
                snapshot["base_commit"] = head or ""
            else:
                snapshot["base_commit"] = read_base_commit(environment.workspace_root)
            from fastapi import HTTPException

            from app.runtime.remote_provisioning import guard_environment_write

            try:
                # The maintained-id query above is only a prefilter. Recheck the
                # canonical maintenance fact under the shared environment lock
                # immediately before assignment, then commit the assignment and
                # release the row lock.
                await guard_environment_write(self.session, environment)
                from app.runtime.maintenance import scheduling_block

                blocked = await scheduling_block(self.session, environment)
                if blocked:
                    await self.session.rollback()
                    return None
            except HTTPException:
                await self.session.rollback()
                return None
            task.employee_id = employee.id
            task.base_commit = snapshot["base_commit"] or None
            task.execution_environment_snapshot = snapshot
            task.claimed_at = datetime.now(UTC)
            service = TaskService(self.session)
            await service.add_event(
                task,
                "TASK_ASSIGNED",
                "work",
                f"任务已分配给 {employee.name}",
                {"auto_execute": employee.auto_execute},
                commit=False,
            )
            task.current_progress = f"已分配给 {employee.name}，等待启动"
            await sync_employee_occupancy(self.session, employee.id, preferred=task)
            await self.session.commit()
            await self.session.refresh(task)
            await event_bus.publish({"type": "TASK_ASSIGNED", "task_id": task.id, "status": task.status})
            return task
        await self.session.rollback()
        return None
