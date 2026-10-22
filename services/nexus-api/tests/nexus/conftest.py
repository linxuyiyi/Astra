"""让既有对话测试带上一条测试路由。空路由用例自己不传 model_route_id。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_sql_database(tmp_path, monkeypatch):
    from app.config.settings import get_settings
    from app.sql_storage import create_test_schema

    database = tmp_path / "nexus.db"
    monkeypatch.setenv("ASTRA_DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    get_settings.cache_clear()
    create_test_schema(database)
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def test_model_routes(monkeypatch, request):
    monkeypatch.setenv("NEXUS_GATEWAY_API_KEY", "gw-test-key")
    monkeypatch.setenv("NEXUS_GATEWAY_OPENAI_BASE", "http://127.0.0.1:18110/v1")
    if request.module.__name__.endswith("test_model_route"):
        return

    async def loader(_url):
        return [{"id": "route-test", "model": "opencode/deepseek-v4.1-flash-free", "enabled": True}]

    monkeypatch.setattr("nexus_api.model_route.fetch_routes", loader)

    async def config(_client, route_id, *, loader=None):
        from nexus_api.model_route import require_enabled_route

        route = await require_enabled_route(route_id)
        return {
            "type": "openai_credential",
            "credential_id": "cred-test",
            "model": route["model"],
            "parameters": {},
        }

    monkeypatch.setattr("nexus_api.main.chat_config_for_route", config)


@pytest.fixture(autouse=True)
def test_service_auth(monkeypatch):
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "agent-service"))
    monkeypatch.setenv("NEXUS_INTERNAL_KEY", "test-internal-key")
