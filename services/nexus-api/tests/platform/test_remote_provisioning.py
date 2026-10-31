import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config.settings import get_settings
from app.database import SessionLocal, init_database
from app.gateway.models import GatewayKey
from app.gateway.security import gateway_key_digest
from app.main import app
from app.models import AgentWorker, OpenCodeExecutionEnvironment, RuntimeSession, Task
from app.runtime.lifecycle import RuntimeService, RuntimeStateError
from app.runtime.maintenance import submit_operation
from app.runtime.remote_provisioning import RemoteProvisioner, managed_config


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mcp_transport", "streamable-http")
    monkeypatch.setattr(settings, "remote_gateway_local_url", "http://10.0.0.8:8110")
    monkeypatch.setattr(settings, "mcp_resource_server_url", "http://10.0.0.8:8201/mcp")
    monkeypatch.setattr(settings, "worker_agent_gateway_url", "ws://10.0.0.8:8100/api/worker-gateway/connect")


async def employee(**overrides):
    await init_database()
    employee_id = "deploy-" + uuid4().hex[:10]
    async with SessionLocal() as db:
        db.add(AgentWorker(id=employee_id, name=employee_id, role="build", current_task_count=0))
        await db.flush()
        values = dict(
            employee_id=employee_id,
            mode="LINUX_REMOTE",
            name="Linux",
            host="test.invalid",
            port=22,
            username="root",
            ssh_password="test-ssh-secret",
            workspace_root="/srv/project",
            opencode_path="/usr/local/bin/opencode",
            status="UNKNOWN",
            status_detail={},
        )
        values.update(overrides)
        db.add(OpenCodeExecutionEnvironment(**values))
        await db.commit()
    return employee_id


async def reserve(employee_id):
    async with SessionLocal() as db:
        environment = await db.scalar(
            select(OpenCodeExecutionEnvironment).where(
                OpenCodeExecutionEnvironment.employee_id == employee_id
            )
        )
        operation = await submit_operation(db, environment.id, "deploy")
        return environment, operation


@pytest.mark.asyncio
async def test_restart_finish_keeps_the_capability_record():
    record = {
        "desired_revision": "rev",
        "applied_revision": "rev",
        "runtime_status": "applied",
        "error": "",
    }
    employee_id = await employee(status_detail={"capability": record})
    env, op = await reserve(employee_id)
    assert env.status_detail["capability"] == record
    applied = await RemoteProvisioner(env, op)._finish(True)
    assert applied
    async with SessionLocal() as db:
        current = await db.get(OpenCodeExecutionEnvironment, env.id)
        assert current.status_detail["capability"] == record


@pytest.mark.asyncio
async def test_channel_switch_keeps_capability(monkeypatch):
    from app.runtime.worker_channel import mark_worker_channel

    record = {
        "desired_revision": "rev",
        "applied_revision": "rev",
        "runtime_status": "applied",
        "error": "",
    }
    employee_id = await employee(status_detail={"capability": record})

    async def environment_id():
        async with SessionLocal() as db:
            environment = await db.scalar(
                select(OpenCodeExecutionEnvironment).where(
                    OpenCodeExecutionEnvironment.employee_id == employee_id
                )
            )
            return environment.id

    target = await environment_id()

    async def switch():
        return {"reloaded": True, "verified": True}

    monkeypatch.setattr("app.runtime.worker_channel._mismatch", lambda _result: None)
    async with SessionLocal() as db:
        stored = await mark_worker_channel(db, target, switch=switch)
        assert stored.worker_channel == "worker"
        assert stored.status_detail["capability"] == record

    employee_id = await employee(status_detail={"capability": record})
    target = await environment_id()

    async def fail():
        raise RuntimeError("switch failed")

    with pytest.raises(HTTPException):
        async with SessionLocal() as db:
            await mark_worker_channel(db, target, switch=fail)
    async with SessionLocal() as db:
        current = await db.scalar(
            select(OpenCodeExecutionEnvironment).where(
                OpenCodeExecutionEnvironment.employee_id == employee_id
            )
        )
        assert current.worker_channel == "ssh"
        assert current.status_detail["capability"] == record


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [{"mode": "LOCAL"}, {"ssh_password": None}])
async def test_reservation_rejects_ineligible_environment(overrides):
    employee_id = await employee(**overrides)
    with pytest.raises(HTTPException) as error:
        await reserve(employee_id)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_reservation_checks_runtime_facts_even_when_employee_count_is_zero():
    employee_id = await employee()
    async with SessionLocal() as db:
        task = Task(
            id="task-" + uuid4().hex,
            title="guard",
            task_type="ISSUE",
            category_id="testing-platform",
            employee_id=employee_id,
            status="SUCCEEDED",
        )
        db.add(task)
        await db.flush()
        db.add(
            RuntimeSession(
                id="ses_" + uuid4().hex,
                task_id=task.id,
                employee_id=employee_id,
                status="UNKNOWN",
                workspace="/srv/project",
                environment={},
            )
        )
        await db.commit()
    with pytest.raises(HTTPException) as error:
        await reserve(employee_id)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_reservation_is_exclusive_and_blocks_manual_start():
    employee_id = await employee()
    env, op = await reserve(employee_id)
    with pytest.raises(HTTPException):
        await reserve(employee_id)
    async with SessionLocal() as db:
        task = Task(
            id="task-" + uuid4().hex,
            title="start guard",
            task_type="ISSUE",
            category_id="testing-platform",
            employee_id=employee_id,
            status="PENDING",
        )
        db.add(task)
        await db.commit()
        with pytest.raises(RuntimeStateError, match="维护"):
            await RuntimeService(db).start(task.id)


@pytest.mark.asyncio
async def test_operation_blocks_configuration_delete_and_test_api():
    employee_id = await employee()
    env, op = await reserve(employee_id)
    # ASGITransport does not run the lifespan executor; the queued operation stays
    # the active maintenance lock for these guard assertions.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for method, url, body in [
            (
                "put",
                f"/api/agent-workers/{employee_id}/environment",
                {
                    "mode": "LINUX_REMOTE",
                    "name": "Linux",
                    "host": "other.invalid",
                    "username": "root",
                    "workspace_root": "/other",
                },
            ),
            ("delete", f"/api/agent-workers/{employee_id}", None),
        ]:
            response = await client.request(method, url, json=body)
            assert response.status_code == 409, response.text
    from app.runtime.maintenance import cancel_operation

    async with SessionLocal() as db:
        await cancel_operation(db, op.id)


@pytest.mark.asyncio
async def test_redeploy_reuses_existing_employee_key():
    employee_id = await employee()
    env, op = await reserve(employee_id)
    raw = "agw_existing-test-key"
    async with SessionLocal() as db:
        db.add(
            GatewayKey(
                id="key-" + uuid4().hex,
                name="employee",
                owner_id=employee_id,
                key_prefix=raw[:12],
                key_digest=gateway_key_digest(raw),
                enabled=True,
                allowed_models=["test-model"],
            )
        )
        await db.commit()

    class SSH:
        async def read_file(self, _path):
            return json.dumps({"provider": {"nexus-gateway": {"options": {"apiKey": raw}}}}).encode()

    worker = RemoteProvisioner(env, op)
    worker.ssh = SSH()
    assert await worker._gateway_key("/config/opencode.json", ["test-model"]) == raw
    assert worker.new_key_id is None
    assert raw not in worker.redact("request " + raw + " test-ssh-secret Bearer mcp-secret")
    assert "test-ssh-secret" not in worker.redact("test-ssh-secret")


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js required for real config merge")
def test_real_config_merge_writes_jsonc_and_keeps_user_settings(tmp_path):
    target = tmp_path / "opencode.jsonc"
    original = {
        "provider": {"custom": {"options": {"apiKey": "private"}}},
        "mcp": {
            "custom-mcp": {"enabled": False},
            "nexus-library": {
                "type": "remote",
                "url": "http://old/mcp",
                "headers": {"Authorization": "Bearer old-mcp-key"},
            },
        },
        "permission": {"bash": "ask"},
    }
    target.write_text("// keep this\n" + json.dumps(original))
    payload = managed_config(
        config_path=str(target),
        model="test-model",
        models=["test-model"],
        gateway_url="http://127.0.0.1:8110/v1",
        gateway_key="agw_test",
        mcp_url="http://127.0.0.1:8201/mcp",
        skills=[{"name": "review", "body": "# review\n"}],
        mcp_refs=[{"name": "worker-mcp", "url": "http://127.0.0.1:9/mcp"}],
    )
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(payload))
    repo = tmp_path / "repo" / ".opencode" / "skills" / "hidden"
    repo.mkdir(parents=True)
    (repo / "SKILL.md").write_text("untouched", encoding="utf-8")
    import asyncio

    from app.runtime.remote_workspace import configure_runtime
    from tests.platform.deployment_target import LocalTarget

    worker = LocalTarget(tmp_path)
    worker.operation_id = "op123"
    asyncio.run(configure_runtime(worker, payload))
    config = json.loads(target.read_text())
    assert config["provider"]["custom"] == original["provider"]["custom"]
    assert config["provider"]["nexus-gateway"]["options"]["baseURL"] == "http://127.0.0.1:8110/v1"
    assert config["mcp"]["custom-mcp"] == original["mcp"]["custom-mcp"]
    assert config["mcp"]["worker-mcp"]["url"] == "http://127.0.0.1:9/mcp"
    assert config["permission"] == original["permission"]
    assert config["model"] == "nexus-gateway/test-model"
    assert "headers" not in config["mcp"]["nexus-library"]
    backup = json.loads((tmp_path / "opencode.jsonc.nexus-backup-op123").read_text().split("\n", 1)[1])
    assert backup == original
    assert target.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "skill" / "review" / "SKILL.md").read_text(encoding="utf-8") == "# review\n"
    assert (repo / "SKILL.md").read_text(encoding="utf-8") == "untouched"


def test_special_model_names_still_use_gateway():
    payload = managed_config(
        config_path="/tmp/opencode.jsonc",
        model="opencode/deepseek-v4.1-flash-free",
        models=[],
        gateway_url="http://127.0.0.1:8110/v1",
        gateway_key="agw_do_not_read",
        mcp_url="http://127.0.0.1:8201/mcp",
    )
    assert payload["model"] == "nexus-gateway/deepseek-v4.1-flash-free"
    assert payload["provider"]["options"]["apiKey"] == "agw_do_not_read"


@pytest.mark.asyncio
async def test_native_busy_refuses_maintenance_before_install():
    env, op = await reserve(await employee())
    worker = RemoteProvisioner(env, op)

    async def run_command(command, timeout=30):
        assert "session/status?directory=%2Fsrv%2Fproject" in command
        return 0, '{"ses_busy": {"type": "busy"}}', ""

    worker.ssh = type("SSH", (), {"run_command": staticmethod(run_command)})()
    with pytest.raises(RuntimeError, match="正在执行"):
        await worker._native_idle(strict=True)


def test_deleted_tunnel_helpers_have_no_callers():
    from pathlib import Path

    root = Path("app")
    banned = (
        "ensure_remote_mcp_tunnel",
        "ensure_remote_gateway_tunnel",
        "close_remote_mcp_tunnels",
        "open_reverse_tunnel",
        "SSHReverseTunnel",
        "SSHTunnel",
    )
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in banned:
            assert name not in text, f"{path} still mentions {name}"


def _agent_install_worker(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "remote_python", "/usr/bin/python3.12")
    monkeypatch.setattr(settings, "remote_pip_index_url", "https://pypi.example.invalid/simple")
    environment = SimpleNamespace(
        id=7,
        opencode_server_port=4096,
        workspace_root="/srv/project",
        ssh_password="secret",
        model_name="",
        opencode_path="/usr/local/bin/opencode",
    )
    worker = RemoteProvisioner(environment, {"id": "op", "action": "deploy"})
    worker.home = "/root"
    worker.config_path = "/etc/nexus/opencode/7/opencode.jsonc"
    worker._manage_process = AsyncMock()
    worker._native_idle = AsyncMock(return_value=True)
    worker._wait_opencode_ready = AsyncMock()
    calls = []

    class SSH:
        def __init__(self):
            self.files = {}

        async def write_file(self, path, data, mode=0o644):
            calls.append(path)
            self.files[path] = data

        async def read_file(self, path):
            return self.files.get(path)

        async def run_command(self, command):
            return 0, "", ""

    worker.ssh = SSH()

    async def command(self, text, timeout=30):
        calls.append((timeout, text))
        return ""

    monkeypatch.setattr(RemoteProvisioner, "_command", command)
    return worker, calls


@pytest.mark.asyncio
async def test_failed_venv_does_not_rotate_the_worker_token(monkeypatch):
    worker, calls = _agent_install_worker(monkeypatch)

    class Gateway:
        async def enroll(self, session, environment_id):
            raise AssertionError("venv 失败不应轮换 token")

    monkeypatch.setattr("app.worker_gateway.service.get_gateway", lambda: Gateway())

    async def command(self, text, timeout=30):
        calls.append((timeout, text))
        if "pip install" in text:
            raise RuntimeError("pip failed")
        return ""

    monkeypatch.setattr(RemoteProvisioner, "_command", command)

    async def emit(_event):
        return None

    with pytest.raises(RuntimeError, match="pip failed"):
        await worker._install_worker(emit)
    assert not any(item == "enroll" for item in calls)
    assert not any(isinstance(item, str) and item.endswith(".env") for item in calls)
    pip = next(item for item in calls if isinstance(item, tuple) and "pip install" in item[1])
    assert pip[0] == 300
    assert pip[1].startswith("/usr/bin/python3.12 -m venv /opt/nexus/workers/7/venv && ")
    assert "--index-url https://pypi.example.invalid/simple " in pip[1]


@pytest.mark.asyncio
async def test_enroll_happens_after_the_venv_and_writes_the_token(monkeypatch):
    worker, calls = _agent_install_worker(monkeypatch)

    class Gateway:
        async def enroll(self, session, environment_id):
            calls.append("enroll")
            return SimpleNamespace(token="wtk_secret", worker_instance_id="wrk_test")

    monkeypatch.setattr("app.worker_gateway.service.get_gateway", lambda: Gateway())

    async def emit(_event):
        return None

    await worker._install_worker(emit)
    pip_at = next(i for i, item in enumerate(calls) if isinstance(item, tuple) and "pip install" in item[1])
    enroll_at = calls.index("enroll")
    token_at = next(
        i for i, item in enumerate(calls) if isinstance(item, str) and ".env.tmp-" in item
    )
    assert pip_at < enroll_at < token_at
    assert calls[pip_at][0] == 300


class _AtomicLocalSSH:
    """A local filesystem double that executes the same path-only commands."""

    def __init__(self, *, partial_at=None, corrupt=False, fail_move=False, read_error=False):
        self.partial_at = partial_at
        self.corrupt = corrupt
        self.fail_move = fail_move
        self.read_error = read_error
        self.writes: list[str] = []

    async def read_file(self, path):
        if self.read_error:
            raise OSError("permission denied while reading")
        try:
            return Path(path).read_bytes()
        except FileNotFoundError:
            return None

    async def write_file(self, path, data, mode=0o644):
        self.writes.append(path)
        if self.partial_at is not None:
            Path(path).write_bytes(data[: self.partial_at])
            raise RuntimeError("sftp interrupted")
        if self.corrupt:
            data = data[:-1]
        Path(path).write_bytes(data)
        os.chmod(path, mode)

    async def run_command(self, command):
        if self.fail_move and command.startswith("mv -f "):
            return 1, "", "rename failed"
        result = subprocess.run(
            ["bash", "-c", command], capture_output=True, text=True, timeout=20
        )
        return result.returncode, result.stdout, result.stderr


def _atomic_worker(secret: str = "ssh-canary-secret"):
    environment = SimpleNamespace(
        id=901,
        ssh_password=secret,
        model_name=None,
        opencode_path="/usr/local/bin/opencode",
    )
    return RemoteProvisioner(environment, {"id": "op-atomic", "action": "deploy"})


@pytest.mark.asyncio
async def test_atomic_write_skips_identical_content_but_enforces_mode(tmp_path):
    target = tmp_path / "worker 1.env"
    target.write_bytes(b"NEXUS_WORKER_TOKEN=canary\n")
    target.chmod(0o644)
    before = target.stat().st_mtime_ns
    ssh = _AtomicLocalSSH()
    worker = _atomic_worker()
    worker.ssh = ssh

    changed = await worker._write_atomic_if_changed(
        str(target), target.read_bytes(), 0o600, backup=True
    )

    assert changed is False
    assert ssh.writes == []
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.stat().st_mtime_ns == before
    assert not (tmp_path / "worker 1.env.nexus-backup").exists()


@pytest.mark.asyncio
async def test_atomic_write_replaces_and_keeps_old_backup(tmp_path):
    target = tmp_path / "worker 1.env"
    target.write_bytes(b"old-canary\n")
    ssh = _AtomicLocalSSH()
    worker = _atomic_worker()
    worker.ssh = ssh

    changed = await worker._write_atomic_if_changed(
        str(target), b"new-value\n", 0o600, backup=True
    )

    assert changed is True
    assert target.read_bytes() == b"new-value\n"
    assert target.stat().st_mode & 0o777 == 0o600
    backup = tmp_path / "worker 1.env.nexus-backup"
    assert backup.read_bytes() == b"old-canary\n"
    assert backup.stat().st_mode & 0o777 == 0o600
    assert len(ssh.writes) == 1
    assert Path(ssh.writes[0]).parent == tmp_path
    assert not list(tmp_path.glob("*.tmp-*"))


@pytest.mark.asyncio
async def test_atomic_write_stops_when_existing_read_fails(tmp_path):
    target = tmp_path / "worker 1.env"
    target.write_bytes(b"old-canary\n")
    target.chmod(0o600)
    ssh = _AtomicLocalSSH(read_error=True)
    worker = _atomic_worker()
    worker.ssh = ssh

    with pytest.raises(OSError, match="permission denied"):
        await worker._write_atomic_if_changed(
            str(target), b"new-secret-value\n", 0o600, backup=True
        )

    assert target.read_bytes() == b"old-canary\n"
    assert ssh.writes == []
    assert not (tmp_path / "worker 1.env.nexus-backup").exists()
    assert not list(tmp_path.glob("*.tmp-*"))


@pytest.mark.asyncio
async def test_atomic_write_interruption_keeps_old_target_and_backup(tmp_path):
    target = tmp_path / "worker 1.env"
    target.write_bytes(b"old-canary\n")
    ssh = _AtomicLocalSSH(partial_at=3)
    worker = _atomic_worker()
    worker.ssh = ssh

    with pytest.raises(RuntimeError) as error:
        await worker._write_atomic_if_changed(
            str(target), b"new-secret-value\n", 0o600, backup=True
        )

    assert target.read_bytes() == b"old-canary\n"
    assert (tmp_path / "worker 1.env.nexus-backup").read_bytes() == b"old-canary\n"
    assert not list(tmp_path.glob("*.tmp-*"))
    assert "new-secret-value" not in str(error.value)
    assert "ssh-canary-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_atomic_write_verification_failure_keeps_old_target(tmp_path):
    target = tmp_path / "worker 1.env"
    target.write_bytes(b"old-canary\n")
    ssh = _AtomicLocalSSH(corrupt=True)
    worker = _atomic_worker()
    worker.ssh = ssh

    with pytest.raises(RuntimeError, match="完整性校验失败") as error:
        await worker._write_atomic_if_changed(
            str(target), b"new-secret-value\n", 0o600, backup=True
        )

    assert target.read_bytes() == b"old-canary\n"
    assert (tmp_path / "worker 1.env.nexus-backup").read_bytes() == b"old-canary\n"
    assert not list(tmp_path.glob("*.tmp-*"))
    assert "new-secret-value" not in str(error.value)


@pytest.mark.asyncio
async def test_atomic_write_rename_failure_keeps_old_target_and_reports(tmp_path):
    target = tmp_path / "worker 1.env"
    target.write_bytes(b"old-canary\n")
    ssh = _AtomicLocalSSH(fail_move=True)
    worker = _atomic_worker()
    worker.ssh = ssh

    with pytest.raises(RuntimeError, match="rename failed"):
        await worker._write_atomic_if_changed(
            str(target), b"new-secret-value\n", 0o600, backup=True
        )

    assert target.read_bytes() == b"old-canary\n"
    assert (tmp_path / "worker 1.env.nexus-backup").read_bytes() == b"old-canary\n"
    assert not list(tmp_path.glob("*.tmp-*"))


@pytest.mark.asyncio
async def test_atomic_write_creates_new_file_with_requested_mode(tmp_path):
    target = tmp_path / "worker 2.env"
    ssh = _AtomicLocalSSH()
    worker = _atomic_worker()
    worker.ssh = ssh

    changed = await worker._write_atomic_if_changed(
        str(target), b"value\n", 0o600, backup=True
    )

    assert changed is True
    assert target.read_bytes() == b"value\n"
    assert target.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "worker 2.env.nexus-backup").exists()


class _ProbeLocalSSH:
    def __init__(self, env):
        self.env = env

    async def run_command(self, command):
        result = subprocess.run(
            command, shell=True, env=self.env, capture_output=True, text=True
        )
        return result.returncode, result.stdout, result.stderr


def _write_probe_node(path, log, output):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$0\" >> {shlex.quote(str(log))}\n"
        f"printf '%s\\n' {shlex.quote(output)}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


async def _probe_worker(tmp_path):
    env, op = await reserve(await employee())
    worker = RemoteProvisioner(env, op)
    worker.home = str(tmp_path)
    worker.python_interpreter = str(tmp_path / "python")
    worker.target_model = "nexus-gateway/probe-model"
    worker.config_path = str(tmp_path / "opencode.jsonc")
    Path(worker.config_path).write_text("{}", encoding="utf-8")
    return worker


def _probe_node_path(tmp_path, worker):
    return (
        tmp_path
        / ".local/share/nexus/workers"
        / str(worker.environment.id)
        / f"node-v{worker.settings.remote_node_version}"
        / "bin"
        / "node"
    )


@pytest.mark.asyncio
async def test_probe_gateway_uses_environment_node_without_global_node(tmp_path):
    worker = await _probe_worker(tmp_path)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    worker.ssh = _ProbeLocalSSH({"PATH": str(empty_bin), "HOME": str(tmp_path)})
    log = tmp_path / "invoked.log"
    node = Path(worker.python_interpreter)
    _write_probe_node(node, log, json.dumps({"status":200,"body":{"data":[{"id":"probe-model"}]}}))
    steps = []

    async def emit(step):
        steps.append(step)

    await worker._probe_gateway(emit)

    assert log.read_text(encoding="utf-8").splitlines() == [str(node)]
    assert steps[-1]["detail"] == "模型中转认证与模型路由正常"


@pytest.mark.asyncio
async def test_probe_gateway_fails_without_environment_node_and_ignores_global(
    tmp_path,
):
    worker = await _probe_worker(tmp_path)
    global_bin = tmp_path / "global-bin"
    global_log = tmp_path / "global.log"
    _write_probe_node(global_bin / "node", global_log, "global-node")
    worker.ssh = _ProbeLocalSSH({"PATH": str(global_bin), "HOME": str(tmp_path)})
    steps = []

    async def emit(step):
        steps.append(step)

    with pytest.raises(RuntimeError):
        await worker._probe_gateway(emit)

    assert not global_log.exists()


@pytest.mark.asyncio
async def test_probe_gateway_fails_when_home_is_unknown(tmp_path):
    worker = await _probe_worker(tmp_path)
    worker.config_path = None
    global_bin = tmp_path / "global-bin"
    global_log = tmp_path / "global.log"
    _write_probe_node(global_bin / "node", global_log, "global-node")
    worker.ssh = _ProbeLocalSSH({"PATH": str(global_bin), "HOME": str(tmp_path)})
    steps = []

    async def emit(step):
        steps.append(step)

    with pytest.raises(RuntimeError, match="尚未准备 OpenCode 配置"):
        await worker._probe_gateway(emit)

    assert not global_log.exists()
