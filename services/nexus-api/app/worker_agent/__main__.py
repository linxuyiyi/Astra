"""python -m app.worker_agent [--env-file /path/to/worker.env]

凭据只从环境变量或受管 worker.env 文件读取，不写进仓库。
单实例守护由 worker.lock（flock LOCK_EX | LOCK_NB + FD_CLOEXEC）排他保证，不依赖 systemd。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
from pathlib import Path
from typing import IO
from urllib.parse import urlsplit

from app.worker_agent.connection import WorkerConnectionManager, load_agent_config
from app.worker_agent.files import sweep_partials
from app.worker_agent.local_opencode import bind_local_opencode
from app.worker_agent.supervisor import (
    OpenCodeSupervisor,
    acquire_daemon_lock,
    cleanup_orphan_opencode,
)
from app.worker_agent.transport import connect_websocket


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = raw_line.partition("=")
        if not sep:
            continue
        name = key.strip()
        if name:
            values[name] = value
    return values


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="nexus-worker-agent")
    parser.add_argument("--env-file", default="", help="受管 worker.env 文件路径")
    return parser.parse_args(argv)


def control_timeout_seconds() -> float:
    raw = os.environ.get("NEXUS_OPENCODE_CONTROL_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return 60.0
    try:
        return float(raw)
    except ValueError:
        return 60.0


def _opencode_port(url: str) -> int:
    try:
        parsed = urlsplit(url)
        if parsed.port:
            return int(parsed.port)
    except ValueError:
        pass
    return 14096


async def _main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    env_file = (args.env_file or os.environ.get("NEXUS_WORKER_ENV_FILE") or "").strip()
    if env_file:
        loaded = load_env_file(Path(env_file))
        os.environ.update(loaded)

    config = load_agent_config(dict(os.environ))
    state_dir_raw = (os.environ.get("NEXUS_WORKER_STATE_DIR") or "").strip()
    lock_handle: IO[bytes] | None = None
    if state_dir_raw:
        lock_handle = acquire_daemon_lock(Path(state_dir_raw))
        if lock_handle is None:
            # 同一环境守护已存活并持有排他锁，重复启动幂等成功退出。
            return 0

    try:
        cleanup_orphan_opencode(config.environment_id)
        if config.workspace_root:
            sweep_partials(config.workspace_root)

        opencode_bin = (os.environ.get("NEXUS_WORKER_OPENCODE_BIN") or "").strip()
        supervise_enabled = bool(opencode_bin)
        supervisor = OpenCodeSupervisor(
            enabled=supervise_enabled,
            opencode_bin=opencode_bin or "opencode",
            port=_opencode_port(config.opencode_url),
            workspace_root=config.workspace_root,
            environment_id=config.environment_id,
            environ=dict(os.environ),
        )
        manager = WorkerConnectionManager(config, supervisor=supervisor)
        bind_local_opencode(manager, timeout=control_timeout_seconds())
        await supervisor.start()

        async def connect():
            return await connect_websocket(config.gateway_url, max_size=config.max_frame_bytes)

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
            await supervisor.stop()
            for signum, previous in previous_handlers.items():
                loop.remove_signal_handler(signum)
                signal.signal(signum, previous)
    finally:
        if lock_handle is not None:
            try:
                lock_handle.close()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
