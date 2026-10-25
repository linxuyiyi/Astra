"""Task status metadata shared verbatim with the frontend."""
import json
from pathlib import Path

TASK_STATUS = json.loads(Path(__file__).with_name("task_status.json").read_text(encoding="utf-8"))
EMPLOYEE_LABELS = {
    "WORKING": "工作中", "WAIT_HUMAN": "等待人工", "IDLE": "空闲", "OFFLINE": "离线",
    "PAUSED": "已暂停", "ERROR": "异常",
}


def project_channel_health(
    *,
    mode: str | None,
    worker_channel: str | None,
    environment_status: str | None,
    worker_status: str | None,
    repository_id: str | None,
    employee_status: str | None,
    task_statuses: list[str],
    runtime_connections: list[str],
    runtime_engine: str = "opencode",
) -> dict[str, bool | str]:
    """Split Worker, OpenCode, repository and human-wait facts.

    An offline Worker must not be presented as an available environment.
    This reads stored facts and does not change Task status.
    """

    remote_worker = mode == "LINUX_REMOTE" and worker_channel == "worker"
    worker_online = worker_status == "online"
    if remote_worker and not worker_online:
        availability = "offline"
        opencode_reachable = False
    else:
        availability = environment_status or "UNKNOWN"
        opencode_reachable = availability == "AVAILABLE"
    if "UNAVAILABLE" in runtime_connections and availability == "AVAILABLE":
        availability = "UNAVAILABLE"
        opencode_reachable = False
    return {
        "worker_online": worker_online if remote_worker else True,
        "runtime_engine": runtime_engine,
        "runtime_reachable": opencode_reachable,
        "opencode_reachable": opencode_reachable if runtime_engine == "opencode" else False,
        "repository_bound": bool(repository_id),
        "executing": employee_status == "WORKING" or "RUNNING" in task_statuses,
        "waiting_human": employee_status == "WAIT_HUMAN" or "WAITING" in task_statuses,
        "availability": availability,
    }


class RuntimeProjection:
    @property
    def runtime_state(self):
        return self.status

    @property
    def runtime_status_label(self):
        if self.__tablename__ == "t_nexus_tasks":
            return TASK_STATUS[self.status]["label"]
        return EMPLOYEE_LABELS.get(self.status, self.status)

    @property
    def runtime_detail(self):
        return getattr(self, "runtime_error", None) or getattr(self, "current_progress", "") or ""

    @property
    def failure(self):
        error = getattr(self, "failure_reason", None)
        return {"raw_error": error} if error else None
