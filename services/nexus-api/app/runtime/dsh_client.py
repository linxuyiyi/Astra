"""DSH 的薄 RuntimeDriver：复用 Worker Gateway，不暴露 OpenCode 原生 API。"""

from __future__ import annotations

import json

from app.runtime.worker_transport import WorkerCommandTransport


class DshRuntimeClient:
    def __init__(self, environment_id: int, binding, *, transport=None):
        self.binding = binding
        self.transport = transport or WorkerCommandTransport(environment_id=environment_id)

    async def call(self, action: str, **data) -> dict:
        args = {
            "task_id": self.binding.task_id,
            "binding_id": self.binding.id,
            "native_session_id": self.binding.native_session_id,
            **data,
        }
        result = await self.transport.run_command(f"dsh_{action}", args, session_id=self.binding.id)
        if isinstance(result.get("body"), str):
            result = json.loads(result["body"])
        return result

    async def prepare(self, *, model: str) -> dict:
        return await self.call(
            "prepare",
            workspace=self.binding.workspace,
            model=model,
            skill_refs=self.binding.environment.get("skill_refs", []),
            mcp_names=self.binding.environment.get("mcp_names", []),
        )

    async def lookup(self) -> dict:
        return await self.call("lookup")

    async def snapshot(self, *, after: int = 0) -> dict:
        return await self.call("snapshot", after=after)

    async def submit(self, *, submission_id: str, text: str, generation: int, fence: int) -> dict:
        return await self.call(
            "submit", submission_id=submission_id, text=text, generation=generation, fence=fence
        )

    async def cancel(self, *, fence: int) -> dict:
        return await self.call("cancel", fence=fence)

    async def dispose(self, *, fence: int) -> dict:
        return await self.call("dispose", fence=fence)

    async def reply_permission(self, *, permission_id: str, option_id: str) -> dict:
        return await self.call("permission", permission_id=permission_id, option_id=option_id)

    async def read_trace_page(self, *, cursor: int = 0, limit: int = 40) -> dict:
        return await self.call("trace", cursor=cursor, limit=limit)
