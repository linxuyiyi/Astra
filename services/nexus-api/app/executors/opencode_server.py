from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from app.config.settings import Settings, get_settings
from app.executors.errors import (
    OpenCodeExecutionError,
    OpenCodeExecutionTimeout,
    OpenCodeServerUnavailable,
    OpenCodeSessionNotFound,
)
from app.runtime.opencode_server import OpenCodeServerManager

WorkspacePath = Path | PurePosixPath


class OpenCodeServerCapabilityError(RuntimeError):
    pass


def split_model(value: str | None) -> tuple[str, str] | None:
    if not value:
        return None
    provider, separator, model = value.partition("/")
    if not separator or not provider or not model:
        raise ValueError(f"OpenCode 模型必须使用 provider/model 格式：{value}")
    return provider, model


class OpenCodeServerClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        base_url: str | None = None,
        auth: httpx.BasicAuth | None = None,
        timeout_seconds: int | None = None,
    ):
        self.settings = settings or get_settings()
        self.manager = OpenCodeServerManager(self.settings)
        self.base_url = (base_url or self.manager.base_url).rstrip("/")
        self._auth = auth if base_url else self.manager.auth
        # HTTP requests are control-plane calls. The long execution budget is a
        # separate deadline, so a stalled request fails fast instead of holding a
        # worker for opencode_timeout_seconds.
        self._timeout_seconds = timeout_seconds or self.settings.opencode_control_timeout_seconds

    @property
    def auth(self) -> httpx.BasicAuth | None:
        return self._auth

    def _timeout(self, *, stream: bool = False) -> httpx.Timeout:
        return httpx.Timeout(
            None if stream else self._timeout_seconds,
            connect=10,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        directory: WorkspacePath | None = None,
        body: Any = None,
        query: dict[str, Any] | None = None,
        include_headers: bool = False,
        command_request_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> Any:
        params = dict(query or {})
        if directory:
            params["directory"] = str(directory)
        try:
            status, content, headers = await self.exchange(
                method, path, params=params, body=body, session_id=session_id, task_id=task_id
            )
        except httpx.TimeoutException as exc:
            raise OpenCodeExecutionTimeout(str(exc)) from exc
        except httpx.TransportError as exc:
            raise OpenCodeServerUnavailable(str(exc)) from exc
        if status == 404 and path.startswith("/session/") and path != "/session/status":
            raise OpenCodeSessionNotFound(path.split("/")[2])
        if status >= 500:
            raise OpenCodeServerUnavailable(f"OpenCode HTTP {status}")
        if status >= 400:
            raise OpenCodeExecutionError(f"OpenCode HTTP {status}: {content[:1000]!r}")
        payload = json.loads(content) if content else None
        return (payload, headers) if include_headers else payload

    async def exchange(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        async with httpx.AsyncClient(auth=self.auth, timeout=self._timeout()) as client:
            response = await client.request(
                method,
                f"{self.base_url}{path}",
                params=params,
                json=body,
            )
        headers = {key.lower(): value for key, value in response.headers.items()}
        return response.status_code, response.content, headers

    async def health(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(auth=self.auth, timeout=3) as client:
                response = await client.get(f"{self.base_url}/global/health")
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OpenCodeServerUnavailable(f"OpenCode Server 不可用: {exc}") from exc
        version = str(payload.get("version") or "") or None
        version_error = self.manager.version_error(version)
        if version_error:
            raise OpenCodeServerCapabilityError(version_error)
        if payload.get("healthy") is not True:
            raise OpenCodeServerCapabilityError(
                f"OpenCode Server 健康检查失败: {json.dumps(payload, ensure_ascii=False)}"
            )
        return {"healthy": True, "version": version}

    async def events(self, directory: WorkspacePath) -> AsyncIterator[dict[str, Any]]:
        async with httpx.AsyncClient(auth=self.auth, timeout=self._timeout(stream=True)) as client:
            async with client.stream(
                "GET",
                f"{self.base_url}/event",
                params={"directory": str(directory)},
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = json.loads(line[6:])
                    if isinstance(payload, dict):
                        yield payload

    async def create_session(
        self,
        directory: WorkspacePath,
        *,
        title: str,
        model_name: str | None,
        agent: str | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "title": title,
            "metadata": {"astraRuntime": "opencode_server"},
        }
        model = split_model(model_name)
        if model:
            body["model"] = {"providerID": model[0], "id": model[1]}
        if agent:
            body["agent"] = agent
        return await self.request("POST", "/session", directory=directory, body=body)

    async def session(self, directory: WorkspacePath, session_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/session/{session_id}", directory=directory, session_id=session_id)

    async def prompt(
        self,
        directory: WorkspacePath,
        session_id: str,
        *,
        text: str,
        model_name: str | None,
        agent: str | None,
    ) -> None:
        body: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        model = split_model(model_name)
        if model:
            body["model"] = {"providerID": model[0], "modelID": model[1]}
        if agent:
            body["agent"] = agent
        await self.request(
            "POST",
            f"/session/{session_id}/prompt_async",
            directory=directory,
            body=body,
            session_id=session_id,
        )

    async def messages(self, directory: WorkspacePath, session_id: str) -> list[dict[str, Any]]:
        return await self.request(
            "GET", f"/session/{session_id}/message", directory=directory, session_id=session_id
        )

    async def messages_page(
        self, directory: WorkspacePath, session_id: str, *, limit: int, before: str | None = None
    ) -> dict[str, Any]:
        query: dict[str, Any] = {"limit": limit}
        if before:
            query["before"] = before
        messages, headers = await self.request(
            "GET",
            f"/session/{session_id}/message",
            directory=directory,
            query=query,
            include_headers=True,
            session_id=session_id,
        )
        return {"messages": messages, "next_before": headers.get("x-next-cursor")}

    async def children(self, directory: WorkspacePath, session_id: str) -> list[dict[str, Any]]:
        return await self.request(
            "GET", f"/session/{session_id}/children", directory=directory, session_id=session_id
        )

    async def questions(self, directory: WorkspacePath) -> list[dict[str, Any]]:
        return await self.request("GET", "/question", directory=directory)

    async def permissions(self, directory: WorkspacePath) -> list[dict[str, Any]]:
        return await self.request("GET", "/permission", directory=directory)

    async def reply_question(
        self,
        directory: WorkspacePath,
        request_id: str,
        answers: list[list[str]],
    ) -> bool:
        return bool(
            await self.request(
                "POST",
                f"/question/{request_id}/reply",
                directory=directory,
                body={"answers": answers},
            )
        )

    async def reply_permission(
        self,
        directory: WorkspacePath,
        request_id: str,
        reply: str,
    ) -> bool:
        return bool(
            await self.request(
                "POST",
                f"/permission/{request_id}/reply",
                directory=directory,
                body={"reply": reply},
            )
        )

    async def abort(self, directory: WorkspacePath, session_id: str) -> bool:
        return bool(
            await self.request(
                "POST", f"/session/{session_id}/abort", directory=directory, session_id=session_id
            )
        )

    async def status(self, directory: WorkspacePath, session_id: str) -> str:
        await self.session(directory, session_id)
        statuses = await self.request("GET", "/session/status", directory=directory, session_id=session_id)
        return str((statuses.get(session_id) or {"type": "idle"}).get("type"))
