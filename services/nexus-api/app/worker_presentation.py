"""Agent Worker 的名称展示，不使用关联数字员工的名称。"""

from app.models import AgentWorker


def worker_display_name(worker: AgentWorker) -> str:
    """调用方须预加载 execution_environment，避免展示时隐式查询。"""
    environment = worker.execution_environment
    name = environment.name.strip() if environment and environment.name else ""
    return name or f"Worker · {worker.id}"
