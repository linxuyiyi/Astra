"""Worker/remote/MCP settings aliases stay independent and Nexus-only."""

from __future__ import annotations

from collections import Counter

import pytest
from pydantic import ValidationError

from app.config.settings import Settings


def test_worker_setting_aliases_are_unique():
    aliases = [
        field.validation_alias
        for field in Settings.model_fields.values()
        if isinstance(field.validation_alias, str)
    ]
    duplicates = [key for key, count in Counter(aliases).items() if count > 1]
    assert duplicates == []


def test_library_and_task_mcp_hosts_are_independent(monkeypatch):
    for name in (
        "NEXUS_MCP_HOST",
        "NEXUS_MCP_PORT",
        "NEXUS_TASK_MCP_HOST",
        "NEXUS_TASK_MCP_PORT",
        "ASTRA_MCP_HOST",
        "ASTRA_TASK_MCP_HOST",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_MCP_HOST", "10.0.0.11")
    monkeypatch.setenv("NEXUS_MCP_PORT", "19001")
    monkeypatch.setenv("NEXUS_TASK_MCP_HOST", "10.0.0.12")
    monkeypatch.setenv("NEXUS_TASK_MCP_PORT", "19002")
    settings = Settings(_env_file=None)
    assert settings.mcp_host == "10.0.0.11"
    assert settings.mcp_port == 19001
    assert settings.task_mcp_host == "10.0.0.12"
    assert settings.task_mcp_port == 19002


def test_old_astra_worker_and_mcp_env_names_are_not_accepted(monkeypatch):
    for name in (
        "NEXUS_MCP_HOST",
        "NEXUS_REMOTE_OPENCODE_VERSION",
        "NEXUS_WORKER_GATEWAY_BUS",
        "NEXUS_OPENCODE_CONTROL_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ASTRA_MCP_HOST", "10.9.9.9")
    monkeypatch.setenv("ASTRA_REMOTE_OPENCODE_VERSION", "0.0.1")
    monkeypatch.setenv("ASTRA_WORKER_GATEWAY_BUS", "redis")
    monkeypatch.setenv("ASTRA_OPENCODE_CONTROL_TIMEOUT_SECONDS", "90")
    with pytest.raises(ValidationError, match="NEXUS_"):
        Settings(_env_file=None)
