"""独立 Task MCP 的部署地址配置。资源挂载回归见 test_explicit_bindings。"""

def test_task_mcp_address_uses_configured_listener_port_and_preserves_explicit_url(monkeypatch):
    from types import SimpleNamespace

    from nexus_api.mcp_endpoints import task_mcp_url

    monkeypatch.delenv("NEXUS_TASK_MCP_URL", raising=False)
    monkeypatch.setattr("app.config.settings.get_settings", lambda: SimpleNamespace(task_mcp_port=18202))
    assert task_mcp_url() == "http://127.0.0.1:18202/mcp"
    monkeypatch.setattr("app.config.settings.get_settings", lambda: SimpleNamespace(task_mcp_port=19202))
    assert task_mcp_url() == "http://127.0.0.1:19202/mcp"
    monkeypatch.setenv("NEXUS_TASK_MCP_URL", "http://task.internal:18202/mcp")
    assert task_mcp_url() == "http://task.internal:18202/mcp"
