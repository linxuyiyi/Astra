"""可选真实 OpenCode 1.18.35 验证；始终使用临时目录和独立端口。"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys

import pytest

from app.worker_agent import channel_switch, process_manager


@pytest.mark.skipif(
    sys.platform != "linux" or not os.environ.get("NEXUS_TEST_OPENCODE_BIN"),
    reason="需 Linux 上显式配置独立 OpenCode 测试可执行文件",
)
def test_real_opencode_config_and_managed_restart(tmp_path):
    binary = os.environ["NEXUS_TEST_OPENCODE_BIN"]
    container_mode = bool(os.environ.get("NEXUS_TEST_NO_SYSTEMD"))
    if container_mode:
        assert not process_manager.has_systemd()
    assert shutil.which(binary)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_root = tmp_path / "config"
    config_root.mkdir()
    config = config_root / "opencode.jsonc"
    gateway = "http://127.0.0.1:1/v1"
    mcp = "http://127.0.0.1:1/mcp"
    config.write_text(json.dumps({
        "model": "nexus-gateway/qa",
        "small_model": "nexus-gateway/qa",
        "enabled_providers": ["nexus-gateway"],
        "disabled_providers": ["opencode"],
        "provider": {
            "nexus-gateway": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": gateway, "apiKey": "qa-not-a-real-key"},
                "models": {"qa": {"name": "qa"}},
            },
        },
        "mcp": {"nexus-library": {"type": "remote", "url": mcp, "enabled": False}},
    }))
    record = tmp_path / "opencode-process.json"
    log = tmp_path / "opencode-process.log"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        "PATH": os.environ["PATH"], "HOME": str(tmp_path),
        "OPENCODE_CONFIG": str(config), "OPENCODE_CONFIG_DIR": str(config_root),
        "XDG_DATA_HOME": str(tmp_path / "data"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"), "XDG_CONFIG_HOME": str(tmp_path / "global"),
        "NEXUS_WORKER_TOKEN": "qa-must-not-reach-opencode",
        "NEXUS_WORKER_OPENCODE_BIN": binary,
        "NEXUS_WORKER_OPENCODE_PROCESS_RECORD": str(record),
        "NEXUS_WORKER_OPENCODE_UNIT": "nexus-qa.service" if container_mode else "",
    }
    command = [binary, "serve", "--hostname", "127.0.0.1", "--port", str(port)]
    try:
        process_manager.manage_process(
            record, command=command, workspace=str(workspace), log_path=log, environ=env, opencode=True,
        )
        channel_switch._wait_until_healthy(port)
        effective = channel_switch.read_effective_config(port, str(workspace))
        assert effective["provider"]["nexus-gateway"]["options"]["baseURL"] == gateway
        assert effective["mcp"]["nexus-library"]["url"] == mcp
        assert effective["model"] == effective["small_model"] == "nexus-gateway/qa"
        old_identity = json.loads(record.read_text())["identity"]
        assert process_manager.managed_running(record)
        process_manager.manage_process(
            record, command=command, workspace=str(workspace), log_path=log, environ=env, opencode=True,
        )
        assert json.loads(record.read_text())["identity"] == old_identity
        channel_switch.restart_local_opencode(str(workspace), port, environ=env)
        assert json.loads(record.read_text())["identity"]["pid"] != old_identity["pid"]
        after = channel_switch.read_effective_config(port, str(workspace))
        assert after["provider"]["nexus-gateway"]["options"]["baseURL"] == gateway
        assert after["mcp"]["nexus-library"]["url"] == mcp
        assert process_manager.managed_running(record)
    finally:
        if record.exists():
            process_manager._stop(record)
