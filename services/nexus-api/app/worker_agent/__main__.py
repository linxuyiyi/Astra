"""python -m app.worker_agent

凭据只从环境变量读取，不写进仓库。
"""

from __future__ import annotations

import asyncio
import os
import signal

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

    driver = None
    if config_path := os.environ.get("NEXUS_DSH_CONFIG"):
        from pathlib import Path

        from app.worker_agent.dsh_acp.driver import DriverConfig, DshAcpDriver

        driver = DshAcpDriver(
            DriverConfig.load(Path(config_path), config.workspace_root), config.worker_instance_id,
        )
        manager.runtime_driver = driver
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous_handlers = {}
    if os.name == "posix":
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            loop.add_signal_handler(signum, stop.set)
    running = asyncio.create_task(manager.run(connect, stop))
    stopping = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({running, stopping}, return_when=asyncio.FIRST_COMPLETED)
        if running.done():
            await running
    finally:
        stop.set()
        running.cancel()
        stopping.cancel()
        await asyncio.gather(running, stopping, return_exceptions=True)
        try:
            if driver:
                await driver.dispose()
        finally:
            for signum, previous in previous_handlers.items():
                loop.remove_signal_handler(signum)
                signal.signal(signum, previous)


if __name__ == "__main__":
    asyncio.run(_main())
