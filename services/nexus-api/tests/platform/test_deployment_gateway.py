"""部署环境必须能脱离安装进程恢复，不依赖本机 OpenCode 或厂商 key。"""

import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.deployment import check_wiki_project, register_gateway_key
from app.gateway.models import GatewayKey
from app.gateway.security import gateway_key_digest
from app.gateway.urls import gateway_base

ROOT = Path(__file__).resolve().parents[4]
INSTALLER = ROOT / "ops/deploy/install_nexus.sh"












@pytest.mark.parametrize("url", ["http://gateway.test:18110", "http://gateway.test:18110/v1/"])
def test_gateway_clients_share_canonical_v1(url):
    assert gateway_base(url) == "http://gateway.test:18110/v1"


@pytest.mark.parametrize(
    "url",
    ["http://gateway.test/v2", "http://user:secret@gateway.test", "bad", "http://gateway.test?key=secret"],
)
def test_invalid_gateway_url_does_not_echo_credentials(url):
    with pytest.raises(ValueError) as exc:
        gateway_base(url)
    assert "secret" not in str(exc.value)


async def test_platform_key_registration_is_idempotent_and_never_revives_revoked_key(monkeypatch):
    raw = "agw_test_" + uuid4().hex
    monkeypatch.setenv("NEXUS_GATEWAY_API_KEY", raw)
    monkeypatch.setenv("NEXUS_GATEWAY_OPENAI_BASE", "http://gateway.test/v1")
    await register_gateway_key()
    await register_gateway_key()
    async with SessionLocal() as session:
        rows = (
            await session.scalars(select(GatewayKey).where(GatewayKey.key_digest == gateway_key_digest(raw)))
        ).all()
        assert len(rows) == 1
        rows[0].enabled = False
        await session.commit()
    with pytest.raises(RuntimeError, match="禁用"):
        await register_gateway_key()


async def test_wiki_health_cannot_mask_project_auth_failure(monkeypatch):
    from app.config.settings import get_settings
    from app.llm_wiki import LlmWikiError

    monkeypatch.setattr(get_settings(), "llm_wiki_enabled", True)
    monkeypatch.setattr(get_settings(), "llm_wiki_api_base_url", "http://wiki.test/api/v1")
    monkeypatch.setattr(get_settings(), "llm_wiki_api_token", "test-token")
    monkeypatch.setattr(get_settings(), "llm_wiki_project_id", "test-project")
    original = httpx.AsyncClient
    paths = []

    def handler(request):
        paths.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"ok": True, "status": "running"})
        return httpx.Response(403)

    monkeypatch.setattr(
        "app.llm_wiki.httpx.AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    with pytest.raises(LlmWikiError, match="Token"):
        await check_wiki_project()
    assert paths == ["/api/v1/health", "/api/v1/projects/test-project/search"]


def test_local_managed_runtime_cannot_use_vendor_provider_or_small_model():
    from app.runtime.opencode_model import gateway_environment

    original = {
        "NEXUS_GATEWAY_API_KEY": "agw_test_local",
        "NEXUS_GATEWAY_OPENAI_BASE": "http://gateway.test:18110",
        "OPENCODE_CONFIG_CONTENT": json.dumps(
            {
                "mcp": {"custom": {"type": "remote", "url": "http://mcp.test"}},
                "provider": {"vendor": {}},
                "small_model": "vendor/direct",
            }
        ),
    }
    result = gateway_environment("alias-model", original)
    config = json.loads(result["OPENCODE_CONFIG_CONTENT"])
    assert config["model"] == config["small_model"] == "nexus-gateway/alias-model"
    assert config["enabled_providers"] == ["nexus-gateway"]
    assert config["provider"]["nexus-gateway"]["options"]["baseURL"] == "http://gateway.test:18110/v1"
    assert config["mcp"]["custom"]["url"] == "http://mcp.test"
    assert "agw_test_local" not in result["OPENCODE_CONFIG_CONTENT"]
    assert json.loads(original["OPENCODE_CONFIG_CONTENT"])["small_model"] == "vendor/direct"




@pytest.mark.parametrize("field,value", [
    ("small_model", "vendor/small"), ("model", "vendor/main"),
    ("enabled_providers", ["nexus-gateway", "vendor"]),
])
async def test_shared_serve_checks_actual_config_before_ready(monkeypatch, field, value):
    from app.config.settings import Settings
    from app.runtime.opencode_model import gateway_environment
    from app.runtime.opencode_server import OpenCodeServerManager

    source = {"NEXUS_GATEWAY_API_KEY": "agw_test"}
    config = json.loads(gateway_environment("default", source)["OPENCODE_CONFIG_CONTENT"])
    config[field] = value
    monkeypatch.delenv("NEXUS_GATEWAY_OPENAI_BASE", raising=False)
    original = httpx.AsyncClient

    def handler(request):
        if request.url.path == "/global/health":
            return httpx.Response(200, json={"healthy": True, "version": "1.18.35"})
        assert request.url.path == "/config"
        return httpx.Response(200, json=config)

    monkeypatch.setattr("app.runtime.opencode_server.httpx.AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    manager = OpenCodeServerManager(Settings(seed_demo=False))
    with pytest.raises(RuntimeError, match="统一网关"):
        await manager.wait_until_ready()
    config[field] = json.loads(gateway_environment("default", source)["OPENCODE_CONFIG_CONTENT"])[field]
    assert (await manager.wait_until_ready()).healthy
