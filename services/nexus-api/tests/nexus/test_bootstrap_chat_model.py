"""聊天模型引导脚本。不连接 Agent Service，也不使用真实密钥。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[4]


def load_bootstrap():
    path = ROOT / "services/agent-service/bootstrap_chat_model.py"
    spec = importlib.util.spec_from_file_location("nexus_bootstrap_chat_model", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def bootstrap():
    return load_bootstrap()


def test_deepseek_alias_fills_openai_credential(bootstrap, monkeypatch):
    monkeypatch.setenv("NEXUS_CHAT_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-flash")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")

    data, model = bootstrap.credential_payload()

    assert model == "deepseek-flash"
    assert data == {
        "type": "openai_credential",
        "api_key": "sk-test-not-real",
        "base_url": "https://api.deepseek.com",
    }


def test_openai_api_key_wins_over_deepseek_alias(bootstrap, monkeypatch):
    monkeypatch.setenv("NEXUS_CHAT_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-flash")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")

    data, _model = bootstrap.credential_payload()

    assert data["api_key"] == "sk-openai-test"


def test_openai_provider_exits_when_no_key(bootstrap, monkeypatch):
    monkeypatch.setenv("NEXUS_CHAT_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-flash")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(SystemExit, match="DEEPSEEK_API_KEY"):
        bootstrap.credential_payload()


def test_existing_openai_credential_must_match_base_url(bootstrap):
    seen = bootstrap.same_endpoint(
        {"type": "openai_credential", "base_url": "https://api.openai.com", "api_key": "secret"},
        {"type": "openai_credential", "base_url": "https://api.deepseek.com", "api_key": "other"},
    )
    matched = bootstrap.same_endpoint(
        {"type": "openai_credential", "base_url": "https://api.deepseek.com"},
        {"type": "openai_credential", "base_url": "https://api.deepseek.com", "api_key": "other"},
    )

    assert seen is False
    assert matched is True


def test_printed_config_has_no_api_key(bootstrap, monkeypatch, capsys):
    monkeypatch.setenv("NEXUS_CHAT_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-flash")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            body = {
                "credentials": [
                    {
                        "id": "openai-default",
                        "data": {"type": "openai_credential", "api_key": "other-secret"},
                    }
                ],
                "total": 1,
            }
            return httpx.Response(200, json=body)
        posted["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"credential_id": "cred-deepseek"})

    transport = httpx.MockTransport(handler)
    original_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(bootstrap.httpx, "Client", factory)
    bootstrap.main()
    printed = capsys.readouterr().out

    assert "sk-test-not-real" not in printed
    assert "other-secret" not in printed
    assert json.loads(printed) == {
        "type": "openai_credential",
        "credential_id": "cred-deepseek",
        "model": "deepseek-flash",
        "parameters": {},
    }
    assert posted["body"]["data"]["api_key"] == "sk-test-not-real"
    assert posted["body"]["data"]["base_url"] == "https://api.deepseek.com"
