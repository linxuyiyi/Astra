"""独立复盘模型调用：走现有 Nexus Gateway，不创建 AgentScope Agent。"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

import httpx

from app.config.settings import Settings, get_settings

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class ReviewLLMError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ReviewLLMClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None

    @property
    def model_id(self) -> str:
        return self.settings.experience_review_model

    def _key(self) -> str:
        return (
            self.settings.experience_review_gateway_key
            or os.environ.get("NEXUS_GATEWAY_API_KEY", "")
            or ""
        )

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=90.0)
        headers = {"Content-Type": "application/json"}
        key = self._key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return await self._client.post(
            self.settings.experience_review_gateway_url,
            headers=headers,
            json=body,
        )

    async def complete_json(self, system: str, user: str) -> dict[str, Any]:
        body = {
            "model": self.settings.experience_review_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        last_error = "模型调用失败"
        for attempt in range(3):
            try:
                response = await self._post(body)
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"Gateway HTTP {response.status_code}"
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                if response.status_code >= 400:
                    raise ReviewLLMError(
                        "gateway_rejected",
                        f"Gateway HTTP {response.status_code}: {response.text[:500]}",
                    )
                payload = response.json()
                content = payload["choices"][0]["message"]["content"]
                if not isinstance(content, str):
                    raise ValueError("模型返回内容不是字符串")
                try:
                    return json.loads(_FENCE.sub("", content.strip()))
                except json.JSONDecodeError:
                    last_error = "模型返回不是合法 JSON"
                    continue
            except ReviewLLMError:
                raise
            except Exception as exc:  # noqa: BLE001 - HTTP boundary
                last_error = str(exc)
                await asyncio.sleep(0.5 * (2**attempt))
        raise ReviewLLMError("llm_unavailable", last_error)
