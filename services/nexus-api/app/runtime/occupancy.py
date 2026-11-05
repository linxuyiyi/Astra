"""Task 是 Worker 独占资源的唯一事实源。取消只有停止确认后才落终态。"""

from sqlalchemy import and_, exists, select

from app.models import AgentWorker, Task


def task_occupies(task: Task) -> bool:
    return bool(task.agent_worker_id and task.status in {"RUNNING", "WAITING"})


def occupying_clause(agent_worker_id):
    return and_(Task.agent_worker_id == agent_worker_id, Task.status.in_(("RUNNING", "WAITING")))


def occupying_exists(agent_worker_id):
    return exists(select(Task.id).where(occupying_clause(agent_worker_id)))


async def sync_worker_occupancy(session, agent_worker_id, *, preferred=None):
    if not agent_worker_id:
        return None
    worker = await session.get(AgentWorker, agent_worker_id)
    if worker and worker.status not in {"PAUSED", "OFFLINE"}:
        occupied = await session.scalar(select(Task.id).where(occupying_clause(agent_worker_id)).limit(1))
        worker.status = "WORKING" if occupied else "IDLE"
    return worker
