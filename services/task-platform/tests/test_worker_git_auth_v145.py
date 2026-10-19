"""Real read-only Git auth through the per-environment askpass helper."""

from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select

from app.config.settings import get_settings
from app.database import SessionLocal
from app.domain import EnvironmentStatus
from app.models import DigitalEmployee, OpenCodeExecutionEnvironment
from app.runtime.maintenance import OperationLease, claim_next, submit_operation
from app.runtime.remote_provisioning import RemoteProvisioner
from app.worker_agent import inspection
from app.worker_agent.process_env import child_environ


def _git(*args, cwd=None, env=None):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


class GitHTTPServer:
    """Minimal smart-HTTP server backed by `git http-backend` with Basic auth."""

    def __init__(self, root: Path, token: str):
        self.root = root
        self.token = token
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/project.git"

    def _handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return None

            def do_GET(self):
                self._serve()

            def do_POST(self):
                self._serve()

            def _serve(self):
                expected = "Basic " + base64.b64encode(
                    f"git:{owner.token}".encode()
                ).decode()
                if self.headers.get("Authorization") != expected:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="git"')
                    self.end_headers()
                    return
                parsed = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                env = {
                    **os.environ,
                    "GIT_PROJECT_ROOT": str(owner.root),
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "PATH_INFO": parsed.path,
                    "QUERY_STRING": parsed.query,
                    "REQUEST_METHOD": self.command,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(length),
                }
                completed = subprocess.run(
                    ["git", "http-backend"],
                    input=body,
                    env=env,
                    capture_output=True,
                )
                raw = completed.stdout
                separator = b"\r\n\r\n" if b"\r\n\r\n" in raw else b"\n\n"
                head, _, payload = raw.partition(separator)
                status = 200
                headers = {}
                for line in head.decode("latin-1").splitlines():
                    key, _, value = line.partition(":")
                    if not value:
                        continue
                    if key.lower() == "status":
                        status = int(value.strip().split()[0])
                    else:
                        headers[key.strip()] = value.strip()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)

        return Handler

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def http_git(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _git("init", "-q", "-b", "main", cwd=source)
    _git("config", "user.name", "Nexus", cwd=source)
    _git("config", "user.email", "nexus@example.invalid", cwd=source)
    (source / "README.md").write_text("fixture\n", encoding="utf-8")
    _git("add", "README.md", cwd=source)
    _git("commit", "-q", "-m", "fixture", cwd=source)
    bare_root = tmp_path / "repos"
    bare_root.mkdir()
    _git("clone", "-q", "--bare", str(source), str(bare_root / "project.git"))

    token = "pat-correct-one"
    with GitHTTPServer(bare_root, token) as server:
        workspace = tmp_path / "workspace"
        _git("clone", "-q", str(bare_root / "project.git"), str(workspace))
        _git("remote", "set-url", "origin", server.url, cwd=workspace)

        token_file = tmp_path / "git.token"
        token_file.write_text(token, encoding="utf-8")
        token_file.chmod(0o600)
        askpass = tmp_path / "askpass"
        askpass.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  *Username*) printf '%s\\n' git ;;\n"
            f"  *) cat '{token_file}' ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        askpass.chmod(0o700)
        yield workspace, token, token_file, askpass, server


def _configure_git_env(monkeypatch, askpass: Path):
    monkeypatch.setenv("GIT_ASKPASS", str(askpass))
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "")


def test_installer_askpass_authenticates_real_http_git_without_secret_in_environment(
    http_git, tmp_path
):
    _workspace, token, token_file, _askpass, server = http_git
    script = (Path(__file__).resolve().parents[1] / "app/runtime/remote_install.sh").read_text(
        encoding="utf-8"
    )
    functions = script.split("# >>> nexus-installer-functions\n", 1)[1].split(
        "# <<< nexus-installer-functions", 1
    )[0]
    real_git = shutil.which("git")
    assert real_git
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "git.log"
    shim = bindir / "git"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "secret = os.environ.get('TEST_PAT', '')\n"
        "leaked = [k for k, v in os.environ.items() if k != 'TEST_PAT' and secret and secret in str(v)]\n"
        "askpass = os.environ.get('GIT_ASKPASS')\n"
        "ask_text = Path(askpass).read_text() if askpass and Path(askpass).is_file() else ''\n"
        "token_path = os.environ.get('TOKEN_PATH')\n"
        "mode = None\n"
        "if token_path and Path(token_path).is_file():\n"
        "    mode = oct(Path(token_path).stat().st_mode & 0o777)\n"
        "record = {'argv': sys.argv[1:], 'leaked_env': leaked, "
        "'leaked_argv': bool(secret and any(secret in str(a) for a in sys.argv[1:])), "
        "'askpass': ask_text, 'mode': mode}\n"
        "with open(os.environ['GIT_LOG'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(record) + '\\n')\n"
        f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    shim.chmod(0o700)
    workspace = tmp_path / "installer-clone"
    token_file.write_text(token, encoding="utf-8")
    token_file.chmod(0o644)
    body = (
        "set -euo pipefail\n"
        + functions
        + f"\npython_bin={shlex.quote(sys.executable)}\n"
        + f"workspace={shlex.quote(str(workspace))}\n"
        + f"workspace_real={shlex.quote(str(workspace))}\n"
        + "action=deploy\n"
        + f"repo_url={shlex.quote(server.url)}\n"
        + "repo_branch=main\n"
        + f"token_file={shlex.quote(str(token_file))}\n"
        + "repo_changed=false\n"
        + "remote_commit=''\n"
        + "repository_stage\n"
        + "printf 'RESULT\\t%s\\t%s\\n' \"$repo_changed\" \"$remote_commit\"\n"
    )
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "GIT_LOG": str(log),
        "TEST_PAT": token,
        "TOKEN_PATH": str(token_file),
        "GIT_TERMINAL_PROMPT": "0",
    }
    completed = subprocess.run(
        ["bash", "-c", body], text=True, capture_output=True, env=env, timeout=30
    )
    assert completed.returncode == 0, completed.stderr
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert records, (completed.stdout, completed.stderr)
    clone = next(item for item in records if "clone" in item["argv"])
    assert clone["leaked_env"] == []
    assert clone["leaked_argv"] is False
    assert token not in clone["askpass"]
    assert str(token_file) in clone["askpass"]
    assert clone["mode"] == oct(0o600)
    assert (workspace / "README.md").read_text(encoding="utf-8") == "fixture\n"
    assert not token_file.exists()


def test_correct_pat_reads_remote_and_wrong_pat_fails(http_git, monkeypatch):
    workspace, token, token_file, askpass, _server = http_git
    _configure_git_env(monkeypatch, askpass)

    observed = inspection._inspect_repository(str(workspace), set())
    assert observed["authentication_verified"] is True
    assert observed["verification_scope"] == "refs:read"
    assert observed["error"] is None

    token_file.write_text("pat-wrong", encoding="utf-8")
    observed = inspection._inspect_repository(str(workspace), {token})
    assert observed["authentication_verified"] is False
    assert "认证" in observed["error"]
    encoded = json.dumps(observed, ensure_ascii=False)
    assert token not in encoded
    assert "pat-wrong" not in encoded


def test_rotated_pat_is_read_without_restart(http_git, monkeypatch):
    workspace, token, token_file, askpass, server = http_git
    _configure_git_env(monkeypatch, askpass)
    assert inspection._inspect_repository(str(workspace), set())[
        "authentication_verified"
    ] is True

    server.token = "pat-rotated"
    token_file.write_text("pat-rotated", encoding="utf-8")
    observed = inspection._inspect_repository(str(workspace), set())
    assert observed["authentication_verified"] is True


def test_pat_never_appears_in_argv_or_child_environment(http_git, monkeypatch):
    workspace, token, _token_file, askpass, _server = http_git
    _configure_git_env(monkeypatch, askpass)
    calls = []
    original = inspection.subprocess.run

    def capture(command, **kwargs):
        calls.append((list(command), dict(kwargs.get("env") or {})))
        return original(command, **kwargs)

    monkeypatch.setattr(inspection.subprocess, "run", capture)
    observed = inspection._inspect_repository(str(workspace), {token})
    assert observed["authentication_verified"] is True
    for command, env in calls:
        assert token not in " ".join(str(item) for item in command)
        assert token not in " ".join(str(value) for value in env.values())


def test_child_environ_allows_only_safe_git_variables(monkeypatch):
    source = {
        "PATH": "/usr/bin",
        "GIT_ASKPASS": "/etc/nexus/git-1-askpass",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
        "NEXUS_WORKER_TOKEN": "wtk_secret",
        "NEXUS_GIT_TOKEN": "pat_secret",
        "ASTRA_GIT_TOKEN": "pat_secret",
    }
    cleaned = child_environ(source)
    for key in (
        "GIT_ASKPASS",
        "GIT_TERMINAL_PROMPT",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
    ):
        assert key in cleaned
    assert "NEXUS_WORKER_TOKEN" not in cleaned
    assert "NEXUS_GIT_TOKEN" not in cleaned
    assert "ASTRA_GIT_TOKEN" not in cleaned


def test_file_url_sanitizers_preserve_absolute_path():
    from app.runtime.worker_health import _sanitize_url as health_sanitize

    expected = "file:///srv/git/project.git"
    assert inspection._sanitize_url(expected + "?token=secret") == expected
    assert health_sanitize(expected + "#fragment") == expected


@pytest.mark.asyncio
async def test_deploy_uses_written_credential_revision(monkeypatch):
    monkeypatch.setattr(get_settings(), "mcp_transport", "streamable-http")
    async with SessionLocal() as db:
        db.add(DigitalEmployee(id="gitrev", name="gitrev", role="build"))
        await db.flush()
        db.add(
            OpenCodeExecutionEnvironment(
                employee_id="gitrev",
                mode="LINUX_REMOTE",
                name="linux",
                host="test.invalid",
                username="root",
                ssh_password="secret",
                workspace_root="/srv/project",
                status="AVAILABLE",
                status_detail={},
            )
        )
        await db.commit()
    async with SessionLocal() as db:
        environment = await db.scalar(
            select(OpenCodeExecutionEnvironment).where(
                OpenCodeExecutionEnvironment.employee_id == "gitrev"
            )
        )
        operation = await submit_operation(
            db, environment.id, "deploy", requested_by="test"
        )
        operation_id = operation.id
        db.expunge(environment)
    assert await claim_next("owner-gitrev") == operation_id
    lease = OperationLease(operation_id, 1, "owner-gitrev", 60)
    now = __import__("datetime").datetime.now(__import__("datetime").UTC)
    desired_repo = {
        "id": "repo",
        "url": "https://git.example/repo.git",
        "target_branch": "main",
        "authentication": "token",
        "credential_revision": "desired-new",
        "error": None,
    }
    probe = {
        "stale": False,
        "status": EnvironmentStatus.AVAILABLE,
        "desired": {"repository": desired_repo},
        "observation": {"source": "worker"},
        "equipment": [],
        "repository": desired_repo,
        "checked_at": now,
        "values": {
            "status": EnvironmentStatus.AVAILABLE,
            "last_checked_at": now,
            "status_detail": {
                "deployed": {"repository": dict(desired_repo)},
                "observed_at": now.isoformat(),
                "observed_epoch": 1,
            },
        },
        "observed_epoch": 1,
    }

    async def fake_observe(*_args, **_kwargs):
        return probe

    monkeypatch.setattr("app.runtime.maintenance.observe_environment", fake_observe)
    provisioner = RemoteProvisioner(
        environment,
        SimpleNamespace(id=operation_id, action="deploy"),
        lease=lease,
    )
    provisioner.git_repository_id = "repo"
    provisioner.git_credential_revision = "written-actual"
    assert await provisioner._finish(True) is True
    async with SessionLocal() as db:
        stored = await db.get(OpenCodeExecutionEnvironment, environment.id)
        deployed = stored.status_detail["deployed"]["repository"]
        assert deployed["credential_revision"] == "written-actual"
        assert deployed["target_branch"] == "main"


@pytest.mark.asyncio
async def test_token_rotation_does_not_restart_unchanged_units(monkeypatch):
    import hashlib

    from app.runtime.remote_provisioning import (
        RemoteProvisioner,
        agent_wheel,
        git_askpass_path,
        git_token_path,
    )

    settings = get_settings()
    monkeypatch.setattr(settings, "worker_agent_gateway_url", "ws://10.0.0.8:18100/api/worker-gateway/connect")
    monkeypatch.setattr(settings, "remote_gateway_local_url", "http://10.0.0.8:18110")
    monkeypatch.setattr(settings, "mcp_resource_server_url", "http://10.0.0.8:18201/mcp")
    environment = SimpleNamespace(
        id=77,
        employee_id="gitrot",
        workspace_root="/srv/project",
        opencode_server_port=14096,
        ssh_password="secret",
        model_name="",
        opencode_path="/usr/local/bin/opencode",
    )
    provisioner = RemoteProvisioner(environment, {"id": "op-gitrot", "action": "deploy"})
    provisioner.home = "/root"
    provisioner.config_path = "/etc/nexus/opencode/77/opencode.jsonc"
    provisioner.target_binary = "/root/.local/share/nexus/workers/77/bin/opencode"
    provisioner.git_token = "pat-rotated"
    provisioner.git_repository_id = "repo"
    provisioner.git_credential_revision = "revision"
    provisioner.installer_meta = {
        "changed": {
            "runtime": False,
            "configuration": False,
            "skills": False,
            "repository": False,
        }
    }
    digest = hashlib.sha256(agent_wheel()[1]).hexdigest()
    commands: list[str] = []
    atomic: list[tuple[str, int]] = []

    async def command(text, timeout=30):
        commands.append(text)
        if "wheel.sha256" in text and text.lstrip().startswith("cat "):
            return digest
        if "test -x" in text:
            return "yes"
        return ""

    async def atomic_write(path, _data, mode, *, backup=False):
        if path in {git_token_path(77), git_askpass_path(77)}:
            atomic.append((path, mode))
            return True
        return False

    async def safe(*_args, **_kwargs):
        return None

    async def idle(*_args, **_kwargs):
        return True

    async def credentials(_path):
        return "wtk", "wrk"

    async def emit(_event):
        return None

    monkeypatch.setattr(provisioner, "_command", command)
    monkeypatch.setattr(provisioner, "_write_atomic_if_changed", atomic_write)
    monkeypatch.setattr(provisioner, "_ensure_safe", safe)
    monkeypatch.setattr(provisioner, "_native_idle", idle)
    monkeypatch.setattr(provisioner, "_existing_worker_credentials", credentials)
    await provisioner._install_units(emit)

    assert (git_token_path(77), 0o600) in atomic
    assert (git_askpass_path(77), 0o700) in atomic
    systemd = next(item for item in commands if "systemctl enable" in item)
    assert "daemon-reload" not in systemd
    assert "restart nexus-opencode-77.service" not in systemd
    assert "restart nexus-worker-77.service" not in systemd


@pytest.mark.asyncio
async def test_generated_worker_env_authenticates_real_git_and_matches_unit(
    http_git, monkeypatch, tmp_path
):
    import hashlib

    from app.runtime import remote_provisioning
    from app.runtime.remote_provisioning import (
        RemoteProvisioner,
        agent_wheel,
        safe_git_environment,
    )

    workspace, _token, _token_file, _askpass, _server = http_git
    monkeypatch.setattr(
        remote_provisioning, "WORKER_SECRET_DIR", str(tmp_path / "etc-nexus")
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "worker_agent_gateway_url", "ws://10.0.0.8:18100/api/worker-gateway/connect")
    monkeypatch.setattr(settings, "remote_gateway_local_url", "http://10.0.0.8:18110")
    monkeypatch.setattr(settings, "mcp_resource_server_url", "http://10.0.0.8:18201/mcp")
    environment = SimpleNamespace(
        id=88,
        employee_id="gitwire",
        workspace_root=str(workspace),
        opencode_server_port=14096,
        ssh_password="secret",
        model_name="",
        opencode_path="/usr/local/bin/opencode",
    )
    provisioner = RemoteProvisioner(environment, {"id": "op-gitwire", "action": "deploy"})
    provisioner.home = "/root"
    provisioner.config_path = "/etc/nexus/opencode/88/opencode.jsonc"
    provisioner.target_binary = "/root/.local/share/nexus/workers/88/bin/opencode"
    provisioner.git_token = "pat-correct-one"
    provisioner.git_repository_id = "repo"
    provisioner.git_credential_revision = "revision"
    provisioner.installer_meta = {
        "changed": {
            "runtime": False,
            "configuration": False,
            "skills": False,
            "repository": False,
        }
    }
    wheel_digest = hashlib.sha256(agent_wheel()[1]).hexdigest()
    commands: list[str] = []
    written: dict[str, bytes] = {}

    async def command(text, timeout=30):
        commands.append(text)
        if "wheel.sha256" in text and text.lstrip().startswith("cat "):
            return wheel_digest
        if "test -x" in text:
            return "yes"
        return ""

    async def atomic(path, data, mode, *, backup=False):
        current = written.get(path)
        if current is None and str(tmp_path) in path and Path(path).exists():
            current = Path(path).read_bytes()
        if current == data:
            return False
        written[path] = data
        if str(tmp_path) in path:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(mode)
        return True

    async def safe(*_args, **_kwargs):
        return None

    async def idle(*_args, **_kwargs):
        return True

    async def credentials(_path):
        return "wtk", "wrk"

    async def emit(_event):
        return None

    monkeypatch.setattr(provisioner, "_command", command)
    monkeypatch.setattr(provisioner, "_write_atomic_if_changed", atomic)
    monkeypatch.setattr(provisioner, "_ensure_safe", safe)
    monkeypatch.setattr(provisioner, "_native_idle", idle)
    monkeypatch.setattr(provisioner, "_existing_worker_credentials", credentials)

    await provisioner._install_units(emit)
    env_path = f"{remote_provisioning.WORKER_SECRET_DIR}/worker-88.env"
    unit_path = "/etc/systemd/system/nexus-opencode-88.service"
    env_text = written[env_path].decode()
    unit_text = written[unit_path].decode()
    env_file = tmp_path / "worker-88.env"
    env_file.write_text(env_text, encoding="utf-8")
    loaded: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            loaded[key] = value
    safe_git = safe_git_environment(88)
    assert {key: loaded[key] for key in safe_git} == safe_git
    unit_git = {}
    for line in unit_text.splitlines():
        if line.startswith("Environment=GIT_"):
            key, sep, value = line.removeprefix("Environment=").partition("=")
            if sep:
                unit_git[key] = value
    assert unit_git == safe_git

    for key, value in loaded.items():
        monkeypatch.setenv(key, value)
    observed = inspection._inspect_repository(str(workspace), set())
    assert observed["authentication_verified"] is True

    # Second identical generation changes no env/unit content and does not
    # restart Worker/OpenCode (PID criteria).
    commands.clear()
    await provisioner._install_units(emit)
    assert written[env_path].decode() == env_text
    assert written[unit_path].decode() == unit_text
    systemd = next(item for item in commands if "systemctl enable" in item)
    assert "daemon-reload" not in systemd
    assert "restart nexus-worker-88.service" not in systemd
    assert "restart nexus-opencode-88.service" not in systemd
