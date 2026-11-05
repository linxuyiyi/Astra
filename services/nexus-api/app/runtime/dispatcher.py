"""锁定 Task 和 Worker 后领取，WAITING 和离线任务继续占用。"""

from datetime import UTC, datetime

from sqlalchemy import case, exists, select

from app.models import AgentWorker, Task, WorkerMaintenanceOperation
from app.runtime.occupancy import occupying_exists
from app.services import TaskService


class Dispatcher:
    def __init__(self, session):
        self.session = session

    async def claim_once(self):
        assigned = await self.session.scalar(
            select(Task)
            .join(AgentWorker, AgentWorker.id == Task.agent_worker_id)
            .where(
                AgentWorker.status == "WORKING",
                Task.status == "RUNNING",
                Task.current_session_id.is_(None),
                Task.pending_message_id.is_(None),
            )
            .order_by(Task.claimed_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if assigned:
            await self.session.commit()
            return assigned
        priorities = case({"VERY_HIGH": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}, value=Task.priority)
        ids = (
            await self.session.scalars(
                select(Task.id).where(Task.status == "PENDING").order_by(priorities.desc(), Task.created_at)
            )
        ).all()
        await self.session.commit()
        for task_id in ids:
            task = await self.session.scalar(
                select(Task)
                .where(Task.id == task_id, Task.status == "PENDING")
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if not task:
                await self.session.rollback()
                continue
            maintenance = exists(
                select(WorkerMaintenanceOperation.id).where(
                    WorkerMaintenanceOperation.active_worker_id == AgentWorker.id
                )
            )
            workers = (
                await self.session.scalars(
                    select(AgentWorker)
                    .where(
                        AgentWorker.enabled.is_(True),
                        AgentWorker.status == "IDLE",
                        ~occupying_exists(AgentWorker.id),
                        ~maintenance,
                        *([AgentWorker.id == task.agent_worker_id] if task.agent_worker_id else []),
                    )
                    .order_by(AgentWorker.id)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            worker = next(
                (w for w in workers if task.task_type in w.task_types and task.category_id in w.category_ids),
                None,
            )
            if worker:
                task.agent_worker_id = worker.id
                task.claimed_at = datetime.now(UTC)
                worker.status = "WORKING"
                await TaskService(self.session).transition(
                    task, "RUNNING", current_progress="已领取，准备执行", commit=False
                )
                await self.session.commit()
                return task
            await self.session.rollback()
        return None
