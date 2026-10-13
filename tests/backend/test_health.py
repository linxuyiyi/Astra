# -*- coding: utf-8 -*-
"""Nexus 后端 ``/api/health`` 的真实性与故障边界测试。

- 与**真实官方 ASGI 服务**（真实 lifespan + SQLite）联动，验证健康连通与
  版本核对，不作为 mock 联调证据。
- 明确标记的**单元**测试用 ``httpx.MockTransport`` 覆盖上游不可达与版本
  不符分支，不冒充真实服务。
"""
from __future__ import annotations

import json

import httpx
import pytest

from nexus_backend.adapter import AgentScopeHTTPClient
from nexus_backend.app import _probe_upstream, build_app
from nexus_backend.config import load_config as load_backend_config

USER_ID = "nexus-service"


def _backend_config(tmp_path, url: str = "http://127.0.0.1:8766"):
    return load_backend_config(
        env={
            "NEXUS_BACKEND_DATA_DIR": str(tmp_path / "backend"),
            "NEXUS_BACKEND_PORT": "8799",
            "AGENTSCOPE_SERVICE_URL": url,
            "AGENTSCOPE_INTERNAL_USER_ID": USER_ID,
        },
    )


async def _get(app, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://backend.test",
    ) as client:
        return await client.get(path)


@pytest.mark.asyncio
async def test_health_ok_against_real_agentscope_asgi(tmp_path):
    from nexus_agent_service.app import build_app as build_agentscope_app
    from nexus_agent_service.config import (
        load_config as load_agentscope_config,
    )

    asc_config = load_agentscope_config(
        env={
            "AGENTSCOPE_DATA_DIR": str(tmp_path / "agentscope"),
            "AGENTSCOPE_INTERNAL_USER_ID": USER_ID,
        },
    )
    upstream = build_agentscope_app(asc_config)
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream),
        base_url="http://127.0.0.1:8766",
    )
    adapter = AgentScopeHTTPClient(
        "http://127.0.0.1:8766",
        USER_ID,
        client=upstream_client,
    )
    backend = build_app(
        _backend_config(tmp_path),
        instance_id="inst-ok",
        adapter=adapter,
    )
    async with upstream.router.lifespan_context(upstream):
        async with backend.router.lifespan_context(backend):
            response = await _get(backend, "/api/health")
    await upstream_client.aclose()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["ready"] is True
    assert body["instance_id"] == "inst-ok"
    assert body["agentscope"]["version"] == "2.0.10"
    assert body["agentscope"]["version_ok"] is True
    assert body["agentscope"]["reachable"] is True
    # 未注入模型密钥：服务就绪但模型未配置，健康明确区分，不伪装。
    assert body["agentscope"]["model_configured"] is False


@pytest.mark.asyncio
async def test_api_health_initializing_before_lifespan(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8766",
    )
    adapter = AgentScopeHTTPClient(
        "http://127.0.0.1:8766",
        USER_ID,
        client=client,
    )
    backend = build_app(_backend_config(tmp_path), adapter=adapter)
    response = await _get(backend, "/api/health")
    await client.aclose()

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "initializing"
    assert body["ready"] is False


@pytest.mark.asyncio
async def test_api_health_unavailable_when_upstream_unreachable(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8766",
    )
    adapter = AgentScopeHTTPClient(
        "http://127.0.0.1:8766",
        USER_ID,
        client=client,
    )
    backend = build_app(
        _backend_config(tmp_path),
        instance_id="inst-down",
        adapter=adapter,
    )
    async with backend.router.lifespan_context(backend):
        response = await _get(backend, "/api/health")
    await client.aclose()

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["ready"] is True
    assert body["agentscope"]["reachable"] is False
    assert body["reason"]


@pytest.mark.asyncio
async def test_probe_upstream_rejects_version_mismatch(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "version": "9.9.9",
                    "components": {},
                },
            )
        return httpx.Response(200, json={"ready": True})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8766",
    )
    adapter = AgentScopeHTTPClient(
        "http://127.0.0.1:8766",
        USER_ID,
        client=client,
    )
    config = _backend_config(tmp_path)
    try:
        info = await _probe_upstream(adapter, config)
    finally:
        await client.aclose()

    assert info["healthy"] is False
    assert info["version_ok"] is False
    assert "版本不符" in info["reason"]


@pytest.mark.asyncio
async def test_probe_upstream_rejects_not_ready_extension(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={"status": "ok", "version": "2.0.10", "components": {}},
            )
        return httpx.Response(
            503,
            json={"ready": False, "reason": "初始化未完成"},
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8766",
    )
    adapter = AgentScopeHTTPClient(
        "http://127.0.0.1:8766",
        USER_ID,
        client=client,
    )
    try:
        info = await _probe_upstream(adapter, _backend_config(tmp_path))
    finally:
        await client.aclose()

    assert info["healthy"] is False
    assert info["extension_ready"] is False
    assert "未就绪" in info["reason"]


SENTINEL = "sk-sentinel-health-should-never-appear-0123456789"


@pytest.mark.asyncio
async def test_probe_upstream_hides_upstream_body_secret(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                503,
                json={"status": "bad", "detail": f"sql token={SENTINEL}"},
            )
        return httpx.Response(
            503,
            json={"ready": False, "reason": f"leak token={SENTINEL}"},
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8766",
    )
    adapter = AgentScopeHTTPClient(
        "http://127.0.0.1:8766",
        USER_ID,
        client=client,
    )
    try:
        info = await _probe_upstream(adapter, _backend_config(tmp_path))
    finally:
        await client.aclose()

    assert info["healthy"] is False
    assert SENTINEL not in json.dumps(info, ensure_ascii=False)
    assert SENTINEL not in (info.get("reason") or "")


@pytest.mark.asyncio
async def test_api_health_does_not_leak_transport_exception(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connection refused token={SENTINEL}")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8766",
    )
    adapter = AgentScopeHTTPClient(
        "http://127.0.0.1:8766",
        USER_ID,
        client=client,
    )
    backend = build_app(
        _backend_config(tmp_path),
        instance_id="inst-secret",
        adapter=adapter,
    )
    async with backend.router.lifespan_context(backend):
        response = await _get(backend, "/api/health")
    await client.aclose()

    assert response.status_code == 503
    assert SENTINEL not in response.text
    assert response.json()["agentscope"]["reachable"] is False
