"""远程部署顺序、内网地址校验、Worker Wheel 隔离与默认端口契约。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config.settings import get_settings
from app.runtime.opencode_model import gateway_model
from app.runtime.remote_provisioning import agent_wheel
from app.worker_agent.connection import handle_command
from app.worker_agent.supervisor import RuntimeSupervisor
from app.worker_gateway.protocol import envelope

GATEWAY = "http://10.0.0.8:8110"
MCP = "http://10.0.0.8:8201/mcp"


def _direct(monkeypatch):
    settings = get_settings()
    redis_url = os.environ.get("ASTRA_REDIS_URL") or os.environ.get("ASTRA_TEST_REDIS_URL")
    if redis_url:
        monkeypatch.setattr(settings, "redis_url", redis_url)
    monkeypatch.setattr(settings, "mcp_transport", "streamable-http")
    monkeypatch.setattr(settings, "gateway_openai_base", GATEWAY)
    monkeypatch.setattr(settings, "mcp_resource_server_url", MCP)
    monkeypatch.setattr(settings, "worker_agent_gateway_url", "ws://10.0.0.8:8100/api/worker-gateway/connect")
    return settings


def test_restart_command_restarts_local_opencode(monkeypatch, tmp_path):
    seen = {}
    sup = RuntimeSupervisor(enabled=True, workspace_root=str(tmp_path), port=4096)

    def restart(*, gate=None, timeout=30.0):
        seen["gate"] = gate

    monkeypatch.setattr(sup, "restart_from_thread", restart)
    monkeypatch.setattr(
        "app.worker_agent.inspection.inspect_runtime",
        lambda *_a, **_kw: {"opencode": {"status": "healthy"}},
    )
    message = envelope(
        request_id="restart-1",
        agent_worker_id="wrk",
        session_id=None,
        message_type="command",
        payload={"name": "restart_opencode", "data": {"workspace": str(tmp_path), "port": 4096}},
    )

    def gate():
        return None

    result, error = handle_command(
        message,
        sup,
        workspace_root=str(tmp_path),
        opencode_url="http://127.0.0.1:4096",
        gate=gate,
    )
    assert error is None
    assert result["restarted"] is True
    assert result["snapshot"]["opencode"]["status"] == "healthy"
    assert seen["gate"] is gate


def test_linux_remote_model_stays_on_the_gateway():
    assert gateway_model("deepseek-flash") == "nexus-gateway/deepseek-flash"
    assert gateway_model("opencode/glm-5.1") == "nexus-gateway/glm-5.1"
    source = Path("app/runtime/lifecycle.py").read_text(encoding="utf-8")
    assert "gateway_model(worker.model_name or get_settings().opencode_model)" in source


def repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "ops" / "deploy" / "install_nexus.sh").is_file():
            return parent
    raise AssertionError("找不到仓库根目录")


def test_remote_port_default_is_14096_and_llm_wiki_is_a_lan_server():
    root = repo_root()
    script = (root / "ops/deploy/install_nexus.sh").read_text(encoding="utf-8")
    from app.config.settings import Settings

    assert Settings.model_fields["opencode_server_port"].default == 14096
    assert "4296" not in script
    assert "ASTRA_LLM_WIKI_ENABLED" in (root / "ops/deploy/deploy.py").read_text()
    assert "LLM_WIKI_HOST" not in script
    example = (root / "ops/env/.env.example").read_text(encoding="utf-8")
    assert "4296" not in example
    guide = (root / "ops/deploy/DEPLOYMENT_GUIDE.md").read_text(encoding="utf-8")
    employees = (root / "docs/deploy/REMOTE_EMPLOYEES.md").read_text(encoding="utf-8")
    assert "4296" not in guide
    assert "4296" not in employees
    platform = Path(__file__).resolve().parents[2]
    assert "llm_wiki_api_base_url" in (platform / "app/llm_wiki.py").read_text(encoding="utf-8")


def _venv_python(tmp_path):
    try:
        probe = subprocess.run(["python3", "-c", "import ensurepip"], capture_output=True)
    except FileNotFoundError:
        pytest.skip("没有 python3")
    if probe.returncode != 0:
        pytest.skip("没有 ensurepip")
    venv = tmp_path / "pip-venv"
    created = subprocess.run(["python3", "-m", "venv", str(venv)], capture_output=True, text=True)
    if created.returncode != 0:
        pytest.skip("python3 -m venv 不可用")
    python = venv / "bin" / "python"
    if not python.is_file():
        pytest.skip("venv 没有 python")
    return python


def test_agent_wheel_imports_without_the_backend(tmp_path):
    filename, payload = agent_wheel()
    wheel = tmp_path / filename
    wheel.write_bytes(payload)
    target = tmp_path / "site"
    subprocess.check_call(
        [
            str(_venv_python(tmp_path)),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(target),
            str(wheel),
        ],
        stdout=subprocess.DEVNULL,
    )
    script = (
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import app.worker_agent.connection\n"
        "import app.worker_agent.local_opencode\n"
        "import app.worker_agent.supervisor\n"
        "import app.worker_agent.__main__\n"
        "banned = [\n"
        "    'sqlalchemy', 'fastapi', 'pydantic_settings',\n"
        "    'app.config.settings', 'app.worker_gateway.service',\n"
        "    'app.worker_gateway.data_channel',\n"
        "]\n"
        "loaded = [name for name in banned if name in sys.modules]\n"
        "assert not loaded, loaded\n"
    )
    subprocess.check_call([sys.executable, "-c", script, str(target)])
