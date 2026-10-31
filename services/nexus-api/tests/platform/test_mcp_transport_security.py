"""MCP DNS-rebinding allowlist follows the configured resource authority."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config.settings import get_settings
from app.mcp_server import build_mcp_server, transport_security

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "transport-security-test", "version": "1"},
    },
}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


def _app(settings, *, host: str):
    server = build_mcp_server()
    return server.streamable_http_app(
        streamable_http_path=settings.mcp_streamable_http_path,
        stateless_http=True,
        json_response=True,
        transport_security=transport_security(settings),
        host=host,
    )


def _post(client, host: str, body: dict):
    return client.post(
        "/mcp",
        json=body,
        headers={
            "host": host,
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2025-06-18",
        },
    )


def test_transport_security_allows_configured_authority_only(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(
        settings, "mcp_resource_server_url", "http://172.17.13.60:18042/mcp"
    )
    monkeypatch.setattr(settings, "mcp_issuer_url", "http://172.17.13.60:18042")
    monkeypatch.setattr(settings, "mcp_host", "0.0.0.0")
    monkeypatch.setattr(settings, "mcp_port", 18042)

    security = transport_security(settings)
    assert security.enable_dns_rebinding_protection is True
    assert "172.17.13.60:18042" in security.allowed_hosts
    assert "127.0.0.1:*" in security.allowed_hosts
    assert "localhost:*" in security.allowed_hosts
    assert "*" not in security.allowed_hosts
    assert "evil.example:18042" not in security.allowed_hosts
    assert "http://172.17.13.60:18042" in security.allowed_origins
    assert "*" not in security.allowed_origins


def test_declared_remote_host_initializes_and_lists_tools(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(
        settings, "mcp_resource_server_url", "http://host.lima.internal:61269/mcp"
    )
    monkeypatch.setattr(settings, "mcp_issuer_url", "http://host.lima.internal:61269")
    monkeypatch.setattr(settings, "mcp_host", "127.0.0.1")
    monkeypatch.setattr(settings, "mcp_port", 61269)

    with TestClient(_app(settings, host=settings.mcp_host)) as client:
        initialized = _post(client, "host.lima.internal:61269", INITIALIZE)
        assert initialized.status_code == 200, initialized.text
        assert initialized.json()["result"]["serverInfo"]["name"]

        tools = _post(client, "host.lima.internal:61269", TOOLS_LIST)
        assert tools.status_code == 200, tools.text
        names = {item["name"] for item in tools.json()["result"]["tools"]}
        assert {"library_search", "library_get"} <= names

        evil = _post(client, "evil.example:61269", INITIALIZE)
        assert evil.status_code == 421


def test_default_loopback_authority_still_works(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(
        settings, "mcp_resource_server_url", "http://127.0.0.1:18201/mcp"
    )
    monkeypatch.setattr(settings, "mcp_issuer_url", "http://127.0.0.1:18201")
    monkeypatch.setattr(settings, "mcp_host", "127.0.0.1")
    monkeypatch.setattr(settings, "mcp_port", 18201)

    with TestClient(_app(settings, host=settings.mcp_host)) as client:
        response = _post(client, "127.0.0.1:18201", INITIALIZE)
        assert response.status_code == 200, response.text
