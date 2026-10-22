"""任务接单资格：类型与数字员工关系只在这里定义。"""

from __future__ import annotations

from app.models import AgentWorker


def worker_assignment_block(
    worker: AgentWorker, *, task_type: str, responsible_employee_id: str | None
) -> str | None:
    if not worker.enabled:
        return "指定 Worker 未启用"
    if task_type not in (worker.task_types or []):
        return "指定 Worker 不支持该任务类型"
    owner = (responsible_employee_id or "").strip()
    bound = (worker.digital_employee_id or "").strip()
    if owner and owner != bound:
        return "指定 Worker 不属于该责任数字员工"
    return None
