"""Worker 配置与部署 SSH 边界；OpenCode 路径及端口仅来自运行配置。"""

import asyncio

from app.config.settings import get_settings
from app.runtime.ssh_client import SSHSession


def environment_snapshot(worker):
    keys = (
        "id",
        "name",
        "mode",
        "host",
        "port",
        "username",
        "workspace_root",
        "repository_url",
        "branch",
        "model_name",
        "skill_refs",
    )
    return {key: getattr(worker, key) for key in keys}


class ExecutionEnvironmentRuntime:
    def __init__(self, settings=None):
        self.settings = settings or get_settings()

    async def open_session(self, worker):
        from app.runtime.repository import cipher

        if not worker.host or not worker.username or not worker.ssh_password:
            raise RuntimeError("Worker 尚未配置 SSH 连接")
        session = SSHSession(
            host=worker.host,
            port=worker.port,
            username=worker.username,
            password=cipher().decrypt(worker.ssh_password),
            connect_timeout=self.settings.ssh_connect_timeout_seconds,
        )
        await session.connect()
        return session

    async def run_remote(self, worker, command, *, timeout=None):
        session = await self.open_session(worker)
        try:
            return await asyncio.wait_for(
                session.run_command(command), timeout or self.settings.opencode_timeout_seconds
            )
        finally:
            await session.close()
