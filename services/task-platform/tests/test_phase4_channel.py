"""Phase 4: worker-only remote execution, intranet addresses, and install mark."""

import asyncio
import io
import json
import os
import subprocess
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config.settings import get_settings
from app.database import SessionLocal, init_database
from app.gateway.models import GatewayKey, GatewayPool, GatewayRoute
from app.gateway.security import gateway_key_digest
from app.models import DigitalEmployee, OpenCodeExecutionEnvironment, WorkerMaintenanceOperation
from app.runtime.environment import ExecutionEnvironmentRuntime
from app.runtime.maintenance import OperationLease, submit_operation
from app.runtime.opencode_model import gateway_model
from app.runtime.remote_provisioning import RemoteProvisioner, agent_python, agent_wheel
from app.runtime.worker_channel import complete_worker_mark, read_worker_channel
from app.runtime.workspace_files import upload, validate_path
from app.worker_agent.connection import handle_command
from app.worker_agent.files import execute_file_command
from app.worker_agent.supervisor import RuntimeSupervisor
from app.worker_gateway.data_channel import DataChannel
from app.worker_gateway.protocol import envelope
from tests.test_worker_gateway import authenticate, gateway_for


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
    monkeypatch.setattr(
        settings, "worker_agent_gateway_url", "ws://10.0.0.8:8100/api/worker-gateway/connect"
    )
    return settings


async def _employee(**overrides) -> tuple[str, int]:
    await init_database()
    employee_id = "p4-" + uuid4().hex[:10]
    async with SessionLocal() as db:
        db.add(DigitalEmployee(id=employee_id, name=employee_id, role="build", current_task_count=0))
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
            return {"reloaded": False}

        return switch

    monkeypatch.setattr("app.runtime.worker_channel._worker_switch", fake_switch)
    with pytest.raises(HTTPException) as error:
        async with SessionLocal() as db:
            await complete_worker_mark(db, environment_id, operation.id, lease=lease)
    assert error.value.status_code == 409
    async with SessionLocal() as db:
        assert await read_worker_channel(db, environment_id) == "ssh"


@pytest.mark.asyncio
async def test_online_restart_does_not_open_ssh(monkeypatch):
    _direct(monkeypatch)
    gateway = gateway_for()
    monkeypatch.setattr("app.worker_gateway.service.get_gateway", lambda: gateway)
    monkeypatch.setattr("app.runtime.worker_client.get_gateway", lambda: gateway)
    employee_id, environment_id = await _employee()
    enrolled, auth = await authenticate_existing(gateway, environment_id)
    queues = gateway.bus.bound[enrolled.worker_instance_id]

    async def answer():
        status = await queues.wait_frame()
        assert status["payload"]["name"] == "http"
        assert status["payload"]["data"]["path"] == "/session/status"
        async with SessionLocal() as session:
            await DataChannel(gateway).commit_upload(
                session,
                request_id=status["request_id"],
                worker_instance_id=enrolled.worker_instance_id,
                epoch=auth.epoch,
                upload_id="status",
                http_status=200,
                headers={},
                content_type="application/json",
                chunk=json.dumps({"ses_idle": {"type": "idle"}}),
                complete=True,
            )
        frame = await queues.wait_frame()
        assert frame["payload"]["name"] == "restart_opencode"
        await _complete(gateway, frame, auth.epoch, {"restarted": True})
        health = await queues.wait_frame()
        assert health["payload"]["data"]["path"] == "/global/health"
        async with SessionLocal() as session:
            await DataChannel(gateway).commit_upload(
                session,
                request_id=health["request_id"],
                worker_instance_id=enrolled.worker_instance_id,
                epoch=auth.epoch,
                upload_id="health",
                http_status=200,
                headers={},
                content_type="application/json",
                chunk=json.dumps({"healthy": True, "version": "1.18.35"}),
                complete=True,
            )

    running = asyncio.create_task(answer())
    environment, operation, lease = await _reserve(employee_id, "restart")
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
    await running
    assert called is False
    assert events[-1]["success"] is True


@pytest.mark.asyncio
async def test_online_restart_does_not_require_ssh_password(monkeypatch):
    _direct(monkeypatch)
    gateway = gateway_for()
    monkeypatch.setattr("app.worker_gateway.service.get_gateway", lambda: gateway)
    monkeypatch.setattr("app.runtime.worker_client.get_gateway", lambda: gateway)
    employee_id, environment_id = await _employee(ssh_password=None)
    await authenticate_existing(gateway, environment_id)
    environment, operation, lease = await _reserve(employee_id, "restart")
    assert operation.action == "restart"
    assert environment.ssh_password is None


@pytest.mark.asyncio
async def test_install_marks_inside_the_operation_and_serves_files(tmp_path, monkeypatch):
    _direct(monkeypatch)
    gateway = gateway_for()
    monkeypatch.setattr("app.worker_gateway.service.get_gateway", lambda: gateway)
    monkeypatch.setattr("app.runtime.worker_client.get_gateway", lambda: gateway)

    async def nested(*_args, **_kwargs):
        raise AssertionError("deploy 不应再取一把维护锁")

    monkeypatch.setattr("app.runtime.worker_channel.mark_worker_channel", nested)
    workspace = str(tmp_path)
    employee_id, environment_id = await _employee(workspace_root=workspace)
    suffix = uuid4().hex[:8]
    async with SessionLocal() as db:
        pool_id = "pool-" + suffix
        db.add(GatewayPool(id=pool_id, name="pool-" + suffix))
        db.add(GatewayRoute(id="route-" + suffix, model="deepseek-flash", pool_id=pool_id, enabled=True))
        db.add(
            GatewayKey(
                id="key-" + suffix,
                name="employee",
                owner_id=employee_id,
                key_prefix=RAW_KEY[:12],
                key_digest=gateway_key_digest(RAW_KEY),
                enabled=True,
                allowed_models=["deepseek-flash"],
            )
        )
        await db.commit()
    environment, operation, lease = await _reserve(employee_id, "deploy")

    files: dict[str, bytes] = {}
    commands: list[str] = []
    seen: list[str] = []
    holder: dict = {}

    class Channel:
        async def readline(self):
            return b""

        async def read(self, _size=-1):
            return b""

        async def wait(self):
            return 0

        async def close(self):
            return None

        stdout = None
        stderr = None

    channel = Channel()
    channel.stdout = channel
    channel.stderr = channel

    class SSH:
        async def run_command(self, command):
            commands.append(command)
            if "pip install" in command:
                assert any(path.endswith(".whl") for path in files)
                assert not any(path.endswith(".env") for path in files)
            if "session/status" in command:
                return 0, "", ""
            if "$HOME" in command:
                return 0, "/root\n/root/.config\n", ""
            if command.strip() == "id -u":
                return 0, "0\n", ""
            if command.startswith("node ") or " node " in f" {command}":
                return 0, "模型中转认证与模型路由正常\n", ""
            if "systemctl restart" in command:
                await bring_installed_worker_online()
                return 0, "", ""
            return 0, "", ""

        async def read_file(self, path):
            if path in files:
                return files[path]
            if path.endswith("opencode.json") or path.endswith("opencode.jsonc"):
                return json.dumps(
                    {"provider": {"nexus-gateway": {"options": {"apiKey": RAW_KEY}}}}
                ).encode()
            return None

        async def write_file(self, path, data, mode=0o644):
            files[path] = data

        async def open_exec(self, _command):
            return channel

        async def close(self):
            return None

    ssh_calls = []

    async def opened(self, _environment):
        ssh_calls.append("ssh")
        return SSH()

    monkeypatch.setattr(ExecutionEnvironmentRuntime, "open_session", opened)

    async def bring_installed_worker_online():
        env = next(value.decode() for key, value in files.items() if key.endswith(".env"))
        values = dict(line.split("=", 1) for line in env.splitlines() if "=" in line)
        auth = await authenticate(
            gateway,
            environment_id,
            values["NEXUS_WORKER_INSTANCE_ID"],
            values["NEXUS_WORKER_TOKEN"],
            "auth-install",
        )
        assert await gateway.bus.set_owner(
            auth.worker_instance_id, gateway.owner_id, auth.epoch
        )
        queues = await gateway.bus.bind(
            auth.worker_instance_id, epoch=auth.epoch, owner_id=gateway.owner_id
        )
        assert queues is not None
        holder["task"] = asyncio.create_task(answer(queues, auth.epoch))

    async def answer(queues, epoch):
        while True:
            frame = await queues.wait_frame()
            name = frame["payload"]["name"]
            seen.append(name)
            data = frame["payload"].get("data") or {}
            if name == "switch_channel":
                await _complete(
                    gateway,
                    frame,
                    epoch,
                    {"reloaded": True, "gateway_url": GATEWAY, "mcp_url": MCP},
                )
                continue
            if name == "http" and data.get("path") == "/mcp":
                async with SessionLocal() as session:
                    await DataChannel(gateway).commit_upload(
                        session,
                        request_id=frame["request_id"],
                        worker_instance_id=frame["worker_instance_id"],
                        epoch=epoch,
                        upload_id="mcp",
                        http_status=200,
                        headers={},
                        content_type="application/json",
                        chunk=json.dumps({"nexus-library": {"status": "connected"}}),
                        complete=True,
                    )
                continue
            if name == "upload_file":
                async with SessionLocal() as session:
                    fetched = await DataChannel(gateway).fetch_request(
                        session,
                        request_id=frame["request_id"],
                        worker_instance_id=frame["worker_instance_id"],
                        epoch=epoch,
                    )
                result, _body = execute_file_command(
                    frame["payload"],
                    allowed_root=workspace,
                    request_id=frame["request_id"],
                    deadline=frame.get("expires_at") or 0,
                    content=fetched.content,
                )
                await _complete(gateway, frame, epoch, result)
                continue
            if name == "validate_path":
                result, _body = execute_file_command(
                    frame["payload"],
                    allowed_root=workspace,
                    request_id=frame["request_id"],
                    deadline=frame.get("expires_at") or 0,
                )
                await _complete(gateway, frame, epoch, result)

    events = []

    async def emit(event):
        events.append(event)

    await RemoteProvisioner(environment, operation, lease=lease).run(emit)
    assert events[-1]["success"] is True, events[-1]
    assert ssh_calls == ["ssh"]
    assert seen[:2] == ["switch_channel", "http"]
    texts = []
    for value in files.values():
        try:
            texts.append(value.decode())
        except UnicodeDecodeError:
            continue
    assert any(f"NEXUS_REMOTE_GATEWAY_URL={GATEWAY}" in text for text in texts)
    assert any(f"NEXUS_MCP_RESOURCE_URL={MCP}" in text for text in texts)
    assert any("NEXUS_WORKER_OPENCODE_URL=http://127.0.0.1:4096" in text for text in texts)
    assert any("serve --hostname 127.0.0.1 --port 4096" in text for text in texts)
    assert any(GATEWAY in text and "nexus-gateway" in text for text in texts)
    wheels = [path for path in files if path.endswith(".whl")]
    assert wheels == [f"/opt/nexus/workers/{environment_id}/nexus_worker_agent-1.4.5-py3-none-any.whl"]
    with zipfile.ZipFile(io.BytesIO(files[wheels[0]])) as archive:
        packed = set(archive.namelist())
    assert "app/worker_agent/__main__.py" in packed
    assert "app/worker_gateway/heartbeat.py" in packed
    assert "app/config/settings.py" not in packed
    assert "app/worker_gateway/service.py" not in packed
    install = next(command for command in commands if "pip install" in command)
    assert install.startswith(f"python3 -m venv /opt/nexus/workers/{environment_id}/venv && ")
    assert f"{agent_python(environment_id)} -m pip install" in install
    assert "--force-reinstall" in install
    assert wheels[0] in install
    worker_unit = f"nexus-worker-{environment_id}.service"
    opencode_unit = f"nexus-opencode-{environment_id}.service"
    unit = files[f"/etc/systemd/system/{worker_unit}"].decode()
    assert f"ExecStart={agent_python(environment_id)} -m app.worker_agent\n" in unit
    assert "/usr/bin/python3" not in unit
    skill_dir = f"/etc/nexus/opencode/{environment_id}"
    opencode = files[f"/etc/systemd/system/{opencode_unit}"].decode()
    worker_env = files[f"/etc/nexus/worker-{environment_id}.env"].decode()
    assert f"Environment=OPENCODE_CONFIG_DIR={skill_dir}\n" in opencode
    assert f"OPENCODE_CONFIG_DIR={skill_dir}\n" in worker_env
    restart = next(command for command in commands if "systemctl daemon-reload" in command)
    assert restart == (
        "systemctl daemon-reload && "
        f"systemctl enable {opencode_unit} {worker_unit} && "
        f"systemctl restart {opencode_unit} && "
        f"systemctl restart {worker_unit}"
    )
    assert commands.index(install) < commands.index(restart)
    async with SessionLocal() as db:
        stored = await db.get(OpenCodeExecutionEnvironment, environment_id)
        assert stored.worker_channel == "worker"
        assert stored.ssh_password == "test-ssh-secret"
        finished = await db.get(WorkerMaintenanceOperation, operation.id)
        assert finished.state == "SUCCEEDED"
        ready = SimpleNamespace(id=stored.id, mode=stored.mode)
    relative = await validate_path(ready, workspace, ".")
    assert relative == "."
    uploaded = await upload(ready, workspace, "note.txt", b"phase4")
    assert Path(uploaded).read_bytes() == b"phase4"
    assert ssh_calls == ["ssh"]
    holder["task"].cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder["task"]


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


def test_remote_port_default_is_14096_and_llm_wiki_tunnel_stays():
    root = repo_root()
    script = (root / "ops/deploy/install_nexus.sh").read_text(encoding="utf-8")
    assert "REMOTE_WORKER_OPENCODE_PORT:=14096" in script
    assert "4296" not in script
    assert "ssh -N -i" in script
    example = (root / "ops/env/.env.example").read_text(encoding="utf-8")
    assert "REMOTE_WORKER_OPENCODE_PORT=14096" in example
    assert "4296" not in example
    guide = (root / "ops/deploy/DEPLOYMENT_GUIDE.md").read_text(encoding="utf-8")
    employees = (root / "docs/deploy/REMOTE_EMPLOYEES.md").read_text(encoding="utf-8")
    assert "4296" not in guide
    assert "4296" not in employees
    platform = Path(__file__).resolve().parents[1]
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
