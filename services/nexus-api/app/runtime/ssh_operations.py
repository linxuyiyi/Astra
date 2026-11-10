"""部署的本地辅助通过复用 SSH exec 调用，无网络命令总线。"""

import asyncio
import json
import shlex

from app.runtime.worker_connection import connections


class WorkerOperationError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


async def run_worker_operation(worker, name, data, *, content=None):
    from app.runtime.remote_provisioning import WORKER_SECRET_DIR, agent_python

    connection = await connections.get(worker)
    command = shlex.join(
        [
            agent_python(worker.id),
            "-m",
            "app.worker_tools.operations",
            f"{WORKER_SECRET_DIR}/worker-{worker.id}.env",
            name,
        ]
    )
    payload = json.dumps({**data, "upload": content is not None}).encode() + b"\n"
    code, out, err = await asyncio.wait_for(
        connection.ssh.run_command(command, stdin=payload + (content or b"")), 90
    )
    if code:
        try:
            result = json.loads(out)
        except ValueError:
            raise WorkerOperationError("ssh_operation_failed", err[:1000]) from None
        raise WorkerOperationError(result["error_code"], result["error"])
    return json.loads(out)
