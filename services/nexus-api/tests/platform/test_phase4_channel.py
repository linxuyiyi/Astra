"""Phase 4: worker-only remote execution, intranet addresses, and install mark."""

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config.settings import get_settings
from app.database import SessionLocal, init_database
from app.models import AgentWorker, OpenCodeExecutionEnvironment
from app.runtime.environment import ExecutionEnvironmentRuntime
from app.runtime.maintenance import OperationLease, submit_operation
from app.runtime.opencode_model import gateway_model
from app.runtime.remote_provisioning import RemoteProvisioner, agent_wheel
from app.runtime.worker_channel import complete_worker_mark, read_worker_channel
from app.worker_agent.connection import handle_command
from app.worker_agent.supervisor import RuntimeSupervisor
from app.worker_gateway.protocol import envelope
from tests.platform.test_worker_gateway import authenticate


@pytest.fixture(autouse=True)
def healthy_observation(monkeypatch):
    from app.domain import EnvironmentStatus

    async def fake(environment_id, *, persist=False, **_kwargs):
        now = datetime.now(UTC)
        return {
            "stale": False,
            "status": EnvironmentStatus.AVAILABLE,
            "desired": {},
            "observation": {"source": "test"},
            "equipment": [],
            "repository": None,
            "checked_at": now,
            "values": {
                "status": EnvironmentStatus.AVAILABLE,
                "last_checked_at": now,
                "status_detail": {},
            },
        }

    monkeypatch.setattr("app.runtime.maintenance.observe_environment", fake)


GATEWAY = "http://10.0.0.8:8110"
MCP = "http://10.0.0.8:8201/mcp"
RAW_KEY = "agw_phase4-install-key"


def _direct(monkeypatch):
    settings = get_settings()
    redis_url = os.environ.get("ASTRA_REDIS_URL") or os.environ.get("ASTRA_TEST_REDIS_URL")
    if redis_url:
        monkeypatch.setattr(settings, "redis_url", redis_url)
    monkeypatch.setattr(settings, "mcp_transport", "streamable-http")
    monkeypatch.setattr(settings, "remote_gateway_local_url", GATEWAY)
    monkeypatch.setattr(settings, "mcp_resource_server_url", MCP)
    monkeypatch.setattr(settings, "worker_agent_gateway_url", "ws://10.0.0.8:8100/api/worker-gateway/connect")
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
        operation.state = "PREFLIGHT"
        operation.stage = "preflight"
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


async def _complete(gateway, frame, epoch, result):
    reply = envelope(
        request_id=frame["request_id"],
        worker_instance_id=frame["worker_instance_id"],
        environment_id=frame["environment_id"],
        session_id=frame.get("session_id"),
        message_type="response",
        payload={"stage": "result", "ok": True, "result": result, "error_code": None},
    )
    reply.epoch = epoch
    async with SessionLocal() as session:
        await gateway.apply_response(
            session,
            worker_instance_id=frame["worker_instance_id"],
            epoch=epoch,
            message=reply,
        )
    await gateway.bus.publish_response(frame["request_id"], reply.to_dict())


@pytest.mark.asyncio
async def test_deploy_rejects_loopback_before_ssh(monkeypatch):
    monkeypatch.setattr(get_settings(), "mcp_transport", "streamable-http")
    monkeypatch.setattr(get_settings(), "remote_gateway_local_url", "http://127.0.0.1:8110")
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
    assert events[-1]["success"] is False
    assert "内网" in events[-1]["message"]


@pytest.mark.asyncio
async def test_failed_switch_does_not_mark_worker(monkeypatch):
    _direct(monkeypatch)
    employee_id, environment_id = await _employee()
    _environment, operation, lease = await _reserve(employee_id, "deploy")

    def fake_switch(*_args):
        async def switch():
            return {"reloaded": False, "verified": False}

        return switch

    monkeypatch.setattr("app.runtime.worker_channel._worker_switch", fake_switch)
    with pytest.raises(HTTPException) as error:
        async with SessionLocal() as db:
            await complete_worker_mark(db, environment_id, operation.id, lease=lease)
    assert error.value.status_code == 409
    async with SessionLocal() as db:
        assert await read_worker_channel(db, environment_id) == "ssh"


@pytest.mark.asyncio
async def test_online_restart_is_owned_by_nexus_over_ssh(monkeypatch):
    from unittest.mock import AsyncMock

    _direct(monkeypatch)
    employee_id, _environment_id = await _employee()
    environment, operation, lease = await _reserve(employee_id, "restart")
    worker = RemoteProvisioner(environment, operation, lease=lease)
    ssh = SimpleNamespace(close=AsyncMock())
    opened = AsyncMock(return_value=ssh)
    monkeypatch.setattr(worker.runtime, "open_session", opened)
    worker._acquire_install_lock = AsyncMock()
    worker._native_idle = AsyncMock(return_value=True)
    worker._restart_processes = AsyncMock()
    worker._finish = AsyncMock(return_value=True)
    events = []

    async def emit(event):
        events.append(event)

    await worker.run(emit)
    opened.assert_awaited_once()
    worker._restart_processes.assert_awaited_once()
    assert events[-1]["success"] is True


@pytest.mark.asyncio
async def test_restart_requires_ssh_credentials_even_when_worker_is_online(monkeypatch):
    _direct(monkeypatch)
    employee_id, _environment_id = await _employee(ssh_password=None)
    with pytest.raises(HTTPException, match="SSH"):
        await _reserve(employee_id, "restart")


@pytest.mark.parametrize("failure", [None, "gateway", "agent"])
async def test_server_deployment_order_and_failure_do_not_report_early_success(monkeypatch, failure):
    from unittest.mock import AsyncMock

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
    worker._mark_inside_operation = stage("online")
    worker._observe_capability = AsyncMock(return_value={})
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
        else ["probe", "runtime", "gateway", "agent", "online"]
    )
    assert events[-1]["success"] is (failure is None)
    ssh.close.assert_awaited_once()


def test_restart_command_restarts_local_opencode(monkeypatch):
    seen = {}

    def restart(workspace, port, *, environ=None, gate=None):
        seen["workspace"] = workspace
        seen["port"] = port
        seen["gate"] = gate

    monkeypatch.setattr("app.worker_agent.channel_switch.restart_local_opencode", restart)
    message = envelope(
        request_id="restart-1",
        worker_instance_id="wrk",
        environment_id=1,
        session_id=None,
        message_type="command",
        payload={"name": "restart_opencode", "data": {"workspace": "/srv/project", "port": 4096}},
    )

    def gate():
        return None

    result, error = handle_command(
        message,
        RuntimeSupervisor(),
        workspace_root="/srv/project",
        opencode_url="http://127.0.0.1:4096",
        gate=gate,
    )
    assert error is None
    assert result == {"restarted": True}
    assert seen["workspace"] == "/srv/project"
    assert seen["port"] == 4096
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
    # llm_wiki 服务器版本按局域网地址接入，不再有 SSH 隧道和主机模式。
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


async def authenticate_existing(gateway, environment_id):
    from app.worker_gateway.service import EnrollResult

    async with SessionLocal() as session:
        enrolled = await gateway.enroll(session, environment_id)
    assert isinstance(enrolled, EnrollResult)
    auth = await authenticate(
        gateway, environment_id, enrolled.worker_instance_id, enrolled.token, "auth-online"
    )
    assert await gateway.bus.set_owner(auth.worker_instance_id, gateway.owner_id, auth.epoch)
    queues = await gateway.bus.bind(auth.worker_instance_id, epoch=auth.epoch, owner_id=gateway.owner_id)
    assert queues is not None
    return enrolled, auth
