"""python -m app.worker_agent

凭据只从环境变量读取，不写进仓库。
"""

from __future__ import annotations

import asyncio
import os

from app.worker_agent.connection import WorkerConnectionManager, load_agent_config
from app.worker_agent.files import sweep_partials
from app.worker_agent.local_opencode import bind_local_opencode
from app.worker_agent.supervisor import RuntimeSupervisor
from app.worker_agent.transport import connect_websocket


def control_timeout_seconds() -> float:
    raw = os.environ.get("NEXUS_OPENCODE_CONTROL_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return 60.0
    try:
        return float(raw)
    except ValueError:
        return 60.0


async def _main() -> None:
    config = load_agent_config(dict(os.environ))
    if config.workspace_root:
        sweep_partials(config.workspace_root)
    manager = WorkerConnectionManager(config, supervisor=RuntimeSupervisor())
    bind_local_opencode(manager, timeout=control_timeout_seconds())

    async def connect():
        return await connect_websocket(config.gateway_url, max_size=config.max_frame_bytes)

    await manager.run(connect, asyncio.Event())


if __name__ == "__main__":
    asyncio.run(_main())
