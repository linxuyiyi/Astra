# -*- coding: utf-8 -*-
"""应用启动、扩展接口与默认模型引导的边界测试。

这些测试运行真实官方 lifespan 与真实 SQLite 存储，但不启动 Uvicorn
进程、不调用真实模型。
"""
from __future__ import annotations

import httpx
import pytest

from nexus_agent_service.app import build_app
from nexus_agent_service.bootstrap import bootstrap_default_model
from nexus_agent_service.config import DEFAULT_CREDENTIAL_ID, load_config


def _config(tmp_path, api_key: str | None = None, user: str | None = None):
    env = {
        "AGENTSCOPE_DATA_DIR": str(tmp_path / "agentscope"),
        "AGENTSCOPE_MODEL_NAME": "deepseek-flash",
    }
    if api_key:
        env["AGENTSCOPE_MODEL_API_KEY"] = api_key
    if user:
        env["AGENTSCOPE_INTERNAL_USER_ID"] = user
    return load_config(env=env)


async def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_defaults_without_key_reports_unconfigured(tmp_path):
    config = _config(tmp_path)
    app = build_app(config)
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            health = (await client.get("/nexus/health")).json()
            assert health["status"] == "ok"
            assert health["model_configured"] is False
            assert health["reason"]

            defaults = (await client.get("/nexus/defaults")).json()
            assert defaults["model_configured"] is False
            assert defaults["chat_model_config"] is None
            assert defaults["reason"]

            native = await client.get(
                "/health",
                headers={"X-User-ID": config.internal_user_id},
            )
            assert native.status_code == 200
            assert native.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_defaults_with_key_and_idempotent_bootstrap(tmp_path):
    secret = "unit-test-secret-key"
    config = _config(tmp_path, api_key=secret)
    app = build_app(config)
    async with app.router.lifespan_context(app):
        storage = app.state.storage
        credentials = await storage.list_credentials(config.internal_user_id)
        assert len(credentials) == 1
        assert credentials[0].id == DEFAULT_CREDENTIAL_ID

        # 再次引导必须幂等，不新增重复凭据。
        await bootstrap_default_model(storage, config)
        credentials = await storage.list_credentials(config.internal_user_id)
        assert len(credentials) == 1

        async with await _client(app) as client:
            defaults = (await client.get("/nexus/defaults")).json()
            assert defaults["model_configured"] is True
            cfg = defaults["chat_model_config"]
            assert cfg["credential_id"] == DEFAULT_CREDENTIAL_ID
            assert cfg["type"] == "deepseek_chat"
            assert cfg["model"] == "deepseek-flash"
            # 响应绝不包含密钥。
            assert secret not in (await client.get("/nexus/defaults")).text
            assert secret not in (await client.get("/nexus/health")).text


@pytest.mark.asyncio
async def test_reuses_persisted_credential_without_env_key(tmp_path):
    """先带 env 密钥启动写入凭据；无 env 重启须复用同一条，仍 configured。"""
    secret = "restart-persist-sentinel"
    config_first = _config(tmp_path, api_key=secret)
    app_first = build_app(config_first)
    async with app_first.router.lifespan_context(app_first):
        storage = app_first.state.storage
        credentials = await storage.list_credentials(
            config_first.internal_user_id,
        )
        assert len(credentials) == 1
        first_id = credentials[0].id
        assert first_id == DEFAULT_CREDENTIAL_ID

    # 同一 data_dir、无 env 密钥重启：不得要求重新提交已落库的密钥。
    config_restart = _config(tmp_path)
    assert config_restart.model_configured is False
    app_restart = build_app(config_restart)
    async with app_restart.router.lifespan_context(app_restart):
        storage = app_restart.state.storage
        credentials = await storage.list_credentials(
            config_restart.internal_user_id,
        )
        assert len(credentials) == 1
        assert credentials[0].id == DEFAULT_CREDENTIAL_ID
        async with await _client(app_restart) as client:
            health = (await client.get("/nexus/health")).json()
            assert health["model_configured"] is True
            defaults = (await client.get("/nexus/defaults")).json()
            assert defaults["model_configured"] is True
            assert defaults["chat_model_config"]["credential_id"] == (
                DEFAULT_CREDENTIAL_ID
            )
            assert secret not in (await client.get("/nexus/defaults")).text


@pytest.mark.asyncio
async def test_health_not_ready_before_lifespan(tmp_path):
    """lifespan 未完成前不得报告 ok；退出后清除就绪。"""
    config = _config(tmp_path)
    app = build_app(config)
    async with await _client(app) as client:
        before = await client.get("/nexus/health")
        assert before.status_code == 503
        assert before.json()["status"] != "ok"
        assert before.json()["ready"] is False
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            ready = await client.get("/nexus/health")
            assert ready.status_code == 200
            assert ready.json()["ready"] is True
    # lifespan 退出后清除就绪。
    assert app.state.nexus_ready is False
