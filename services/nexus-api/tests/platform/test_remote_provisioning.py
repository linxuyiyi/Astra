import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config.settings import get_settings
from app.runtime.remote_provisioning import (
    START_ALL_PATH,
    RemoteProvisioner,
    managed_config,
    render_start_all_script,
    render_worker_ctl_script,
    worker_ctl_path,
)
from app.runtime.worker_readiness import compute_target_digest


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mcp_transport", "streamable-http")
    monkeypatch.setattr(settings, "gateway_openai_base", "http://10.0.0.8:8110")
    monkeypatch.setattr(settings, "mcp_resource_server_url", "http://10.0.0.8:8201/mcp")
    monkeypatch.setattr(settings, "worker_agent_gateway_url", "ws://10.0.0.8:8100/api/worker-gateway/connect")


@pytest.mark.asyncio
async def test_ctl_and_start_all_scripts_are_non_systemd_and_target_digest_tracks_identity():
    ctl = render_worker_ctl_script("7", port=4096)
    start_all = render_start_all_script()
    assert "systemctl" not in ctl and "systemd" not in ctl
    assert "systemctl" not in start_all and "systemd" not in start_all
    assert "app.worker_agent" in ctl
    assert all(branch in ctl for branch in ("start)", "stop)", "restart)", "status)"))
    assert "/opt/nexus/workers" in start_all

    base = SimpleNamespace(
        mode="LINUX_REMOTE",
        host="10.0.0.9",
        port=22,
        username="root",
        workspace_root="/srv/project",
    )
    same = SimpleNamespace(
        mode="LINUX_REMOTE",
        host="10.0.0.9",
        port=22,
        username="root",
        workspace_root="/srv/project/",
    )
    moved = SimpleNamespace(
        mode="LINUX_REMOTE",
        host="10.0.0.10",
        port=22,
        username="root",
        workspace_root="/srv/project",
    )
    assert compute_target_digest(base) == compute_target_digest(same)
    assert compute_target_digest(base) != compute_target_digest(moved)


@pytest.mark.parametrize("matching_process", [True, False])
async def test_owned_listener_script_checks_each_process_before_foreign_tail(
    tmp_path, monkeypatch, matching_process
):
    worker = RemoteProvisioner(SimpleNamespace(id="7", model_name=""), {"id": "probe"})
    proc = tmp_path / "proc"
    for pid, mark in [("11", "7" if matching_process else "8"), ("22", "unrelated")]:
        entry = proc / pid
        entry.mkdir(parents=True)
        (entry / "environ").write_bytes(f"NEXUS_AGENT_WORKER_ID={mark}\0".encode())

    async def execute(command, timeout=30):
        args = shlex.split(command)
        script = args[3].replace("pathlib.Path('/proc')", f"pathlib.Path({str(proc)!r})")
        result = subprocess.run(
            [sys.executable, "-I", "-c", script, str(tmp_path / "worker.lock"), "7"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    monkeypatch.setattr(worker, "_command", execute)
    if matching_process:
        await worker._require_owned_listener(interpreter=sys.executable)
    else:
        with pytest.raises(RuntimeError, match="未知进程"):
            await worker._require_owned_listener(interpreter=sys.executable)


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
        id="7",
        mode="LINUX_REMOTE",
        host="10.0.0.9",
        port=22,
        username="root",
        workspace_root="/srv/project",
        ssh_password="secret",
        model_name="",
        repository_url=None,
        skill_refs=[],
        mcp_refs=[],
    )
    worker = RemoteProvisioner(environment, {"id": "op", "action": "deploy"})
    worker.opencode_port = 4096
    worker.deployed_spec_revision = "test-revision"
    worker.home = "/root"
    worker.config_path = "/etc/nexus/opencode/7/opencode.jsonc"
    worker._run_ctl = AsyncMock()
    worker._native_idle = AsyncMock(return_value=True)
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
        async def enroll(self, session, agent_worker_id, *, target_digest=None):
            raise AssertionError("venv 失败不应轮换 token")

    monkeypatch.setattr("app.worker_gateway.service.get_gateway", lambda *_a, **_kw: Gateway())

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
        async def enroll(self, session, agent_worker_id, *, target_digest=None):
            calls.append(("enroll", target_digest))
            return SimpleNamespace(token="wtk_secret", agent_worker_id="7", epoch=3)

    monkeypatch.setattr("app.worker_gateway.service.get_gateway", lambda *_a, **_kw: Gateway())

    async def emit(_event):
        return None

    await worker._install_worker(emit)
    pip_at = next(
        i for i, item in enumerate(calls) if isinstance(item, tuple) and "pip install" in str(item[1])
    )
    enroll_at = next(i for i, item in enumerate(calls) if isinstance(item, tuple) and item[0] == "enroll")
    token_at = next(i for i, item in enumerate(calls) if isinstance(item, str) and ".env.tmp-" in item)
    ctl_at = next(
        i for i, item in enumerate(calls) if isinstance(item, str) and f"{worker_ctl_path(7)}.tmp-" in item
    )
    start_all_at = next(
        i for i, item in enumerate(calls) if isinstance(item, str) and f"{START_ALL_PATH}.tmp-" in item
    )
    assert pip_at < enroll_at < token_at < ctl_at < start_all_at
    assert calls[pip_at][0] == 300
    worker._run_ctl.assert_awaited_once_with("restart")


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
        result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=20)
        return result.returncode, result.stdout, result.stderr


def _atomic_worker(secret: str = "ssh-canary-secret"):
    environment = SimpleNamespace(
        id="901",
        ssh_password=secret,
        model_name=None,
    )
    return RemoteProvisioner(environment, {"id": "op-atomic", "action": "deploy"})


async def test_atomic_write_creates_missing_config_parent_without_chmod_existing_parent(tmp_path):
    target = tmp_path / "config" / "skill" / "proof" / "SKILL.md"
    tmp_path.chmod(0o755)
    worker = _atomic_worker()
    worker.ssh = _AtomicLocalSSH()

    assert await worker._write_atomic_if_changed(str(target), b"# Proof\n", 0o600)
    assert target.read_bytes() == b"# Proof\n"
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    assert tmp_path.stat().st_mode & 0o777 == 0o755


@pytest.mark.asyncio
async def test_atomic_write_skips_identical_content_but_enforces_mode(tmp_path):
    target = tmp_path / "worker 1.env"
    target.write_bytes(b"NEXUS_WORKER_TOKEN=canary\n")
    target.chmod(0o644)
    before = target.stat().st_mtime_ns
    ssh = _AtomicLocalSSH()
    worker = _atomic_worker()
    worker.ssh = ssh

    changed = await worker._write_atomic_if_changed(str(target), target.read_bytes(), 0o600, backup=True)

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

    changed = await worker._write_atomic_if_changed(str(target), b"new-value\n", 0o600, backup=True)

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
        await worker._write_atomic_if_changed(str(target), b"new-secret-value\n", 0o600, backup=True)

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
        await worker._write_atomic_if_changed(str(target), b"new-secret-value\n", 0o600, backup=True)

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
        await worker._write_atomic_if_changed(str(target), b"new-secret-value\n", 0o600, backup=True)

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
        await worker._write_atomic_if_changed(str(target), b"new-secret-value\n", 0o600, backup=True)

    assert target.read_bytes() == b"old-canary\n"
    assert (tmp_path / "worker 1.env.nexus-backup").read_bytes() == b"old-canary\n"
    assert not list(tmp_path.glob("*.tmp-*"))


@pytest.mark.asyncio
async def test_atomic_write_creates_new_file_with_requested_mode(tmp_path):
    target = tmp_path / "worker 2.env"
    ssh = _AtomicLocalSSH()
    worker = _atomic_worker()
    worker.ssh = ssh

    changed = await worker._write_atomic_if_changed(str(target), b"value\n", 0o600, backup=True)

    assert changed is True
    assert target.read_bytes() == b"value\n"
    assert target.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "worker 2.env.nexus-backup").exists()


class _ProbeLocalSSH:
    def __init__(self, env):
        self.env = env

    async def run_command(self, command):
        result = subprocess.run(command, shell=True, env=self.env, capture_output=True, text=True)
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
@pytest.mark.parametrize("state", ["CLEAR", "STOPPED"])
async def test_bootstrap_without_interpreter_accepts_clear_or_stopped_listener(state):
    worker = RemoteProvisioner(
        SimpleNamespace(id="7", model_name=""),
        {"id": "bootstrap"},
    )
    worker.opencode_port = 4096
    worker._ensure_safe = AsyncMock()
    worker._command = AsyncMock(side_effect=["", state])

    await worker._bootstrap_idle(interpreter_ready=False)

    worker._ensure_safe.assert_awaited_once()
    command = worker._command.call_args.args[0]
    assert command.startswith("bash -c ")
    assert command.endswith(" -- 1000")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["FOREIGN", "UNKNOWN", "STILL_LISTENING"])
async def test_bootstrap_without_interpreter_rejects_unreleased_listener(state):
    worker = RemoteProvisioner(
        SimpleNamespace(id="7", model_name=""),
        {"id": "bootstrap"},
    )
    worker.opencode_port = 4096
    worker._ensure_safe = AsyncMock()
    worker._command = AsyncMock(side_effect=["", state])

    with pytest.raises(RuntimeError, match="受管 OpenCode"):
        await worker._bootstrap_idle(interpreter_ready=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["HTTP 503 Service Unavailable", "HTTP 401 Unauthorized"])
async def test_readiness_retries_transient_mcp_on_current_epoch_only(monkeypatch, failure):
    from app.runtime import remote_provisioning

    row = SimpleNamespace(id="7", epoch=5, status="PAUSED", enabled=False, model_name=None)
    worker = RemoteProvisioner(row, {"id": "deploy"})
    worker.enrollment_epoch = 4
    worker._command = AsyncMock(return_value="running\nconnecting")
    worker._reconnect_mcp = AsyncMock()
    db = AsyncMock()
    db.get.return_value = row
    session = AsyncMock()
    session.__aenter__.return_value = db
    monkeypatch.setattr(remote_provisioning, "SessionLocal", lambda: session)
    # Jump past the former 30-second check window without delaying the test.
    ticks = iter((0.0, 40.0, 50.0))
    clock = SimpleNamespace(time=lambda: next(ticks))
    monkeypatch.setattr(
        remote_provisioning, "asyncio", SimpleNamespace(get_running_loop=lambda: clock, sleep=AsyncMock()),
    )
    desired = {"mcp": {"required": ["vision"]}}
    monkeypatch.setattr(remote_provisioning, "build_desired_spec", AsyncMock(return_value=desired))

    failed = {"mcp": {"servers": {"vision": {"status": "failed", "error": failure}}}}
    ready = {"mcp": {"servers": {"vision": {"status": "connected"}}}}
    owner = AsyncMock(side_effect=[("gateway", 4), ("gateway", 5), ("gateway", 5)])
    observation = AsyncMock(side_effect=[failed, ready])
    gateway = SimpleNamespace(bus=SimpleNamespace(get_owner=owner), observed_snapshot=observation)
    monkeypatch.setattr(remote_provisioning, "get_gateway", lambda: gateway)

    def evaluation(_spec, _row, *, observation, online):
        assert online is (observation is not None)
        return {
            "readiness": "READY" if observation is ready else "DEGRADED",
            "runtime_health": {"equipment": []},
        }

    monkeypatch.setattr(remote_provisioning, "evaluate_worker_readiness", evaluation)
    if "401" in failure:
        with pytest.raises(RuntimeError, match="vision 鉴权失败"):
            await worker._converge_readiness(AsyncMock())
        worker._reconnect_mcp.assert_not_awaited()
    else:
        result = await worker._converge_readiness(AsyncMock())
        assert result["readiness"] == "READY"
        worker._reconnect_mcp.assert_awaited_once_with("vision")
    worker._command.assert_awaited_once()
