import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
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
from app.models import DigitalEmployee, OpenCodeExecutionEnvironment, RuntimeSession, Task
from app.runtime.lifecycle import RuntimeService, RuntimeStateError
from app.runtime.maintenance import submit_operation
from app.runtime.remote_provisioning import E2E_MODEL, RemoteProvisioner, managed_config


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "assistant_enabled", False)
    monkeypatch.setattr(settings, "mcp_transport", "streamable-http")
    monkeypatch.setattr(settings, "remote_gateway_local_url", "http://10.0.0.8:8110")
    monkeypatch.setattr(settings, "mcp_resource_server_url", "http://10.0.0.8:8201/mcp")
    monkeypatch.setattr(settings, "worker_agent_gateway_url", "ws://10.0.0.8:8100/api/worker-gateway/connect")


async def employee(**overrides):
    await init_database()
    employee_id = "deploy-" + uuid4().hex[:10]
    async with SessionLocal() as db:
        db.add(DigitalEmployee(id=employee_id, name=employee_id, role="build", current_task_count=0))
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
        return {"reloaded": True}

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
                f"/api/employees/{employee_id}/environment",
                {
                    "mode": "LINUX_REMOTE",
                    "name": "Linux",
                    "host": "other.invalid",
                    "username": "root",
                    "workspace_root": "/other",
                },
            ),
            ("delete", f"/api/employees/{employee_id}", None),
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
        "mcp": {"custom-mcp": {"enabled": False}},
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
        mcp_key="mcp-test",
        skills=[{"name": "review", "body": "# review\n"}],
        mcp_refs=[{"name": "worker-mcp", "url": "http://127.0.0.1:9/mcp"}],
    )
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(payload))
    repo = tmp_path / "repo" / ".opencode" / "skills" / "hidden"
    repo.mkdir(parents=True)
    (repo / "SKILL.md").write_text("untouched", encoding="utf-8")
    source = Path("app/runtime/remote_install.sh").read_text().split("<<'NODE'\n", 1)[1].split("\nNODE", 1)[0]
    result_file = tmp_path / "config-info.json"
    result = subprocess.run(
        [
            shutil.which("node"),
            "-",
            str(payload_file),
            "op123",
            sys.executable,
            str(Path("app/worker_agent/jsonc.py")),
            str(result_file),
        ],
        input=source,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(target.read_text())
    assert config["provider"]["custom"] == original["provider"]["custom"]
    assert config["provider"]["nexus-gateway"]["options"]["baseURL"] == "http://127.0.0.1:8110/v1"
    assert config["mcp"]["custom-mcp"] == original["mcp"]["custom-mcp"]
    assert config["mcp"]["worker-mcp"]["url"] == "http://127.0.0.1:9/mcp"
    assert config["permission"] == original["permission"]
    assert config["model"] == "nexus-gateway/test-model"
    assert config["mcp"]["nexus-library"]["headers"]["Authorization"] == "Bearer mcp-test"
    backup = json.loads((tmp_path / "opencode.jsonc.nexus-backup-op123").read_text().split("\n", 1)[1])
    assert backup == original
    assert target.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "skill" / "review" / "SKILL.md").read_text(encoding="utf-8") == "# review\n"
    assert (repo / "SKILL.md").read_text(encoding="utf-8") == "untouched"


def test_e2e_config_points_at_the_free_model_and_omits_the_gateway_key():
    payload = managed_config(
        config_path="/tmp/opencode.jsonc",
        model=E2E_MODEL,
        models=[],
        gateway_url="http://127.0.0.1:8110/v1",
        gateway_key="agw_do_not_read",
        mcp_url="http://127.0.0.1:8201/mcp",
        mcp_key="mcp-test",
        e2e=True,
    )
    encoded = json.dumps(payload)
    assert payload["model"] == E2E_MODEL
    assert payload["provider"] is None
    assert payload["e2e"] is True
    assert "agw_do_not_read" not in encoded


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


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js required for npm installation metadata")
@pytest.mark.parametrize("outcome", ["success", "install-failed", "wrong-version"])
def test_npm_install_migrates_manual_binary_and_preserves_it_on_failure(tmp_path, outcome):
    import os

    fixture_root = tmp_path / "employee home"
    stub_dir = tmp_path / "tools"
    stub_dir.mkdir()
    target = tmp_path / "opencode"
    target.write_text("#!/bin/sh\nprintf '1.18.35\\n'\n")
    target.chmod(0o755)
    calls = tmp_path / "npm-calls"
    npm = stub_dir / "npm"
    npm.write_text(
        """#!/bin/sh
set -eu
printf '%s\\n' "$*" >>"$npm_calls"
test "$install_outcome" != install-failed || exit 17
prefix="$fixture_root/npm"
mkdir -p "$prefix/bin" "$prefix/lib/node_modules/opencode-ai"
printf '{"version":"1.18.35"}' >"$prefix/lib/node_modules/opencode-ai/package.json"
actual=1.18.35
test "$install_outcome" != wrong-version || actual=1.2.27
printf '#!/bin/sh\\nprintf "%s\\\\n"\\n' "$actual" >"$prefix/bin/opencode"
chmod 755 "$prefix/bin/opencode"
"""
    )
    npm.chmod(0o755)
    script = Path("app/runtime/remote_install.sh").read_text()
    source = script.split(
        "# npm is installed under the Worker-specific prefix, never into a shared global path.\n",
        1,
    )[1].split("step 'OpenCode 安装' passed", 1)[0]
    source = (
        'fail() { printf "%s\\n" "$*" >&2; exit 1; }\n'
        "runtime_changed=false\n"
        'npm_prefix="$fixture_root/npm"\n'
        'npm_package="$npm_prefix/lib/node_modules/opencode-ai/package.json"\n'
        'npm_binary="$npm_prefix/bin/opencode"\n'
        'npm_cache="$fixture_root/npm-cache"\n'
        + source
    )
    env = dict(os.environ, PATH=str(stub_dir) + os.pathsep + os.environ["PATH"])
    env.update(
        fixture_root=str(fixture_root),
        npm_calls=str(calls),
        binary=str(target),
        opencode_version="1.18.35",
        npm_registry="https://registry.example.invalid",
        install_outcome=outcome,
    )
    result = subprocess.run(["bash", "-eu", "-c", source], env=env, capture_output=True, text=True)
    if outcome != "success":
        assert result.returncode != 0
        assert not target.is_symlink()
        assert "1.18.35" in target.read_text()
        return
    assert result.returncode == 0, result.stderr
    assert target.is_symlink()
    assert "opencode-ai@1.18.35" in calls.read_text()
    assert "--registry https://registry.example.invalid" in calls.read_text()
    assert "--include=optional" in calls.read_text()
    # A satisfied npm package is reused; a matching standalone binary alone was not.
    again = subprocess.run(["bash", "-eu", "-c", source], env=env, capture_output=True, text=True)
    assert again.returncode == 0, again.stderr
    assert len(calls.read_text().splitlines()) == 1


def _install_script(*extra, home: Path, payload: Path):
    return subprocess.run(
        [
            "bash",
            "app/runtime/remote_install.sh",
            "/srv/project",
            "/usr/local/bin/opencode",
            "22.23.3",
            "1.18.35",
            "https://registry.example.invalid",
            str(payload),
            "op123",
            *extra,
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home)},
        timeout=20,
    )


def test_install_script_rejects_python_below_3_11(tmp_path):
    payload = tmp_path / "payload.json"
    payload.write_text("{}")
    result = _install_script(str(tmp_path / "missing-python"), home=tmp_path, payload=payload)
    assert result.returncode != 0
    assert "需要 Python ≥3.11" in result.stderr
    assert "apt-get" not in result.stderr + result.stdout
    assert "nodejs.org" not in result.stderr + result.stdout


def test_install_script_installs_venv_package_for_the_selected_python(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    python = bindir / "python3.12"
    python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-\" ]; then exec /usr/bin/python3 \"$@\"; fi\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in\n"
        "    *ensurepip*) exit 1 ;;\n"
        "    *print*) printf '3.12\\n'; exit 0 ;;\n"
        "  esac\n"
        "done\n"
        "exit 0\n"
    )
    python.chmod(0o755)
    apt_log = tmp_path / "apt.log"
    apt = bindir / "apt-get"
    apt.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$APT_LOG\"\nexit 0\n")
    apt.chmod(0o755)
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps(
            {
                "action": "deploy",
                "environment_id": 1,
                "opencode_server_port": 4096,
                "config_path": str(tmp_path / "opencode.jsonc"),
            }
        )
    )
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "APT_LOG": str(apt_log),
    }
    result = subprocess.run(
        [
            "bash",
            "app/runtime/remote_install.sh",
            "/srv/project",
            "/usr/local/bin/opencode",
            "22.23.3",
            "1.18.35",
            "https://registry.example.invalid",
            str(payload),
            "op123",
            str(python),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    assert result.returncode != 0
    assert "需要安装 python3.12-venv" in result.stderr
    assert "nodejs.org" not in result.stderr + result.stdout
    if os.geteuid() == 0:
        assert "python3.12-venv" in apt_log.read_text()
    else:
        assert not apt_log.exists()


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
    calls = []

    class SSH:
        async def write_file(self, path, data, mode=0o644):
            calls.append(path)

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
        await worker._install_units(emit)
    assert not any(item == "enroll" for item in calls)
    assert not any(isinstance(item, str) and item.endswith(".env") for item in calls)
    pip = next(item for item in calls if isinstance(item, tuple) and "pip install" in item[1])
    assert pip[0] == 300
    assert pip[1].startswith("/usr/bin/python3.12 -m venv /opt/nexus/workers/7/venv && ")
    assert "-i https://pypi.example.invalid/simple " in pip[1]


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

    await worker._install_units(emit)
    pip_at = next(i for i, item in enumerate(calls) if isinstance(item, tuple) and "pip install" in item[1])
    enroll_at = calls.index("enroll")
    token_at = next(i for i, item in enumerate(calls) if isinstance(item, str) and item.endswith(".env"))
    assert pip_at < enroll_at < token_at
    assert calls[pip_at][0] == 300
