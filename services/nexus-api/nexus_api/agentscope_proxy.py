"""把官方 AgentScope 控制台的 API 代理到统一入口（18120）。

控制台壳和 ``AgentscopeView`` 都把 ``server_url`` 固定为 ``/agentscope-api/``；
统一入口若不代理该前缀，页面能打开但 ``/health``、``/agent/``、``/credential/``
全部 404。开发由 Vite、生产由 nginx 提供同样的代理；这里让直连 18120 也成立。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

PREFIX = "/agentscope-api"
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# 逐跳头不能跨代理转发；content-length 由 httpx 重新计算。
_DROP_REQUEST_HEADERS = {
    "host",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "accept-encoding",
}
_DROP_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
}

router = APIRouter()

# 与 nexus_api.main 的聊天客户端同一来源：部署脚本和部署指南都设置这个变量。
DEFAULT_BASE_URL = "http://127.0.0.1:18000"


def agentscope_base_url() -> str:
    configured = os.environ.get("NEXUS_AGENTSCOPE_BASE_URL") or DEFAULT_BASE_URL
    return configured.rstrip("/")


def _user_id() -> str:
    return os.environ.get("NEXUS_AGENTSCOPE_USER_ID", "nexus")


@router.api_route(f"{PREFIX}/{{path:path}}", methods=METHODS, include_in_schema=False)
async def proxy_agentscope(path: str, request: Request) -> StreamingResponse:
    # 人类日程身份只能由 Nexus 的 require_owner 派生，控制台不能冒充。
    if request.headers.get("x-user-id", "").casefold().startswith("nexus-schedule-"):
        raise HTTPException(403, "日程身份请通过日程安排入口访问")
    query = request.url.query
    url = f"{agentscope_base_url()}/{path}"
    if query:
        url = f"{url}?{query}"

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _DROP_REQUEST_HEADERS
    }
    from .auth import internal_headers
    headers["x-user-id"] = _user_id()
    headers.update(internal_headers())

    body = await request.body()
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=None, write=120, pool=30))
    upstream_request = client.build_request(request.method, url, headers=headers, content=body)
    try:
        response = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(502, f"AgentScope 不可达：{exc.__class__.__name__}") from exc

    response_headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in _DROP_RESPONSE_HEADERS
    }

    async def body_stream() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(
        body_stream(),
        status_code=response.status_code,
        headers=response_headers,
    )
