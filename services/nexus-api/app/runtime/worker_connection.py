"""连接事实与 Worker 的调度和运行状态独立。"""

from app.config.settings import get_settings
from app.domain import ExecutionMode


async def worker_connections(workers, *, bus=None) -> dict[str, bool]:
    remote_ids = [worker.id for worker in workers if worker.mode != ExecutionMode.LOCAL]
    owners = {}
    if remote_ids:
        if bus is None:
            from app.worker_gateway.service import get_gateway

            bus = get_gateway().bus
        owners = await bus.get_owners(remote_ids)
    local_enabled = get_settings().executor_type == "opencode_server"
    return {
        worker.id: (
            local_enabled and worker.status != "OFFLINE"
            if worker.mode == ExecutionMode.LOCAL
            else (owners.get(worker.id) or (None, None))[1] == worker.epoch
        )
        for worker in workers
    }
