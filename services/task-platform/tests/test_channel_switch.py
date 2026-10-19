"""Intranet address rewrite, effective config, and reload staying on the maintenance path."""

import asyncio
import json
import random

import pytest

from app.worker_agent.channel_switch import apply_channel_switch, global_config_path
from app.worker_agent.connection import AgentConfig, WorkerConnectionManager, handle_command
from app.worker_agent.jsonc import loads_jsonc
from app.worker_agent.supervisor import RuntimeSupervisor
from app.worker_gateway.protocol import envelope


def _config(path, *, gateway="http://127.0.0.1:19998", mcp="http://127.0.0.1:19997/mcp"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "provider": {
                    "other": {"options": {"baseURL": "http://keep"}},
                    "nexus-gateway": {
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {"baseURL": gateway, "apiKey": "gw-secret"},
                    },
                },
                "mcp": {
                    "nexus-library": {
                        "type": "remote",
                        "url": mcp,
                        "headers": {"Authorization": "Bearer mcp-secret"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_global_config_path_prefers_opencode_config(tmp_path):
    target = tmp_path / "opencode.jsonc"
    assert global_config_path({"OPENCODE_CONFIG": str(target), "HOME": "/tmp"}) == target


def test_switch_writes_direct_urls_and_returns_effective_override(tmp_path):
    path = tmp_path / "opencode.json"
    _config(path)
    reloads: list[int] = []
    probed: list[str] = []

    def read_effective():
        written = json.loads(path.read_text(encoding="utf-8"))
        written["provider"]["nexus-gateway"]["options"]["baseURL"] = "http://127.0.0.1:19999"
        return written

    result = apply_channel_switch(
        workspace=str(tmp_path),
        port=4096,
        config_path=path,
        environ={
            "NEXUS_REMOTE_GATEWAY_URL": "http://10.0.0.8:8110",
            "NEXUS_MCP_RESOURCE_URL": "http://10.0.0.8:8201/mcp",
        },
        probe=lambda url: probed.append(url) or True,
        reload=lambda: reloads.append(1),
        read_effective=read_effective,
    )
    stored = json.loads(path.read_text(encoding="utf-8"))
    options = stored["provider"]["nexus-gateway"]["options"]
    library = stored["mcp"]["nexus-library"]
    assert options["baseURL"] == "http://10.0.0.8:8110"
    assert options["apiKey"] == "gw-secret"
    assert stored["provider"]["other"]["options"]["baseURL"] == "http://keep"
    assert library["url"] == "http://10.0.0.8:8201/mcp"
    assert library["headers"]["Authorization"] == "Bearer mcp-secret"
    assert result == {
        "gateway_url": "http://127.0.0.1:19999",
        "mcp_url": "http://10.0.0.8:8201/mcp",
        "reloaded": True,
        "verified": False,
    }
    assert reloads == [1]
    assert probed == ["http://10.0.0.8:8110", "http://10.0.0.8:8201/mcp"]
    assert (path.parent / "opencode.json.nexus-backup").is_file()


def test_switch_merges_existing_jsonc(tmp_path):
    path = tmp_path / "opencode.json"
    _config(path)
    original = path.read_text(encoding="utf-8")
    jsonc = path.with_suffix(".jsonc")
    jsonc.write_text("// note\n" + original, encoding="utf-8")
    apply_channel_switch(
        workspace=str(tmp_path),
        port=4096,
        config_path=path,
        environ={
            "NEXUS_REMOTE_GATEWAY_URL": "http://10.0.0.8:8110",
            "NEXUS_MCP_RESOURCE_URL": "http://10.0.0.8:8201/mcp",
        },
        probe=lambda _url: True,
        reload=lambda: None,
        read_effective=lambda: loads_jsonc(jsonc.read_text(encoding="utf-8")),
    )
    stored = json.loads(jsonc.read_text(encoding="utf-8"))
    assert stored["provider"]["nexus-gateway"]["options"]["baseURL"] == "http://10.0.0.8:8110"
    assert stored["provider"]["nexus-gateway"]["options"]["apiKey"] == "gw-secret"
    assert path.read_text(encoding="utf-8") == original


def test_switch_stops_before_reload_when_probe_or_config_fails(tmp_path):
    path = tmp_path / "opencode.json"
    _config(path)
    original = path.read_text(encoding="utf-8")
    reloads: list[int] = []

    with pytest.raises(RuntimeError, match="探测失败"):
        apply_channel_switch(
            workspace=str(tmp_path),
            port=4096,
            config_path=path,
            probe=lambda _url: False,
            reload=lambda: reloads.append(1),
        )
    assert path.read_text(encoding="utf-8") == original

    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="nexus-gateway"):
        apply_channel_switch(
            workspace=str(tmp_path),
            port=4096,
            config_path=path,
            probe=lambda _url: True,
            reload=lambda: reloads.append(1),
        )
    assert reloads == []


def test_switch_command_ignores_payload_urls(monkeypatch):
    captured: dict[str, object] = {}

    def fake_apply(**kwargs):
        captured.update(kwargs)
        return {
            "gateway_url": "http://10.0.0.8:8110",
            "mcp_url": "http://10.0.0.8:8201/mcp",
            "reloaded": True,
            "verified": True,
        }

    monkeypatch.setattr("app.worker_agent.channel_switch.apply_channel_switch", fake_apply)
    message = envelope(
        request_id="switch-1",
        worker_instance_id="wrk",
        environment_id=1,
        session_id=None,
        message_type="command",
        payload={
            "name": "switch_channel",
            "data": {
                "workspace": "/srv/work",
                "port": 4096,
                "gateway_url": "http://evil",
                "mcp_url": "http://evil/mcp",
            },
        },
    )
    result, error = handle_command(
            message,
            RuntimeSupervisor(),
            workspace_root="/srv/work",
            opencode_url="http://127.0.0.1:4096",
        )
    assert error is None
    assert result["reloaded"] is True
    assert captured["workspace"] == "/srv/work"
    assert captured["port"] == 4096
    assert set(captured) == {"workspace", "port", "gate"}


def test_disconnect_does_not_reload(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(
        "app.worker_agent.channel_switch.restart_local_opencode",
        lambda *_args, **_kwargs: calls.append(1),
    )
    supervisor = RuntimeSupervisor()

    async def scenario():
        async def connect():
            raise ConnectionError("closed")

        manager = WorkerConnectionManager(
            AgentConfig(
                gateway_url="ws://nexus.internal/api/worker-gateway/connect",
                worker_instance_id="wrk_agent",
                environment_id=7,
                token="wtk_test",
            ),
            supervisor=supervisor,
            rng=random.Random(0),
            sleep=lambda _seconds: asyncio.sleep(0),
        )
        await manager.run(connect, asyncio.Event(), max_sessions=1)

    asyncio.run(scenario())
    assert calls == []
    assert supervisor.restart_count == 0


def test_switch_same_config_already_effective_does_not_write_or_reload(tmp_path, monkeypatch):
    desired_gateway = "http://10.0.0.8:8110"
    desired_mcp = "http://10.0.0.8:8201/mcp"
    path = tmp_path / "opencode.json"
    _config(path, gateway=desired_gateway, mcp=desired_mcp)
    before = path.stat().st_mtime_ns
    reloads: list[int] = []

    def fail_native(*_args, **_kwargs):
        raise AssertionError("already-effective switch must not inspect/restart native")

    monkeypatch.setattr("app.worker_agent.channel_switch.assert_native_idle", fail_native)
    result = apply_channel_switch(
        workspace=str(tmp_path),
        port=4096,
        config_path=path,
        environ={
            "NEXUS_REMOTE_GATEWAY_URL": desired_gateway,
            "NEXUS_MCP_RESOURCE_URL": desired_mcp,
        },
        probe=lambda _url: True,
        reload=lambda: reloads.append(1),
        read_effective=lambda: json.loads(path.read_text(encoding="utf-8")),
    )
    assert path.stat().st_mtime_ns == before
    assert reloads == []
    assert result["reloaded"] is False
    assert result["verified"] is True


def test_switch_file_matches_but_running_config_is_old_reloads(tmp_path):
    desired_gateway = "http://10.0.0.8:8110"
    desired_mcp = "http://10.0.0.8:8201/mcp"
    path = tmp_path / "opencode.json"
    _config(path, gateway=desired_gateway, mcp=desired_mcp)
    old = json.loads(path.read_text(encoding="utf-8"))
    old["provider"]["nexus-gateway"]["options"]["baseURL"] = "http://127.0.0.1:19998"
    calls = {"read": 0, "reload": 0}

    def read_effective():
        calls["read"] += 1
        return old if calls["read"] == 1 else json.loads(path.read_text(encoding="utf-8"))

    def reload():
        calls["reload"] += 1

    result = apply_channel_switch(
        workspace=str(tmp_path),
        port=4096,
        config_path=path,
        environ={
            "NEXUS_REMOTE_GATEWAY_URL": desired_gateway,
            "NEXUS_MCP_RESOURCE_URL": desired_mcp,
        },
        probe=lambda _url: True,
        reload=reload,
        read_effective=read_effective,
    )
    assert calls["reload"] == 1
    assert result["reloaded"] is True
    assert result["verified"] is True


def test_switch_busy_or_unknown_never_writes_or_reloads(tmp_path, monkeypatch):
    path = tmp_path / "opencode.json"
    _config(path)
    original = path.read_text(encoding="utf-8")
    reloads: list[int] = []

    def busy(*_args, **_kwargs):
        raise RuntimeError("OpenCode 存在正在执行的 Session")

    monkeypatch.setattr("app.worker_agent.channel_switch.assert_native_idle", busy)
    with pytest.raises(RuntimeError, match="正在执行"):
        apply_channel_switch(
            workspace=str(tmp_path),
            port=4096,
            config_path=path,
            environ={
                "NEXUS_REMOTE_GATEWAY_URL": "http://10.0.0.8:8110",
                "NEXUS_MCP_RESOURCE_URL": "http://10.0.0.8:8201/mcp",
            },
            probe=lambda _url: True,
            reload=lambda: reloads.append(1),
            read_effective=lambda: json.loads(path.read_text(encoding="utf-8")),
        )
    assert path.read_text(encoding="utf-8") == original
    assert reloads == []


def test_switch_stale_gate_blocks_before_write(tmp_path):
    path = tmp_path / "opencode.json"
    _config(path)
    original = path.read_text(encoding="utf-8")
    reloads: list[int] = []

    def stale():
        raise RuntimeError("维护租约已失效")

    with pytest.raises(RuntimeError, match="租约"):
        apply_channel_switch(
            workspace=str(tmp_path),
            port=4096,
            config_path=path,
            environ={
                "NEXUS_REMOTE_GATEWAY_URL": "http://10.0.0.8:8110",
                "NEXUS_MCP_RESOURCE_URL": "http://10.0.0.8:8201/mcp",
            },
            probe=lambda _url: True,
            reload=lambda: reloads.append(1),
            read_effective=lambda: json.loads(path.read_text(encoding="utf-8")),
            gate=stale,
        )
    assert path.read_text(encoding="utf-8") == original
    assert reloads == []
