"""独立 OpenCode 进程监管；SSH 连接不参与进程生命周期。"""

import argparse
import asyncio
import os
import signal
from pathlib import Path
from urllib.parse import urlsplit

from app.worker_tools.files import sweep_partials
from app.worker_tools.supervisor import OpenCodeSupervisor, acquire_daemon_lock, cleanup_orphan_opencode


def load_env_file(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value
    return values


async def _main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    args = parser.parse_args(argv)
    os.environ.update(load_env_file(args.env_file))
    lock = acquire_daemon_lock(Path(os.environ["NEXUS_WORKER_STATE_DIR"]))
    if lock is None:
        return 0
    supervisor = OpenCodeSupervisor(
        enabled=True,
        opencode_bin=os.environ["NEXUS_WORKER_OPENCODE_BIN"],
        port=urlsplit(os.environ["NEXUS_WORKER_OPENCODE_URL"]).port,
        workspace_root=os.environ["NEXUS_WORKER_WORKSPACE_ROOT"],
        agent_worker_id=os.environ["NEXUS_AGENT_WORKER_ID"],
        environ=dict(os.environ),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        cleanup_orphan_opencode(os.environ["NEXUS_AGENT_WORKER_ID"])
        sweep_partials(os.environ["NEXUS_WORKER_WORKSPACE_ROOT"])
        await supervisor.start()
        await stop.wait()
    finally:
        await supervisor.stop()
        lock.close()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
