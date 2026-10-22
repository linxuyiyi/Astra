"""Talk to the OpenCode process on 127.0.0.1 and return the body to the Gateway."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from app.worker_gateway.protocol import MAX_CHUNK_BYTES

_PASS_HEADERS = {"x-next-cursor", "link", "x-has-more", "content-type"}


def gateway_http_base(gateway_url: str) -> str:
    """Origin of the Gateway. The connect path is not part of the data-channel URL."""

    parts = urlsplit(gateway_url)
    from app.worker_agent.transport import websocket_url

    websocket_url(gateway_url)  # 同一地址校验规则；不得把 HTTPS 静默降为 HTTP。
    scheme = "https" if parts.scheme in {"wss", "https"} else "http"
    return f"{scheme}://{parts.netloc}"


def bind_local_opencode(manager, *, timeout: float = 60) -> None:
    base = gateway_http_base(manager.config.gateway_url)
    token = manager.config.token
    opencode = manager.config.opencode_url.rstrip("/")

    async def fetch_body(request_id: str, epoch: int) -> dict[str, Any]:
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    f"{base}/api/worker-gateway/data/{request_id}/request",
                    headers=_auth(token, epoch),
                )
                response.raise_for_status()
                return response.json()

    async def fetch_upload(request_id: str, epoch: int) -> bytes:
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    f"{base}/api/worker-gateway/data/{request_id}/request",
                    headers=_auth(token, epoch),
                )
                response.raise_for_status()
                return response.content

    async def local_request(method: str, path: str, fetched: Any) -> dict[str, Any]:
        query: dict[str, Any] = {}
        raw = fetched
        if isinstance(fetched, dict) and "query" in fetched:
            query = dict(fetched.get("query") or {})
            raw = fetched.get("body")
        content = raw if isinstance(raw, str) else json.dumps(raw if raw is not None else {})
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(
                    method,
                    f"{opencode}{path}",
                    params=query,
                    content=content.encode("utf-8"),
                    headers={"content-type": "application/json"},
                )
        headers = {
            key.lower(): value
            for key, value in response.headers.items()
            if key.lower() in _PASS_HEADERS
        }
        return {
            "status": response.status_code,
            "body": response.text,
            "headers": headers,
            "content_type": response.headers.get("content-type"),
        }

    async def upload_body(request_id: str, epoch: int, result: dict[str, Any]) -> None:
        text = str(result.get("body") or "")
        upload_id = uuid4().hex
        pieces = _chunks(text)
        if not pieces:
            pieces = [""]
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(timeout=timeout) as client:
                for index, piece in enumerate(pieces):
                    response = await client.put(
                        f"{base}/api/worker-gateway/data/{request_id}/response",
                        headers=_auth(token, epoch),
                        json={
                            "upload_id": upload_id,
                            "http_status": int(result.get("status") or 200),
                            "headers": result.get("headers") or {},
                            "content_type": result.get("content_type"),
                            "chunk": piece,
                            "complete": index == len(pieces) - 1,
                            **(
                                {"result": result["result"]}
                                if isinstance(result.get("result"), dict)
                                else {}
                            ),
                        },
                    )
                    response.raise_for_status()

    manager.fetch_body = fetch_body
    manager.fetch_upload = fetch_upload
    manager.local_request = local_request
    manager.upload_body = upload_body


def _auth(token: str, epoch: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Astra-Worker-Epoch": str(epoch)}


def _chunks(text: str) -> list[str]:
    """按 UTF-8 字节宽度切分。一次编码，线性扫描，不拆开多字节字符。"""

    if not text:
        return []
    data = text.encode("utf-8")
    pieces: list[str] = []
    start = 0
    size = len(data)
    limit = MAX_CHUNK_BYTES
    while start < size:
        end = min(start + limit, size)
        if end < size:
            while end > start and data[end] & 0xC0 == 0x80:
                end -= 1
            if end == start:
                end = min(start + limit, size)
        pieces.append(data[start:end].decode("utf-8"))
        start = end
    return pieces
