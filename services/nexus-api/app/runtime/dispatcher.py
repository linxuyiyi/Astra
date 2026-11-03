from datetime import UTC, datetime

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain import EmployeeStatus, TaskStatus
from app.event_bus import event_bus
from app.models import (
    AgentWorker,
    OpenCodeExecutionEnvironment,
    Task,
    WorkerInstance,
    WorkerMaintenanceOperation,
)
from app.runtime.environment import environment_snapshot
from app.runtime.occupancy import occupying_exists, sync_employee_occupancy
from app.runtime.worker_readiness import (
    ACTIVE_MAINTENANCE_STATES,
    build_desired_spec_from_context,
    evaluate_worker_readiness,
    load_readiness_context,
    scheduling_block,
)
from app.services import TaskService
from app.worker_presentation import worker_display_name


def _active_maintenance_exists(environment_id):
    return exists(
        select(WorkerMaintenanceOperation.id).where(
            WorkerMaintenanceOperation.environment_id == environment_id,
            WorkerMaintenanceOperation.state.in_(tuple(ACTIVE_MAINTENANCE_STATES)),
        )
    )


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
                    ~_active_maintenance_exists(OpenCodeExecutionEnvironment.id)
                ),
            ]
            if task.employee_id:
                # 指定员工由人工分配，auto_claim 只约束无人认领的任务池。
                conditions.append(AgentWorker.id == task.employee_id)
            else:
                conditions.append(AgentWorker.auto_claim.is_(True))
            employees = list(
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
            environments = [
                item.execution_environment
                for item in employees
                if item.execution_environment is not None
            ]
            context = await load_readiness_context(self.session, environments)
            env_ids = [env.id for env in environments if env.id is not None]
            instances_by_env = (
                {
                    row.environment_id: row
                    for row in (
                        await self.session.scalars(
                            select(WorkerInstance).where(WorkerInstance.environment_id.in_(env_ids))
                        )
                    ).all()
                }
                if env_ids
                else {}
            )

            chosen_employee: AgentWorker | None = None
            for candidate in employees:
                candidate_env = candidate.execution_environment
                if (
                    candidate_env is None
                    or task.task_type not in candidate.task_types
                    or task.category_id not in candidate.category_ids
                ):
                    continue
                desired = build_desired_spec_from_context(candidate_env, context)
                readiness = evaluate_worker_readiness(
                    desired,
                    candidate_env,
                    instances_by_env.get(candidate_env.id) if candidate_env.id is not None else None,
                    employee_enabled=bool(candidate.enabled),
                )
                if not readiness["schedulable"]:
                    continue

                locked_employee = await self.session.scalar(
                    select(AgentWorker)
                    .options(selectinload(AgentWorker.execution_environment))
                    .where(*conditions, AgentWorker.id == candidate.id)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
                if locked_employee is None or locked_employee.execution_environment is None:
                    continue
                locked_env = locked_employee.execution_environment
                if (
                    task.task_type not in locked_employee.task_types
                    or task.category_id not in locked_employee.category_ids
                    or await scheduling_block(self.session, locked_env) is not None
                ):
                    continue
                chosen_employee = locked_employee
                break

            if chosen_employee is None:
                await self.session.rollback()
                continue

            environment = chosen_employee.execution_environment
            task.employee_id = chosen_employee.id
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
                f"任务已分配给 {worker_display_name(chosen_employee)}",
                {"auto_execute": chosen_employee.auto_execute},
                commit=False,
            )
            task.current_progress = f"已分配给 {worker_display_name(chosen_employee)}，等待启动"
            await sync_employee_occupancy(self.session, chosen_employee.id, preferred=task)
            await self.session.commit()
            await self.session.refresh(task)
            await event_bus.publish({"type": "TASK_ASSIGNED", "task_id": task.id, "status": task.status})
            return task
        await self.session.rollback()
        return None
