"""平台助手专用 OpenCode Client。

整个助手业务层只依赖 ``AssistantOpenCodeClient``，不允许在业务代码中散落
``http://127.0.0.1:xxxx``。助手使用独立（或与执行共享但目录隔离）的
OpenCode Server；认证信息由 Backend 持有，浏览器永远不接触。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config.settings import PROJECT_ROOT, Settings, get_settings
from app.executors.opencode_server import OpenCodeServerClient, split_model


class AssistantOpenCodeUnavailable(RuntimeError):
    pass


class AssistantOpenCodeClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.base_url = self.settings.resolved_assistant_opencode_url
        self._auth = (
            httpx.BasicAuth(
                self.settings.assistant_opencode_username,
                self.settings.assistant_opencode_password,
            )
            if self.settings.assistant_opencode_password
            else None
        )
        self._inner = OpenCodeServerClient(
            self.settings,
            base_url=self.base_url,
            auth=self._auth,
            timeout_seconds=self.settings.assistant_request_timeout_seconds,
        )

    @property
    def workspace(self) -> str:
        return str(PROJECT_ROOT)

    async def health(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(auth=self._auth, timeout=3) as client:
                response = await client.get(f"{self.base_url}/global/health")
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AssistantOpenCodeUnavailable(f"助手 OpenCode Server 不可用: {exc}") from exc
        if payload.get("healthy") is not True:
            raise AssistantOpenCodeUnavailable(
                f"助手 OpenCode Server 健康检查失败: {json.dumps(payload, ensure_ascii=False)}"
            )
        return {"healthy": True, "version": str(payload.get("version") or "") or None}

    async def create_session(self, *, title: str) -> str:
        body: dict[str, Any] = {
            "title": title,
            "metadata": {"astraRuntime": "platform-assistant"},
        }
        model = split_model(self.settings.assistant_model)
        if model:
            body["model"] = {"providerID": model[0], "id": model[1]}
        if self.settings.assistant_agent:
            body["agent"] = self.settings.assistant_agent
        created = await self._inner.request("POST", "/session", directory=self.workspace, body=body)
        return str(created["id"])

    async def delete_session(self, session_id: str) -> None:
        try:
            await self._inner.request("DELETE", f"/session/{session_id}", directory=self.workspace)
        except (httpx.HTTPError, RuntimeError):
            # 会话清理失败不影响平台侧删除语义。
            return

    async def session_exists(self, session_id: str) -> bool:
        try:
            await self._inner.request("GET", f"/session/{session_id}", directory=self.workspace)
            return True
        except (httpx.HTTPError, RuntimeError):
            return False

    async def prompt_async(self, session_id: str, *, text: str) -> None:
        body: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        model = split_model(self.settings.assistant_model)
        if model:
            body["model"] = {"providerID": model[0], "modelID": model[1]}
        if self.settings.assistant_agent:
            body["agent"] = self.settings.assistant_agent
        await self._inner.request(
            "POST",
            f"/session/{session_id}/prompt_async",
            directory=self.workspace,
            body=body,
        )

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        return await self._inner.request("GET", f"/session/{session_id}/message", directory=self.workspace)

    async def status(self, session_id: str) -> str:
        statuses = await self._inner.request("GET", "/session/status", directory=self.workspace)
        value = statuses.get(session_id) if isinstance(statuses, dict) else None
        return str((value or {"type": "idle"}).get("type") or "idle")

    async def abort(self, session_id: str) -> None:
        await self._inner.request("POST", f"/session/{session_id}/abort", directory=self.workspace)

    async def stream_events(self) -> AsyncIterator[dict[str, Any]]:
        async for event in self._inner.events(self.workspace):
            yield event
