# -*- coding: utf-8 -*-
"""新测试共享的真实/模拟 ASGI 栈构建器。"""
from __future__ import annotations

from contextlib import asynccontextmanager

import httpx

from nexus_backend.adapter import AgentScopeHTTPClient
from nexus_backend.app import build_app
from nexus_backend.config import load_config as load_backend_config

USER_ID = "nexus-service"
INTERNAL_TOKEN = "test-internal-token-000000000000"


def backend_config(tmp_path, *, token: str | None = INTERNAL_TOKEN):
    return load_backend_config(
        env={
            "NEXUS_BACKEND_DATA_DIR": str(tmp_path / "backend"),
            "NEXUS_BACKEND_PORT": "8799",
            "AGENTSCOPE_SERVICE_URL": "http://127.0.0.1:8766",
            "AGENTSCOPE_INTERNAL_USER_ID": USER_ID,
            **({"NEXUS_INTERNAL_TOKEN": token} if token else {}),
        },
    )


def agentscope_app(
    tmp_path,
    *,
    skill_hubs=None,
    mcp_hubs=None,
    token=None,
    model_key=None,
):
    from nexus_agent_service.app import build_app as build_agentscope_app
    from nexus_agent_service.config import load_config as load_agentscope_config

    env = {
        "AGENTSCOPE_DATA_DIR": str(tmp_path / "agentscope"),
        "AGENTSCOPE_INTERNAL_USER_ID": USER_ID,
    }
    if model_key:
        env["AGENTSCOPE_MODEL_API_KEY"] = model_key
    if token:
        env["NEXUS_INTERNAL_TOKEN"] = token
        env["NEXUS_GUARD_URL"] = "http://backend.test"
    config = load_agentscope_config(env=env)
    return build_agentscope_app(
        config,
        skill_hubs=skill_hubs,
        mcp_hubs=mcp_hubs,
    )


@asynccontextmanager
async def real_stack(
    tmp_path,
    *,
    skill_hubs=None,
    mcp_hubs=None,
    token: str | None = INTERNAL_TOKEN,
    model_key=None,
):
    """真实官方 ASGI（可选 fixture hub/真实 guard）+ 真实后端 SQLite。"""
    upstream = agentscope_app(
        tmp_path,
        skill_hubs=skill_hubs,
        mcp_hubs=mcp_hubs,
        token=token,
        model_key=model_key,
    )
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
        backend_config(tmp_path, token=token),
        adapter=adapter,
    )
    async with upstream.router.lifespan_context(upstream):
        async with backend.router.lifespan_context(backend):
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=backend),
                base_url="http://backend.test",
            )
            try:
                yield backend, adapter, client, upstream
            finally:
                await client.aclose()
    await upstream_client.aclose()


@asynccontextmanager
async def mock_stack(tmp_path, handler, *, token: str | None = INTERNAL_TOKEN):
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8766",
    )
    adapter = AgentScopeHTTPClient(
        "http://127.0.0.1:8766",
        USER_ID,
        client=upstream_client,
    )
    backend = build_app(
        backend_config(tmp_path, token=token),
        adapter=adapter,
    )
    async with backend.router.lifespan_context(backend):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backend),
            base_url="http://backend.test",
        )
        try:
            yield backend, adapter, client
        finally:
            await client.aclose()
    await upstream_client.aclose()
