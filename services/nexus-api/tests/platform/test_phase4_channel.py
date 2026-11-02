"""远程部署顺序、内网地址校验、Worker Wheel 隔离与默认端口契约。"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.config.settings import get_settings
from app.database import SessionLocal, init_database
from app.models import AgentWorker, OpenCodeExecutionEnvironment, WorkerMaintenanceOperation
from app.runtime.environment import ExecutionEnvironmentRuntime
from app.runtime.maintenance import OperationLease, submit_operation
from app.runtime.opencode_model import gateway_model
from app.runtime.remote_provisioning import RemoteProvisioner, agent_wheel
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
    monkeypatch.setattr(
        settings, "worker_agent_gateway_url", "ws://10.0.0.8:8100/api/worker-gateway/connect"
    )
    return settings


async def _employee(**overrides) -> tuple[str, int]:
    await init_database()
    employee_id = "p4-" + uuid4().hex[:10]
    async with SessionLocal() as db:
        db.add(AgentWorker(id=employee_id, name=employee_id, role="build", current_task_count=0))
        await db.flush()
        values = dict(
            employee_id=employee_id,
            mode="LINUX_REMOTE",
            name="Linux",
            host="10.0.0.9",
            port=22,
            username="root",
            ssh_password="test-ssh-secret",
            workspace_root="/srv/project",
            opencode_path="/usr/local/bin/opencode",
            opencode_server_port=4096,
            status="UNKNOWN",
            status_detail={},
        )
        values.update(overrides)
        row = OpenCodeExecutionEnvironment(**values)
        db.add(row)
        await db.commit()
        return employee_id, row.id


async def _reserve(employee_id, action):
    async with SessionLocal() as db:
        environment = await db.scalar(
            select(OpenCodeExecutionEnvironment).where(
                OpenCodeExecutionEnvironment.employee_id == employee_id
            )
        )
        operation = await submit_operation(db, environment.id, action, requested_by="test")
        now = datetime.now(UTC)
        operation.state = "RUNNING"
        operation.stage = "running"
        operation.lease_owner = "test-lease"
        operation.lease_until = now + timedelta(hours=1)
        operation.started_at = now
        await db.commit()
        await db.refresh(operation)
        environment_id = environment.id
    async with SessionLocal() as db:
        environment = await db.get(OpenCodeExecutionEnvironment, environment_id)
        db.expunge(environment)
    lease = OperationLease(operation.id, operation.attempt, "test-lease", 3600)
    return environment, operation, lease


@pytest.mark.asyncio
async def test_deploy_rejects_loopback_before_ssh(monkeypatch):
    monkeypatch.setattr(get_settings(), "mcp_transport", "streamable-http")
    monkeypatch.setattr(get_settings(), "gateway_openai_base", "http://127.0.0.1:8110")
    employee_id, _environment_id = await _employee()
    environment, operation, lease = await _reserve(employee_id, "deploy")
    called = False

    async def opened(self, _environment):
        nonlocal called
        called = True
        raise AssertionError("ssh")

    monkeypatch.setattr(ExecutionEnvironmentRuntime, "open_session", opened)
    events = []

    async def emit(event):
        events.append(event)

    await RemoteProvisioner(environment, operation, lease=lease).run(emit)
    assert called is False
    async with SessionLocal() as db:
        stored = await db.get(WorkerMaintenanceOperation, operation.id)
    assert stored is not None and stored.state == "FAILED"
    assert stored.active_environment_id is None
    assert "内网" in str(stored.error_message)


@pytest.mark.parametrize("failure", [None, "gateway", "agent"])
@pytest.mark.asyncio
async def test_server_deployment_order_and_failure_do_not_report_early_success(
    monkeypatch, failure
):
    _direct(monkeypatch)
    employee_id, _ = await _employee()
    environment, operation, lease = await _reserve(employee_id, "deploy")
    worker = RemoteProvisioner(environment, operation, lease=lease)
    ssh = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(worker.runtime, "open_session", AsyncMock(return_value=ssh))
    order = []

    def stage(name):
        async def execute(*_args, **_kwargs):
            order.append(name)
            if failure == name:
                raise RuntimeError("fixture failure: " + name)

        return execute

    monkeypatch.setattr("app.runtime.remote_provisioning.prepare_python", stage("probe"))
    worker._native_idle = AsyncMock(return_value=True)
    worker._install = stage("runtime")
    worker._probe_gateway = stage("gateway")
    worker._install_worker = stage("agent")
    worker._converge_readiness = stage("converge")
    worker._finish = AsyncMock(return_value=True)
    events = []

    async def emit(event):
        events.append(event)

    await worker.run(emit)
    assert order == (
        ["probe", "runtime", "gateway"]
        if failure == "gateway"
        else ["probe", "runtime", "gateway", "agent"]
        if failure == "agent"
        else ["probe", "runtime", "gateway", "agent", "converge"]
    )
    assert events[-1]["success"] is (failure is None)
    ssh.close.assert_awaited_once()


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
        worker_instance_id="wrk",
        environment_id=1,
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
    assert "effective_model = gateway_model(raw_model)" in source


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
