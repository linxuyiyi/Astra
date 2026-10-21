"""统一入口必须代理 AgentScope 控制台的 ``/agentscope-api/``。

控制台壳和配置中心都固定使用 ``server_url=/agentscope-api/``；只排除静态回退
而不注册代理时，页面 200、``/agentscope-api/health`` 404。这里用 ASGI 请求验证
代理确实注册在兜底静态路由之前，并且请求被转发到 AgentScope。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from nexus_api import agentscope_proxy, service
from nexus_api.agentscope_proxy import router as agentscope_proxy_router


class _LazyJSONStream(httpx.AsyncByteStream):
    """MockTransport 的 bytes 响应会提前读掉；这里保留可流式读取的响应体。"""

    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode()

    async def __aiter__(self):
        yield self._payload


def _json_response(payload: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        headers={"content-type": "application/json"},
        stream=_LazyJSONStream(payload),
    )


def test_agentscope_proxy_is_registered_before_frontend_fallback():
    paths = [getattr(route, "path", "") for route in service.app.routes]
    proxy_indexes = [
        index
        for index, route in enumerate(service.app.routes)
        if getattr(route, "original_router", None) is agentscope_proxy_router
    ]
    assert proxy_indexes, "统一入口没有注册 AgentScope 代理"
    assert proxy_indexes[0] < paths.index("/{full_path:path}")


@pytest.fixture()
def upstream(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/health":
            return _json_response({"status": "ok"})
        return _json_response(
            {"path": request.url.path, "body": request.content.decode()},
        )

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(
        agentscope_proxy,
        "httpx",
        SimpleNamespace(
            AsyncClient=factory,
            Timeout=httpx.Timeout,
            HTTPError=httpx.HTTPError,
        ),
    )
    monkeypatch.setenv("NEXUS_AGENTSCOPE_BASE_URL", "http://agentscope.test")
    return seen


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=service.app), base_url="http://nexus.test"
    )


async def test_console_health_is_proxied(upstream):
    async with _client() as client:
        response = await client.get("/agentscope-api/health", headers={"X-User-ID": "alice"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert [request.url.path for request in upstream] == ["/health"]
    assert upstream[0].headers["x-user-id"] == "alice"
    assert str(upstream[0].url).startswith("http://agentscope.test/")


async def test_console_management_calls_keep_method_path_query_and_body(upstream):
    async with _client() as client:
        response = await client.post(
            "/agentscope-api/agent/?user_id=alice",
            json={"name": "文案"},
            headers={"X-User-ID": "alice"},
        )

    assert response.status_code == 200
    assert response.json() == {"path": "/agent/", "body": '{"name":"文案"}'}
    forwarded = upstream[0]
    assert forwarded.method == "POST"
    assert forwarded.url.path == "/agent/"
    assert forwarded.url.query == b"user_id=alice"


async def test_missing_user_header_falls_back_to_configured_identity(upstream, monkeypatch):
    monkeypatch.setenv("NEXUS_AGENTSCOPE_USER_ID", "nexus")

    async with _client() as client:
        response = await client.get("/agentscope-api/health")

    assert response.status_code == 200
    assert upstream[0].headers["x-user-id"] == "nexus"
