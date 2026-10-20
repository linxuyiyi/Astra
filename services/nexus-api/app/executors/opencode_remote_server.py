from __future__ import annotations

from collections.abc import Callable
from pathlib import Path, PurePosixPath

from app.config.settings import Settings
from app.executors.base import ExecutorResult
from app.executors.opencode_server import (
    EventCallback,
    OpenCodeServerClient,
    OpenCodeServerExecutor,
    SessionCallback,
)
from app.models import OpenCodeExecutionEnvironment, Task
from app.runtime.environment import ExecutionEnvironmentRuntime

ClientFactory = Callable[[str], OpenCodeServerClient]


class RemoteOpenCodeServerConnection:
    """Worker-channel OpenCode client. Remote execution does not open SSH."""

    def __init__(
        self,
        environment: OpenCodeExecutionEnvironment,
        settings: Settings,
        runtime: ExecutionEnvironmentRuntime,
        *,
        client_factory: ClientFactory | None = None,
    ):
        self.environment = environment
        self.settings = settings
        self.runtime = runtime
        self.client_factory = client_factory or (
            lambda base_url: OpenCodeServerClient(settings, base_url=base_url)
        )
        self.client: OpenCodeServerClient | None = None

    async def __aenter__(self) -> OpenCodeServerClient:
        try:
            from app.database import SessionLocal
            from app.runtime.worker_channel import read_worker_channel
            from app.runtime.worker_client import WorkerOpenCodeClient

            async with SessionLocal() as session:
                channel = await read_worker_channel(session, self.environment.id)
            if channel != "worker":
                raise RuntimeError("远程环境尚未安装 Worker")
            self.client = WorkerOpenCodeClient(self.settings, environment_id=self.environment.id)
            await self.client.health()
            return self.client
        except Exception as exc:
            raise RuntimeError(
                f"员工 {self.environment.employee_id}；环境 {self.environment.name} (LINUX_REMOTE)；"
                f"Workspace={self.environment.workspace_root}；"
                f"Server Port={self.environment.opencode_server_port}；启动方式=Worker；{exc}"
            ) from exc

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        self.client = None

    async def close(self) -> None:
        self.client = None


class RemoteOpenCodeServerExecutor(OpenCodeServerExecutor):
    """Run the structured Server runtime on a Worker-channel Linux environment."""

    def __init__(
        self,
        environment: OpenCodeExecutionEnvironment,
        settings: Settings,
        runtime: ExecutionEnvironmentRuntime | None = None,
        *,
        event_callback: EventCallback | None = None,
        session_callback: SessionCallback | None = None,
        client_factory: ClientFactory | None = None,
    ):
        super().__init__(
            settings,
            model_name=environment.model_name,
            event_callback=event_callback,
            session_callback=session_callback,
        )
        self.environment = environment
        self.runtime = runtime or ExecutionEnvironmentRuntime(settings)
        self.client_factory = client_factory

    @property
    def remote_workspace(self) -> PurePosixPath:
        return PurePosixPath(self.environment.workspace_root)

    def connection(self) -> RemoteOpenCodeServerConnection:
        return RemoteOpenCodeServerConnection(
            self.environment,
            self.settings,
            self.runtime,
            client_factory=self.client_factory,
        )

    async def start(self, task: Task, _workspace: Path) -> ExecutorResult:
        return await self._execute_remote(task)

    async def _execute_remote(
        self,
        task: Task,
        *,
        session_id: str | None = None,
    ) -> ExecutorResult:
        original_client = self.client
        async with self.connection() as client:
            self.client = client
            try:
                return await self._execute(task, self.remote_workspace, session_id=session_id)
            finally:
                self.client = original_client

    async def abort_session(self, session_id: str) -> bool:
        async with self.connection() as client:
            return await client.abort(self.remote_workspace, session_id)
