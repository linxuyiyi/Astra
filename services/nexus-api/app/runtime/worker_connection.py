"""按 Worker 复用 SSH 和原生 OpenCode loopback 转发，不持久化连接状态机。"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from app.config.settings import get_settings
from app.database import SessionLocal
from app.domain import ExecutionMode
from app.executors.opencode_server import OpenCodeServerClient
from app.models import AgentWorker
from app.runtime.environment import ExecutionEnvironmentRuntime
from app.runtime.occupancy import sync_worker_occupancy

logger = logging.getLogger(__name__)


@dataclass
class WorkerConnection:
    fingerprint: tuple
    ssh: object
    forward: object
    healthy: bool = False
    observation: dict | None = None

    def client(self):
        return OpenCodeServerClient(base_url=self.forward.base_url)

    async def close(self):
        try:
            await self.forward.close()
        finally:
            await self.ssh.close()


class WorkerConnections:
    def __init__(self):
        self.connections: dict[str, WorkerConnection] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    async def get(self, worker):
        if not worker.opencode_port:
            raise RuntimeError("Worker 尚未配置或部署 OpenCode 端口")
        fingerprint = (worker.host, worker.port, worker.username, worker.ssh_password, worker.opencode_port)
        async with self.locks.setdefault(worker.id, asyncio.Lock()):
            current = self.connections.get(worker.id)
            if current and current.fingerprint == fingerprint and current.ssh.is_active():
                return current
            await self._close(worker.id)
            ssh = await ExecutionEnvironmentRuntime().open_session(worker)
            try:
                forward = await ssh.forward_local(worker.opencode_port)
            except BaseException:
                await ssh.close()
                raise
            current = WorkerConnection(fingerprint, ssh, forward)
            self.connections[worker.id] = current
            return current

    async def _close(self, worker_id):
        current = self.connections.pop(worker_id, None)
        if current:
            await current.close()

    async def close(self, worker_id):
        async with self.locks.setdefault(worker_id, asyncio.Lock()):
            await self._close(worker_id)

    def online(self, worker_id):
        current = self.connections.get(worker_id)
        return bool(current and current.healthy and current.ssh.is_active())

    def observation(self, worker_id):
        current = self.connections.get(worker_id)
        return current.observation if self.online(worker_id) else None

    async def probe(self, worker):
        current = await self.get(worker)
        try:
            health = await current.client().health()
        except Exception:
            current.healthy = False
            # HTTP 探测失败不代表 SSH Transport 已断开；保留仍在使用的 SSE/工作区通道。
            if not current.ssh.is_active():
                async with self.locks[worker.id]:
                    if self.connections.get(worker.id) is current:
                        await self._close(worker.id)
            raise
        current.healthy = True
        return health

    async def aclose(self):
        for worker_id in list(self.connections):
            await self.close(worker_id)
        self.locks.clear()


connections = WorkerConnections()


async def worker_connections(workers) -> dict[str, bool]:
    local_enabled = get_settings().executor_type == "opencode_server"
    return {
        worker.id: (
            local_enabled and worker.status != "OFFLINE"
            if worker.mode == ExecutionMode.LOCAL
            else connections.online(worker.id)
        )
        for worker in workers
    }


async def check_worker(worker):
    """连接和健康事实更新不触碰 Task，也不启动或重启 OpenCode。"""
    try:
        health = await connections.probe(worker)
        online = True
    except Exception as exc:
        logger.debug("Worker %s SSH/OpenCode 不可用: %s", worker.id, type(exc).__name__)
        health, online = {}, False
    async with SessionLocal() as session:
        current = await session.scalar(
            select(AgentWorker).where(AgentWorker.id == worker.id).with_for_update()
        )
        if current is None:
            return False
        if (current.host, current.port, current.username, current.ssh_password, current.opencode_port) != (
            worker.host,
            worker.port,
            worker.username,
            worker.ssh_password,
            worker.opencode_port,
        ):
            return False
        current.last_checked_at = datetime.now(UTC)
        if health:
            current.opencode_version = health["version"]
        if current.status != "PAUSED":
            current.status = "IDLE" if online else "OFFLINE"
            await sync_worker_occupancy(session, current.id)
        await session.commit()
    return online


async def supervise_workers():
    async def watch(worker_id):
        while True:
            try:
                async with SessionLocal() as session:
                    worker = await session.get(AgentWorker, worker_id)
                    if worker is None or worker.mode == ExecutionMode.LOCAL:
                        return
                    session.expunge(worker)
                await check_worker(worker)
            except Exception:
                logger.exception("Worker %s 健康巡检失败", worker_id)
            await asyncio.sleep(get_settings().worker_health_interval_seconds)

    watchers = {}
    try:
        while True:
            async with SessionLocal() as session:
                ids = set(
                    (
                        await session.scalars(
                            select(AgentWorker.id).where(AgentWorker.mode != ExecutionMode.LOCAL)
                        )
                    ).all()
                )
            for worker_id in ids:
                if worker_id not in watchers or watchers[worker_id].done():
                    watchers[worker_id] = asyncio.create_task(watch(worker_id))
            for worker_id in set(watchers) - ids:
                task = watchers.pop(worker_id)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await connections.close(worker_id)
            await asyncio.sleep(get_settings().worker_health_interval_seconds)
    finally:
        for task in watchers.values():
            task.cancel()
        await asyncio.gather(*watchers.values(), return_exceptions=True)
        await connections.aclose()
