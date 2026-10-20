import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any

from app.config.settings import Settings
from app.executors.base import ExecutorResult
from app.executors.opencode_cli import OpenCodeCLIExecutor
from app.models import OpenCodeExecutionEnvironment, Task
from app.runtime.environment import ExecutionEnvironmentRuntime
from app.runtime.ssh_client import RemoteExecChannel


class RemoteOpenCodeExecutor(OpenCodeCLIExecutor):
    def __init__(
        self,
        environment: OpenCodeExecutionEnvironment,
        settings: Settings,
        runtime: ExecutionEnvironmentRuntime | None = None,
        event_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ):
        super().__init__(
            settings,
            opencode_binary=environment.opencode_path,
            model_name=environment.model_name,
            event_callback=event_callback,
        )
        self.environment = environment
        self.runtime = runtime or ExecutionEnvironmentRuntime(settings)

    async def _execute(self, task: Task, workspace: Path, *, session_id: str | None = None) -> ExecutorResult:
        remote_workspace = PurePosixPath(workspace.as_posix())
        remote_command = shlex.join(self._command(task, remote_workspace, session_id))
        session = await self.runtime.open_session(self.environment)
        try:
            channel = await session.open_exec(remote_command)
            return await self._communicate_channel(task, channel, session_id=session_id)
        finally:
            await session.close()

    async def _communicate_channel(
        self,
        task: Task,
        channel: RemoteExecChannel,
        *,
        session_id: str | None = None,
    ) -> ExecutorResult:
        return await self._communicate(task, channel, session_id=session_id)

    async def _terminate_process(self, process: RemoteExecChannel) -> None:
        await process.terminate()
