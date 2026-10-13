# -*- coding: utf-8 -*-
"""默认模型引导的错误边界与密钥脱敏回归测试。

运行真实官方 lifespan / SQLite 存储，但不启动 Uvicorn、不调用真实模型。
"""
from __future__ import annotations

import logging
import traceback

import pytest

from nexus_agent_service.app import build_app
from nexus_agent_service.bootstrap import (
    BOOTSTRAP_ERROR_REASON,
    bootstrap_default_model,
)
from nexus_agent_service.config import load_config

SENTINEL_SECRET = "sentinel-secret-must-not-leak"


def _config(data_dir, user: str, api_key: str | None):
    env = {
        "AGENTSCOPE_DATA_DIR": str(data_dir),
        "AGENTSCOPE_INTERNAL_USER_ID": user,
    }
    if api_key:
        env["AGENTSCOPE_MODEL_API_KEY"] = api_key
    return load_config(env=env)


def test_build_app_enables_hide_parameters(tmp_path):
    app = build_app(_config(tmp_path / "agentscope", "nexus-service", "k"))
    assert app.state.storage._engine_kwargs.get("hide_parameters") is True


@pytest.mark.asyncio
async def test_bootstrap_conflict_never_leaks_secret(tmp_path, caplog):
    """固定凭据 id 换内部租户复用同一 DB 会触发原始冲突；对外错误须脱敏。"""
    data_dir = tmp_path / "agentscope"
    config_a = _config(data_dir, "tenant-a", SENTINEL_SECRET)
    config_b = _config(data_dir, "tenant-b", SENTINEL_SECRET)

    app_a = build_app(config_a)
    async with app_a.router.lifespan_context(app_a):
        pass

    app_b = build_app(config_b)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RuntimeError) as excinfo:
            async with app_b.router.lifespan_context(app_b):
                pass

    error = excinfo.value
    assert str(error) == BOOTSTRAP_ERROR_REASON
    rendered = "".join(
        traceback.format_exception(type(error), error, error.__traceback__),
    )
    assert SENTINEL_SECRET not in str(error)
    assert SENTINEL_SECRET not in repr(error)
    assert SENTINEL_SECRET not in rendered
    assert SENTINEL_SECRET not in caplog.text


@pytest.mark.asyncio
async def test_bootstrap_underlying_failure_is_sanitized(tmp_path):
    """底层存储故障同样收敛为不含原始异常的 RuntimeError，不伪装健康。"""

    class _BrokenStorage:
        async def get_credential(self, user_id, credential_id):
            raise RuntimeError(
                f"raw-driver-failure {SENTINEL_SECRET}",
            )

        async def upsert_credential(self, user_id, credential):
            raise RuntimeError(f"raw-driver-failure {SENTINEL_SECRET}")

    config = _config(tmp_path / "agentscope", "nexus-service", None)
    with pytest.raises(RuntimeError) as excinfo:
        await bootstrap_default_model(_BrokenStorage(), config)
    assert str(excinfo.value) == BOOTSTRAP_ERROR_REASON
    assert SENTINEL_SECRET not in str(excinfo.value)
    assert SENTINEL_SECRET not in repr(excinfo.value)
