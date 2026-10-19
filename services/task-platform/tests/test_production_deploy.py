"""Nexus v1.4.4 production deployment contract tests.

These tests run on macOS: they use temporary Git repositories, temporary SQLite,
real local listening ports/HTTP servers, and injected systemd/runner doubles.
Real Linux systemd behavior is covered by ops/deploy/tests/linux_smoke.sh and is
reported as unchecked when it cannot run here.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ops" / "deploy"))

from nexus_deploy.backup import BackupManager, BackupResult, sqlite_revision  # noqa: E402
from nexus_deploy.config import load_config, parse_env_file  # noqa: E402
from nexus_deploy.configgen import (  # noqa: E402
    config_digest,
    ensure_secrets,
    ensure_user_config,
    write_opencode_config,
    write_runtime_env,
)
from nexus_deploy.deploy import Deployer  # noqa: E402
from nexus_deploy.errors import BackupError, ConfigError, LockError, PrepareError  # noqa: E402
from nexus_deploy.gitops import Release, ReleaseBuilder, verify_git_environment  # noqa: E402
from nexus_deploy.nginx import render_nginx  # noqa: E402
from nexus_deploy.ports import OwnershipChecker, PortInspector  # noqa: E402
from nexus_deploy.probes import ProbeSuite, http_json_probe, mcp_probe  # noqa: E402
from nexus_deploy.records import DeploymentRecords  # noqa: E402
from nexus_deploy.runner import CommandResult, SubprocessRunner  # noqa: E402
from nexus_deploy.services import build_catalog  # noqa: E402
from nexus_deploy.systemd import render_unit  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def make_repo(tmp_path: Path, *, compatible: bool = True) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "deploy@example.invalid")
    _git(repo, "config", "user.name", "Deploy Test")
    (repo / "ops" / "deploy").mkdir(parents=True)
    (repo / "services" / "task-platform").mkdir(parents=True)
    (repo / "services" / "nexus-api").mkdir(parents=True)
    (repo / "services" / "agent-service").mkdir(parents=True)
    (repo / "web" / "nexus" / "dist").mkdir(parents=True)
    (repo / "ops" / "deploy" / "release.json").write_text(
        json.dumps({"database_compatible_with_previous": compatible}), encoding="utf-8"
    )
    (repo / "services" / "task-platform" / "pyproject.toml").write_text(
        "[project]\nname='nexus-test'\n", encoding="utf-8"
    )
    (repo / "services" / "nexus-api" / "requirements.txt").write_text("", encoding="utf-8")
    (repo / "services" / "agent-service" / "requirements.txt").write_text("", encoding="utf-8")
    (repo / "web" / "nexus" / "package.json").write_text("{}", encoding="utf-8")
    (repo / "web" / "nexus" / "dist" / "index.html").write_text("<div id=\"app\"></div>", encoding="utf-8")
    (repo / "marker.txt").write_text(str(repo), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "release")
    return repo, _git(repo, "rev-parse", "HEAD")


def make_config(
    tmp_path: Path,
    *,
    repo: Path | None = None,
    commit: str = "a" * 40,
    extra: dict[str, str] | None = None,
):
    config_dir = tmp_path / "etc"
    config_dir.mkdir(parents=True, exist_ok=True)
    values = {
        "NEXUS_REPO_URL": str(repo or tmp_path / "repo"),
        "NEXUS_COMMIT": commit,
        "NEXUS_RELEASES_DIR": str(tmp_path / "opt" / "releases"),
        "NEXUS_CURRENT_LINK": str(tmp_path / "opt" / "current"),
        "NEXUS_CONFIG_DIR": str(config_dir),
        "NEXUS_STATE_DIR": str(tmp_path / "state"),
        "NEXUS_DATA_ROOT": str(tmp_path / "var" / "data"),
        "NEXUS_LOG_DIR": str(tmp_path / "var" / "log"),
        "NEXUS_BACKUP_DIR": str(tmp_path / "state" / "backups"),
        "NEXUS_SYSTEMD_UNIT_DIR": str(tmp_path / "systemd"),
        "NEXUS_NGINX_CONF": str(tmp_path / "nginx" / "nexus.conf"),
        "NEXUS_NGINX_ENABLED": "false",
        "NEXUS_RUN_USER": "tester",
        "ASTRA_DATABASE_BACKEND": "sqlite",
        "ASTRA_MCP_TRANSPORT": "stdio",
        "ASTRA_GATEWAY_ENABLED": "true",
        "NEXUS_GATEWAY_PROBE_KEY": "probe-key",
        "ASTRA_OPENCODE_BINARY": "/bin/true",
        "OPENAI_BASE_URL": "https://models.example.invalid",
        "OPENAI_MODEL": "model-a",
    }
    values.update(extra or {})
    env_file = config_dir / "nexus.env"
    env_file.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8"
    )
    env_file.chmod(0o600)
    return load_config(env_file=env_file, environ={"NEXUS_CONFIG_DIR": str(config_dir)})


class FakeRunner:
    def __init__(self, *, fail_migrate: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail_migrate = fail_migrate
        self.envs: list[dict] = []

    def run(self, command, *, cwd=None, env=None, timeout=1800):
        self.calls.append([str(part) for part in command])
        self.envs.append(dict(env or {}))
        if self.fail_migrate and "upgrade" in command:
            return CommandResult([str(part) for part in command], 1, "", "migration failed")
        if any("bootstrap_chat_model.py" in str(part) for part in command):
            payload = {
                "type": "openai_credential",
                "credential_id": "cred-1",
                "model": "model-a",
                "parameters": {},
            }
            return CommandResult(
                [str(part) for part in command], 0, json.dumps(payload) + "\n", ""
            )
        return CommandResult([str(part) for part in command], 0, "", "")


class FakeBuilder:
    def __init__(self, path: Path, commit: str, *, compatible: bool = True) -> None:
        self.path = path
        self.commit = commit
        self.compatible = compatible
        self.prepared = 0

    def prepare(self) -> Release:
        self.prepared += 1
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "release.json").write_text(
            json.dumps(
                {
                    "commit": self.commit,
                    "database_compatible_with_previous": self.compatible,
                    "status": "complete",
                }
            ),
            encoding="utf-8",
        )
        (self.path / "services" / "task-platform" / ".venv" / "bin").mkdir(
            parents=True, exist_ok=True
        )
        (self.path / ".nexus-release-complete").write_text("ok\n", encoding="utf-8")
        return Release(self.path, self.commit, {"database_compatible_with_previous": self.compatible})


class FakeSystemd:
    def __init__(self, unit_dir: Path | None = None) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self._active = True
        self.main_pids: dict[str, int] = {}
        self.unit_dir = unit_dir

    def write_units(self, catalog):
        self.calls.append(("write_units", catalog.units()))
        if self.unit_dir is not None:
            self.unit_dir.mkdir(parents=True, exist_ok=True)
            for unit in catalog.units():
                (self.unit_dir / f"{unit}.service").write_text(
                    f"[Service]\nExecStart=/bin/true\n# {unit}\n", encoding="utf-8"
                )
        return []

    def daemon_reload(self):
        self.calls.append(("daemon-reload", ()))

    def enable(self, units):
        self.calls.append(("enable", tuple(units)))

    def restart(self, units):
        self.calls.append(("restart", tuple(units)))

    def stop(self, units):
        self.calls.append(("stop", tuple(units)))

    def is_active(self, unit):
        return self._active

    def main_pid(self, unit):
        return self.main_pids.get(unit, 1234 if self._active else 0)

    def control_group(self, unit):
        return f"/system.slice/{unit}.service"

    def status(self, units):
        return {unit: {"active": self._active, "main_pid": self.main_pid(unit)} for unit in units}


class FakeBackup:
    def __init__(self) -> None:
        self.calls = 0

    def backup(self, *, commit, revision=None):
        self.calls += 1
        return BackupResult(status="ok", database="sqlite", database_revision=revision, path="x")


class FakeProbes:
    def __init__(self) -> None:
        self.fail_service_once = False
        self.fail_always = False
        self.fail_name = ""

    def redis(self, *, required=True):
        from nexus_deploy.probes import ProbeResult

        return ProbeResult("Redis", True, "PONG")

    def sql(self, *, required=True):
        from nexus_deploy.probes import ProbeResult

        return ProbeResult("SQL", True, "SELECT 1")

    def service(self, service, catalog, secrets):
        from nexus_deploy.probes import ProbeResult

        if self.fail_always or (self.fail_name and service.name == self.fail_name):
            return ProbeResult(service.description, False, "injected failure")
        if self.fail_service_once:
            self.fail_service_once = False
            return ProbeResult(service.description, False, "injected once")
        return ProbeResult(service.description, True, "ok")

    def nginx_config(self):
        from nexus_deploy.probes import ProbeResult

        return ProbeResult("Nginx 配置", True, "nginx -t")

    def entry(self, *, gateway_enabled, agentscope_enabled=True):
        return []

    def model_cost_probe(self, enabled):
        from nexus_deploy.probes import ProbeResult

        return ProbeResult("模型计费探测", True, "skipped", skipped=True, required=False)


def seed_revision(config, revision: str) -> None:
    config.task_data_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.sqlite_path) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32))")
        connection.execute("DELETE FROM alembic_version")
        connection.execute("INSERT INTO alembic_version VALUES (?)", (revision,))


def make_deployer(
    config,
    tmp_path: Path,
    *,
    commit: str | None = None,
    compatible: bool = True,
    probes: FakeProbes | None = None,
    runner: FakeRunner | None = None,
    systemd: FakeSystemd | None = None,
    backup: FakeBackup | None = None,
) -> Deployer:
    selected_commit = commit or config.commit
    selected_probes = probes or FakeProbes()
    return Deployer(
        config,
        runner=runner or FakeRunner(),
        builder=FakeBuilder(config.release_dir(selected_commit), selected_commit, compatible=compatible),
        systemd=systemd or FakeSystemd(config.unit_dir),
        backup=backup or FakeBackup(),
        probes=selected_probes,
        probe_factory=lambda _config: selected_probes,
        check_platform=False,
        check_git=False,
        check_ports=False,
        sleep=lambda _: None,
        retry_seconds=0,
    )


# --------------------------------------------------------------------- config


def test_user_config_is_created_once(tmp_path):
    path = tmp_path / "nexus.env"
    assert ensure_user_config(path) is True
    first = path.read_text(encoding="utf-8")
    assert ensure_user_config(path) is False
    assert path.read_text(encoding="utf-8") == first


def test_config_and_secrets_are_preserved_and_task_key_is_shared(tmp_path):
    config = make_config(tmp_path, extra={"ASTRA_TASK_MCP_KEY": "shared-key"})
    user_before = config.user_env_file.read_text(encoding="utf-8")
    secrets, changed = ensure_secrets(config, config.user_values)
    assert changed is True
    first = config.secrets_file.read_text(encoding="utf-8")
    secrets_again, changed_again = ensure_secrets(config, config.user_values)
    assert changed_again is False
    assert config.secrets_file.read_text(encoding="utf-8") == first
    assert secrets["ASTRA_TASK_MCP_KEY"] == secrets["NEXUS_TASK_MCP_KEY"] == "shared-key"
    assert stat.S_IMODE(config.secrets_file.stat().st_mode) == 0o600
    assert config.user_env_file.read_text(encoding="utf-8") == user_before

    runtime, _ = write_runtime_env(config)
    assert runtime["ASTRA_ENVIRONMENT"] == "production"
    assert "shared-key" not in json.dumps(runtime)
    assert config.releases_dir.resolve() not in config.task_data_dir.resolve().parents
    assert config.releases_dir.resolve() not in config.nexus_api_data_dir.resolve().parents
    assert config.releases_dir.resolve() not in config.agent_workspace_dir.resolve().parents


def test_opencode_config_is_nexus_specific_and_does_not_touch_global(tmp_path, monkeypatch):
    home = tmp_path / "home"
    global_config = home / ".config" / "opencode" / "opencode.json"
    global_config.parent.mkdir(parents=True)
    global_config.write_text('{"provider":{"deepseek":{}}}', encoding="utf-8")
    before = hashlib.sha256(global_config.read_bytes()).hexdigest()
    monkeypatch.setenv("HOME", str(home))
    config = make_config(tmp_path)
    payload = write_opencode_config(config)
    assert payload["model"] == "nexus/model-a"
    assert payload["provider"]["nexus"]["options"]["baseURL"] == "https://models.example.invalid/v1"
    assert config.opencode_config_file.is_file()
    assert hashlib.sha256(global_config.read_bytes()).hexdigest() == before


def test_config_digest_does_not_include_secret_values(tmp_path):
    config = make_config(tmp_path, extra={"ASTRA_TASK_MCP_KEY": "top-secret"})
    ensure_secrets(config, config.user_values)
    write_runtime_env(config)
    digest = config_digest(config)
    assert len(digest) == 64
    assert "top-secret" not in digest


# ------------------------------------------------------------------------ git


def test_git_commit_is_locked_and_verified(tmp_path):
    repo, commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=commit)
    verify_git_environment(config, SubprocessRunner())

    bad = make_config(tmp_path, repo=repo, commit="f" * 40)
    with pytest.raises(PrepareError):
        verify_git_environment(bad, SubprocessRunner())

    other, _ = make_repo(tmp_path / "other")
    mismatched = make_config(tmp_path / "mismatch", repo=other, commit=commit)
    with pytest.raises(PrepareError):
        verify_git_environment(mismatched, SubprocessRunner())


def test_release_reuse_ignores_deployment_generated_tools(tmp_path, monkeypatch):
    repo, commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=commit)

    def build(release):
        tools = release / "tools"
        (tools / "node_modules" / ".bin").mkdir(parents=True)
        (tools / "package.json").write_text("{}", encoding="utf-8")
        (tools / "package-lock.json").write_text("{}", encoding="utf-8")
        (tools / "node_modules" / "pkg").mkdir()
        binary = tools / "node_modules" / ".bin" / "opencode"
        binary.write_text("#!/bin/sh\necho 1.18.35\n", encoding="utf-8")
        binary.chmod(0o755)
        venv = release / "services" / "task-platform" / ".venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").write_text("", encoding="utf-8")
        (release / ".nexus-release-complete").write_text("ok\n", encoding="utf-8")

    builder = ReleaseBuilder(
        config, SubprocessRunner(), skip_web_build=True, log=lambda _: None
    )
    monkeypatch.setattr(builder, "_build", build)
    first = builder.prepare()
    assert builder.prepare().path == first.path


def test_release_manifest_and_head_are_verified(tmp_path, monkeypatch):
    repo, commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=commit)
    builder = ReleaseBuilder(config, SubprocessRunner(), log=lambda _: None)
    monkeypatch.setattr(
        builder,
        "_build",
        lambda release: (release / ".nexus-release-complete").write_text("ok\n", encoding="utf-8"),
    )
    release = builder.prepare()
    assert _git(release.path, "rev-parse", "HEAD") == commit
    assert release.manifest["commit"] == commit
    assert release.manifest["database_compatible_with_previous"] is True
    assert (release.path / "release.json").is_file()
    again = builder.prepare()
    assert again.path == release.path


# -------------------------------------------------------------------- services


def test_service_catalog_and_units_are_one_process_each(tmp_path):
    repo, commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=commit)
    release = Release(config.release_dir(commit), commit, {})
    catalog = build_catalog(config, release)
    assert set(catalog.units()) == {
        "nexus-task-api",
        "nexus-runtime-beat",
        "nexus-runtime-control",
        "nexus-runtime-agent",
        "nexus-task-mcp",
        "nexus-agent-service",
        "nexus-api",
        "nexus-gateway",
        "nexus-opencode-server",
        "nexus-assistant-opencode",
    }
    for service in catalog.services:
        text = render_unit(config, service)
        assert "Type=oneshot" not in text
        assert "Restart=on-failure" in text
        assert "EnvironmentFile=" in text
        assert "nexus-astra" not in text
        assert "%h" not in text


def test_library_mcp_is_required_only_when_enabled(tmp_path):
    repo, commit = make_repo(tmp_path)
    disabled = make_config(tmp_path / "disabled", repo=repo, commit=commit)
    enabled = make_config(
        tmp_path / "enabled", repo=repo, commit=commit, extra={"ASTRA_MCP_TRANSPORT": "streamable-http"}
    )
    assert "nexus-library-mcp" not in build_catalog(
        disabled, Release(disabled.release_dir(commit), commit, {})
    ).units()
    assert "nexus-library-mcp" in build_catalog(
        enabled, Release(enabled.release_dir(commit), commit, {})
    ).units()


def test_nginx_routes_follow_enabled_modules(tmp_path):
    repo, commit = make_repo(tmp_path)
    enabled = make_config(
        tmp_path / "on", repo=repo, commit=commit, extra={"ASTRA_GATEWAY_ENABLED": "true"}
    )
    disabled = make_config(
        tmp_path / "off", repo=repo, commit=commit, extra={"ASTRA_GATEWAY_ENABLED": "false"}
    )
    assert "location /v1/" in render_nginx(enabled)
    assert "location /v1/" not in render_nginx(disabled)


# -------------------------------------------------------------- deploy success


def test_fresh_install_writes_success_only_after_readiness(tmp_path):
    repo, commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=commit)
    systemd = FakeSystemd()
    deployer = make_deployer(config, tmp_path, systemd=systemd)
    result = deployer.install()
    assert result.ok, result.message
    records = DeploymentRecords(config)
    assert records.installed.is_file()
    assert records.last_success()["commit"] == commit
    assert records.current_commit() == commit
    assert config.current_link.resolve() == config.release_dir(commit).resolve()
    restarts = [units for name, units in systemd.calls if name == "restart"]
    assert any("nexus-task-api" in units for units in restarts)
    assert records.last_success()["database_revision"] is None
    assert config.chat_model_file.is_file()
    assert "NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG=" in config.chat_model_file.read_text(encoding="utf-8")
    assert stat.S_IMODE(config.secrets_file.stat().st_mode) == 0o600


def test_readiness_failure_never_writes_success_marker(tmp_path):
    repo, commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=commit)
    probes = FakeProbes()
    probes.fail_name = "task-api"
    result = make_deployer(config, tmp_path, probes=probes).install()
    assert not result.ok
    records = DeploymentRecords(config)
    assert not records.installed.exists()
    assert not config.current_link.exists()
    assert records.last_attempt()["stage"] == "readiness"
    assert records.last_attempt()["status"] == "failed"


def test_build_failure_keeps_current_release(tmp_path):
    repo, old_commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=old_commit)
    seed_revision(config, "rev-a")
    assert make_deployer(config, tmp_path).install().ok
    old_target = config.current_link.resolve()

    new_repo, new_commit = make_repo(tmp_path / "new")
    new_config = make_config(
        tmp_path, repo=new_repo, commit=new_commit
    )
    class FailingBuilder(FakeBuilder):
        def prepare(self):
            raise PrepareError("injected build failure")

    deployer = Deployer(
        new_config,
        runner=FakeRunner(),
        builder=FailingBuilder(new_config.release_dir(new_commit), new_commit),
        systemd=FakeSystemd(),
        backup=FakeBackup(),
        probes=FakeProbes(),
        check_platform=False,
        check_git=False,
        check_ports=False,
        sleep=lambda _: None,
        retry_seconds=0,
    )
    result = deployer.install()
    assert not result.ok
    assert config.current_link.resolve() == old_target


def test_rollback_restores_previous_release_after_readiness_failure(tmp_path):
    repo, old_commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=old_commit)
    seed_revision(config, "rev-a")
    assert make_deployer(config, tmp_path).install().ok
    old_target = config.current_link.resolve()

    new_repo, new_commit = make_repo(tmp_path / "new")
    new_config = make_config(tmp_path, repo=new_repo, commit=new_commit)
    seed_revision(new_config, "rev-a")
    probes = FakeProbes()
    probes.fail_service_once = True
    deployer = make_deployer(new_config, tmp_path, commit=new_commit, probes=probes)
    result = deployer.install()
    assert not result.ok
    assert result.record["rollback"]["attempted"] is True
    assert result.record["rollback"]["ok"] is True, result.record["rollback"]
    assert new_config.current_link.resolve() == old_target
    assert DeploymentRecords(new_config).last_attempt()["stage"] == "readiness"


def test_missing_snapshot_blocks_rollback_instead_of_guessing(tmp_path):
    repo, old_commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=old_commit)
    seed_revision(config, "rev-a")
    assert make_deployer(config, tmp_path).install().ok
    shutil.rmtree(config.state_dir / "snapshots" / old_commit)

    new_repo, new_commit = make_repo(tmp_path / "new")
    new_config = make_config(tmp_path, repo=new_repo, commit=new_commit)
    seed_revision(new_config, "rev-a")
    probes = FakeProbes()
    probes.fail_service_once = True
    result = make_deployer(new_config, tmp_path, commit=new_commit, probes=probes).install()
    assert not result.ok
    assert result.record["rollback"]["attempted"] is True
    assert result.record["rollback"]["ok"] is False
    assert "缺少部署快照" in result.record["rollback"]["detail"]


def test_incompatible_database_change_blocks_rollback(tmp_path):
    repo, old_commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=old_commit)
    assert make_deployer(config, tmp_path).install().ok

    new_repo, new_commit = make_repo(tmp_path / "new", compatible=False)
    new_config = make_config(tmp_path, repo=new_repo, commit=new_commit)
    probes = FakeProbes()
    probes.fail_service_once = True

    class RevisionDeployer(Deployer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.revisions = iter(["old-revision", "new-revision"])

        def _read_revision(self, commit):
            try:
                return next(self.revisions)
            except StopIteration:
                return "new-revision"

    deployer = RevisionDeployer(
        new_config,
        runner=FakeRunner(),
        builder=FakeBuilder(new_config.release_dir(new_commit), new_commit, compatible=False),
        systemd=FakeSystemd(),
        backup=FakeBackup(),
        probes=probes,
        check_platform=False,
        check_git=False,
        check_ports=False,
        sleep=lambda _: None,
        retry_seconds=0,
    )
    result = deployer.install()
    assert not result.ok
    assert result.record["rollback_blocked"] is True
    assert "人工" in result.record["rollback"]["detail"]
    assert new_config.current_link.resolve() == new_config.release_dir(new_commit).resolve()


def test_concurrent_deploy_is_rejected(tmp_path):
    repo, commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=commit)
    records = DeploymentRecords(config)
    with records.lock():
        with pytest.raises(LockError):
            make_deployer(config, tmp_path).install()



# --------------------------------------------------------------- port ownership


def test_port_owner_must_be_the_expected_unit(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        inspector = PortInspector(SubprocessRunner())
        if not inspector.listening_pids(port):
            pytest.skip("本机没有可用于端口归属测试的 ss/lsof")
        owned = inspector.inspect(
            port, unit="nexus-task-api", main_pid=os.getpid(), control_group=""
        )
        assert owned.owned is True
        unknown = inspector.inspect(port, unit="nexus-api", main_pid=0, control_group="")
        assert unknown.owned is False
        assert "不属于" in unknown.detail or "未知" in unknown.detail


def test_ownership_checker_uses_cgroup_for_child_processes(tmp_path):
    class FakeInspector:
        def inspect(self, port, *, unit, main_pid=0, control_group=""):
            from nexus_deploy.ports import PortStatus

            return PortStatus(port, [99], False, True, unit, f"由 {unit}.service 监听")

    repo, commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=commit)
    checker = OwnershipChecker(
        config,
        FakeInspector(),
        main_pid=lambda unit: 1,
        control_group=lambda unit: f"/system.slice/{unit}.service",
    )
    statuses = checker.check({"x": (1234, "nexus-task-api")})
    assert statuses[0].owned is True


# -------------------------------------------------------------------- probes


class _Handler(BaseHTTPRequestHandler):
    mode = "ok"
    mcp_key = "secret"

    def log_message(self, *args):
        return

    def _send(self, status: int, body: bytes, content_type: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.mode == "bad-structure" and self.path == "/api/health":
            self._send(200, json.dumps({"healthy": True}).encode())
            return
        if self.path == "/":
            self._send(200, b'<!DOCTYPE html><div id="app"></div>', "text/html")
        elif self.path == "/api/health":
            self._send(200, json.dumps({"status": "ok"}).encode())
        elif self.path == "/api/v1/me":
            # Nginx overwrites X-Verified-UID from X-Remote-User.
            uid = self.headers.get("X-Remote-User", "")
            if not uid:
                self._send(401, b'{"detail":"missing"}')
            else:
                self._send(200, json.dumps({"uid": uid}).encode())
        elif self.path == "/agentscope-api/health":
            self._send(
                200,
                json.dumps(
                    {
                        "status": "ok",
                        "version": "2.0.10",
                        "components": {"storage": "ok", "chat_service": "ok"},
                    }
                ).encode(),
            )
        elif self.path == "/v1/models":
            if self.headers.get("Authorization") != f"Bearer {self.mcp_key}":
                self._send(401, b'{"error":{"message":"missing key"}}')
            else:
                self._send(200, json.dumps({"data": []}).encode())
        else:
            self._send(404, b'{"detail":"not found"}')

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        if self.path == "/mcp":
            auth = self.headers.get("Authorization", "")
            employee = self.headers.get("x-nexus-employee-id", "")
            if auth != f"Bearer {self.mcp_key}" or (self.path == "/mcp" and not employee):
                self._send(401, b'{"ok":false}')
                return
            self._send(
                200,
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "serverInfo": {"name": "nexus-test", "version": "1"},
                        },
                    }
                ).encode(),
            )
            return
        self._send(404, b"{}")


@pytest.fixture
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


def test_nginx_entry_probes_validate_structure(tmp_path, http_server):
    repo, commit = make_repo(tmp_path)
    config = make_config(
        tmp_path,
        repo=repo,
        commit=commit,
        extra={
            "NEXUS_ENTRY_URL": http_server,
            "NEXUS_NGINX_ENABLED": "true",
            "NEXUS_GATEWAY_PROBE_KEY": "secret",
        },
    )
    suite = ProbeSuite(config, FakeRunner())
    results = suite.entry(gateway_enabled=True)
    assert [item.ok for item in results] == [True, True, True, True, True]
    _Handler.mode = "bad-structure"
    try:
        assert http_json_probe(
            "bad", f"{http_server}api/health", expected={"status": "ok"}
        ).ok is False
    finally:
        _Handler.mode = "ok"


def test_mcp_probe_requires_authentication(http_server):
    url = f"{http_server}mcp"
    ok = mcp_probe(
        "task-mcp",
        url,
        headers={"Authorization": "Bearer secret", "x-nexus-employee-id": "probe"},
    )
    assert ok.ok is True
    unauthorized = mcp_probe(
        "task-mcp", url, headers={"Authorization": "Bearer wrong", "x-nexus-employee-id": "probe"}
    )
    assert unauthorized.ok is False
    assert "401" in unauthorized.detail


def test_sqlite_sql_and_backup_are_real(tmp_path):
    repo, commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=commit)
    config.task_data_dir.mkdir(parents=True)
    with sqlite3.connect(config.sqlite_path) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        connection.execute("INSERT INTO alembic_version VALUES ('abc123')")
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO sample(value) VALUES ('kept')")
    assert sqlite_revision(config.sqlite_path) == "abc123"
    manager = BackupManager(config, FakeRunner(), which=lambda name: f"/usr/bin/{name}")
    result = manager.backup(commit=commit, revision="abc123")
    assert result.status == "ok"
    assert result.restore_verified is True
    with sqlite3.connect(result.path) as copy:
        assert copy.execute("SELECT value FROM sample").fetchone()[0] == "kept"
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    config.nexus_api_data_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.nexus_api_sqlite_path) as connection:
        connection.execute("CREATE TABLE employees (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO employees VALUES ('e1')")
    result = manager.backup(commit=commit, revision="abc123")
    api_backup = Path(result.extra["sqlite_databases"][1]["path"])
    assert api_backup.is_file()
    with sqlite3.connect(api_backup) as copy:
        assert copy.execute("SELECT id FROM employees").fetchone()[0] == "e1"
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_mysql_backup_requires_explicit_tools_and_ack(tmp_path):
    repo, commit = make_repo(tmp_path)
    config = make_config(
        tmp_path,
        repo=repo,
        commit=commit,
        extra={"ASTRA_DATABASE_BACKEND": "mysql"},
    )
    manager = BackupManager(config, FakeRunner(), which=lambda name: None)
    with pytest.raises(BackupError, match="mysqldump"):
        manager.backup(commit=commit)
    manager = BackupManager(config, FakeRunner(), which=lambda name: f"/usr/bin/{name}")
    with pytest.raises(BackupError, match="NEXUS_MYSQL_BACKUP_ACKNOWLEDGED"):
        manager.backup(commit=commit)


def test_production_entry_does_not_migrate_or_build():
    source = (ROOT / "ops" / "dev" / "astra_service.py").read_text(encoding="utf-8")
    assert "nexus_deploy" in source
    assert "ops/deploy/install_nexus.sh" in source
    assert "if args.mode == \"pro\"" in source


def test_install_wrapper_is_thin():
    script = (ROOT / "ops" / "deploy" / "install_nexus.sh").read_text(encoding="utf-8")
    assert "nexus_deploy" in script
    result = subprocess.run(
        ["bash", "-n", str(ROOT / "ops" / "deploy" / "install_nexus.sh")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_optional_modules_are_not_in_core_catalog(tmp_path):
    repo, commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=commit)
    catalog = build_catalog(config, Release(config.release_dir(commit), commit, {}))
    units = set(catalog.units())
    assert "nexus-llm-wiki-tunnel" not in units
    assert "nexus-llm-wiki-pump" not in units

    from nexus_deploy.optional import llm_wiki_dependencies, remote_worker_dependencies

    assert "LLM_WIKI_BUNDLE" in llm_wiki_dependencies(config)
    assert remote_worker_dependencies(config) == ["--remote-worker HOST", "REMOTE_WORKER_SSH_PASSWORD"]
    source = (ROOT / "ops" / "deploy" / "optional" / "llm_wiki.sh").read_text(encoding="utf-8")
    assert "kubectl" not in source
    assert "nexus-astra" not in source


def test_wiki_runtime_config_is_generated_when_enabled(tmp_path):
    repo, commit = make_repo(tmp_path)
    config = make_config(
        tmp_path,
        repo=repo,
        commit=commit,
        extra={"ASTRA_LLM_WIKI_ENABLED": "true", "LLM_WIKI_LOCAL_PORT": "19828"},
    )
    runtime, _ = write_runtime_env(config)
    assert runtime["ASTRA_LLM_WIKI_ENABLED"] == "true"
    assert runtime["ASTRA_LLM_WIKI_API_BASE_URL"] == "http://127.0.0.1:19828/api/v1"


def test_production_env_template_matches_default_ports():
    template = (ROOT / "ops" / "env" / "nexus-production.env.example").read_text(encoding="utf-8")
    for key, value in {
        "NEXUS_TASK_API_PORT": "18100",
        "NEXUS_GATEWAY_PORT": "18110",
        "NEXUS_NEXUS_API_PORT": "18120",
        "NEXUS_LIBRARY_MCP_PORT": "18201",
        "NEXUS_TASK_MCP_PORT": "18202",
        "NEXUS_AGENT_SERVICE_PORT": "18000",
    }.items():
        assert f"{key}={value}" in template
    assert "NEXUS_COMMIT=" in template
    assert "cursor/" not in template


def test_production_entry_refuses_in_production(tmp_path):
    script = ROOT / "ops" / "dev" / "astra_service.py"
    result = subprocess.run(
        [sys.executable, str(script), "pro", "start"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "ops" / "dev")},
    )
    if result.returncode == 1 and "psutil" in result.stderr:
        pytest.skip("测试解释器缺少 psutil")
    assert result.returncode == 2
    assert "install_nexus.sh" in result.stdout
    assert "不再迁移、构建或托管" in result.stdout


# ---------------------------------------------------- real readiness regressions


class _JsonHandler(BaseHTTPRequestHandler):
    payload: object = {}
    status = 200
    content_type = "application/json"
    body: bytes | None = None
    seen_headers: dict[str, str] = {}

    def log_message(self, *args):
        return

    def do_GET(self):
        self.seen_headers = dict(self.headers)
        raw = self.body if self.body is not None else json.dumps(self.payload).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", self.content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        self.do_GET()


def _server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


class _SequenceRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.envs: list[dict] = []
        self.calls: list[list[str]] = []

    def run(self, command, *, cwd=None, env=None, timeout=1800):
        self.calls.append([str(part) for part in command])
        self.envs.append(dict(env or {}))
        stdout = self.responses.pop(0) if self.responses else ""
        return CommandResult([str(part) for part in command], 0, stdout, "")


def test_beat_probe_requires_broker_and_consumer(tmp_path):
    repo, commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=commit)
    release = Release(config.release_dir(commit), commit, {})
    catalog = build_catalog(config, release)
    beat = catalog.by_name("runtime-beat")
    assert beat.probe == "celery-beat"

    node = beat.celery_node
    ok_runner = _SequenceRunner(
        [
            repr({node: {"ok": "pong"}}),
            repr({node: [{"name": "nexus-control"}]}),
        ]
    )
    assert ProbeSuite(config, ok_runner).service(beat, catalog, {}).ok is True

    missing_consumer = _SequenceRunner([repr({node: {"ok": "pong"}}), repr({node: []})])
    result = ProbeSuite(config, missing_consumer).service(beat, catalog, {})
    assert result.ok is False
    assert "队列" in result.detail


def test_gateway_runtime_healthy_false_fails(tmp_path):
    repo, commit = make_repo(tmp_path)
    handler = type(
        "GatewayFalse",
        (_JsonHandler,),
        {"payload": {"status": "ok", "runtime_healthy": False}},
    )
    server, base = _server(handler)
    try:
        port = server.server_address[1]
        config = make_config(tmp_path, repo=repo, commit=commit)
        config = type(config)(**{**config.__dict__, "ports": {**config.ports, "gateway": port}})
        catalog = build_catalog(config, Release(config.release_dir(commit), commit, {}))
        result = ProbeSuite(config, _SequenceRunner([""])).service(
            catalog.by_name("gateway"), catalog, {}
        )
        assert result.ok is False
        assert "runtime_healthy" in result.detail

        handler.payload = {"status": "ok", "runtime_healthy": True}
        result = ProbeSuite(config, _SequenceRunner([""])).service(
            catalog.by_name("gateway"), catalog, {}
        )
        assert result.ok is True
    finally:
        server.shutdown()
        server.server_close()


def test_agent_health_protocol_is_validated(tmp_path):
    repo, commit = make_repo(tmp_path)
    handler = type(
        "AgentHealth",
        (_JsonHandler,),
        {
            "payload": {
                "status": "ok",
                "version": "2.0.10",
                "components": {"storage": "ok", "chat_service": "ok"},
            }
        },
    )
    server, _base = _server(handler)
    try:
        port = server.server_address[1]
        config = make_config(tmp_path, repo=repo, commit=commit)
        config = type(config)(**{**config.__dict__, "ports": {**config.ports, "agent_service": port}})
        catalog = build_catalog(config, Release(config.release_dir(commit), commit, {}))
        suite = ProbeSuite(config, _SequenceRunner([""]))
        assert suite.service(catalog.by_name("agent-service"), catalog, {}).ok is True

        handler.payload = {"status": "ok"}
        assert suite.service(catalog.by_name("agent-service"), catalog, {}).ok is False
        handler.payload = {
            "status": "not_ready",
            "version": "2.0.10",
            "components": {"storage": "not_ready"},
        }
        result = suite.service(catalog.by_name("agent-service"), catalog, {})
        assert result.ok is False
        assert "not_ready" in result.detail
    finally:
        server.shutdown()
        server.server_close()


def test_mcp_probe_rejects_200_without_protocol_payload(tmp_path):
    class BadMcp(_JsonHandler):
        payload = {"jsonrpc": "2.0", "id": 1, "result": {}}

    server, base = _server(BadMcp)
    try:
        result = mcp_probe(
            "task-mcp",
            f"{base}/mcp",
            headers={"Authorization": "Bearer secret", "x-nexus-employee-id": "probe"},
        )
        assert result.ok is False
        assert "protocolVersion" in result.detail
    finally:
        server.shutdown()
        server.server_close()


def test_sse_mcp_response_is_parsed(tmp_path):
    sse_payload = (
        b"event: message\n"
        b'data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18",'
        b'"serverInfo":{"name":"nexus","version":"1"}}}\n\n'
    )

    class SseMcp(_JsonHandler):
        payload = None
        body = sse_payload
        content_type = "text/event-stream"

    server, base = _server(SseMcp)
    try:
        result = mcp_probe(
            "task-mcp",
            f"{base}/mcp",
            headers={"Authorization": "Bearer secret", "x-nexus-employee-id": "probe"},
        )
        assert result.ok is True
    finally:
        server.shutdown()
        server.server_close()


def test_gateway_entry_without_probe_key_fails(tmp_path, http_server):
    repo, commit = make_repo(tmp_path)
    config = make_config(
        tmp_path,
        repo=repo,
        commit=commit,
        extra={
            "NEXUS_ENTRY_URL": http_server,
            "NEXUS_NGINX_ENABLED": "true",
            "NEXUS_GATEWAY_PROBE_KEY": "",
        },
    )
    results = ProbeSuite(config, _SequenceRunner([""])).entry(gateway_enabled=True)
    gateway = results[-1]
    assert gateway.name == "Nginx /v1/"
    assert gateway.ok is False
    assert "NEXUS_GATEWAY_PROBE_KEY" in gateway.detail


def test_port_with_listener_but_no_pid_is_not_treated_free(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        class BlankRunner:
            def run(self, command, *, cwd=None, env=None, timeout=1800):
                return CommandResult([str(part) for part in command], 0, "", "")

        status = PortInspector(BlankRunner()).inspect(port, unit="nexus-api")
        assert status.owned is False
        assert "未给出 PID" in status.detail


# ------------------------------------------------------------- config/secrets


def test_secrets_are_merged_into_effective_database_config(tmp_path):
    repo, commit = make_repo(tmp_path)
    config = make_config(
        tmp_path,
        repo=repo,
        commit=commit,
        extra={"ASTRA_DATABASE_BACKEND": "mysql", "ASTRA_DATABASE_NAME": "nexus"},
    )
    config.secrets_file.write_text(
        "ASTRA_DATABASE_PASSWORD=db-canary\nOPENAI_API_KEY=key-canary\n",
        encoding="utf-8",
    )
    config.secrets_file.chmod(0o600)
    config = load_config(
        env_file=config.user_env_file, environ={"NEXUS_CONFIG_DIR": str(config.config_dir)}
    )
    assert config.database_password == "db-canary"

    runner = _SequenceRunner(["1"])
    result = ProbeSuite(config, runner).sql()
    assert result.ok is True
    assert runner.envs[-1]["MYSQL_PWD"] == "db-canary"

    from dataclasses import replace

    config = replace(config, mysql_backup_acknowledged=True)
    # The backup command must receive the merged password through MYSQL_PWD, not argv.
    class DumpRunner(_SequenceRunner):
        def run(self, command, *, cwd=None, env=None, timeout=1800):
            result = super().run(command, cwd=cwd, env=env, timeout=timeout)
            (Path(command[command.index("--result-file") + 1])).write_text(
                "CREATE TABLE sample (id int);\n", encoding="utf-8"
            )
            return result

    dump = DumpRunner([""])
    BackupManager(config, dump, which=lambda name: f"/usr/bin/{name}").backup(commit=commit)
    assert dump.envs[-1]["MYSQL_PWD"] == "db-canary"
    assert all("db-canary" not in part for part in dump.calls[-1])


def test_database_url_is_parsed_or_rejected(tmp_path):
    repo, commit = make_repo(tmp_path)
    sqlite_file = tmp_path / "var" / "data" / "url.db"
    config = make_config(
        tmp_path,
        repo=repo,
        commit=commit,
        extra={"ASTRA_DATABASE_URL": f"sqlite+aiosqlite:///{sqlite_file}"},
    )
    assert config.database_backend == "sqlite"
    assert config.sqlite_path == sqlite_file

    mysql = make_config(
        tmp_path / "mysql",
        repo=repo,
        commit=commit,
        extra={
            "ASTRA_DATABASE_URL": "mysql+asyncmy://user:url-pass@db.example:3307/nexus"
        },
    )
    assert mysql.database_backend == "mysql"
    assert mysql.database_host == "db.example"
    assert mysql.database_port == 3307
    assert mysql.database_name == "nexus"
    assert mysql.database_username == "user"
    assert mysql.database_password == "url-pass"

    with pytest.raises(ConfigError):
        make_config(
            tmp_path / "relative",
            repo=repo,
            commit=commit,
            extra={"ASTRA_DATABASE_URL": "sqlite:///./relative.db"},
        )


def test_env_line_roundtrips_systemd_sensitive_values(tmp_path):
    config = make_config(tmp_path)
    from nexus_deploy.configgen import env_line

    value = '{"type":"openai_credential","credential_id":"a b$c","parameters":{}}'
    line = env_line("NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG", value)
    path = config.chat_model_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(line, encoding="utf-8")
    assert parse_env_file(path)["NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG"] == value
    release = Release(config.release_dir(), config.commit, {})
    unit = render_unit(config, build_catalog(config, release).by_name("task-api"))
    assert f"EnvironmentFile=-{config.chat_model_file}" in unit
    assert f"EnvironmentFile=-{config.optional_env_file}" in unit


def test_task_config_is_initialized_without_overwriting(tmp_path):
    repo, commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=commit)
    release_path = config.release_dir(commit)
    defaults = release_path / "services" / "task-platform" / "configs"
    defaults.mkdir(parents=True)
    (defaults / "employees.yaml").write_text("default\n", encoding="utf-8")
    (defaults / "new.yaml").write_text("new\n", encoding="utf-8")
    config.task_config_dir.mkdir(parents=True)
    (config.task_config_dir / "employees.yaml").write_text("custom\n", encoding="utf-8")
    from nexus_deploy.configgen import bootstrap_task_config

    assert bootstrap_task_config(config, release_path) == 1
    assert (config.task_config_dir / "employees.yaml").read_text(encoding="utf-8") == "custom\n"
    assert (config.task_config_dir / "new.yaml").read_text(encoding="utf-8") == "new\n"


# ----------------------------------------------------------- recovery/pruning


def test_migration_failure_never_restarts_old_code_even_if_revision_unchanged(tmp_path):
    repo, old_commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=old_commit)
    seed_revision(config, "rev-a")
    assert make_deployer(config, tmp_path).install().ok

    new_repo, new_commit = make_repo(tmp_path / "new")
    new_config = make_config(tmp_path, repo=new_repo, commit=new_commit)
    seed_revision(new_config, "rev-a")
    runner = FakeRunner(fail_migrate=True)
    deployer = make_deployer(new_config, tmp_path, commit=new_commit, runner=runner)
    result = deployer.install()
    assert not result.ok
    assert result.record["rollback_blocked"] is True
    assert result.record["rollback"]["attempted"] is False
    assert "部分 DDL" in result.record["rollback"]["detail"]


def test_rollback_restores_snapshot_config_files(tmp_path):
    repo, old_commit = make_repo(tmp_path)
    config = make_config(
        tmp_path,
        repo=repo,
        commit=old_commit,
        extra={"OPENAI_MODEL": "model-a", "NEXUS_TASK_API_PORT": "18100"},
    )
    seed_revision(config, "rev-a")
    assert make_deployer(config, tmp_path).install().ok
    old_opencode = config.opencode_config_file.read_text(encoding="utf-8")

    new_repo, new_commit = make_repo(tmp_path / "new")
    new_config = make_config(
        tmp_path,
        repo=new_repo,
        commit=new_commit,
        extra={
            "OPENAI_MODEL": "model-b",
            "NEXUS_TASK_API_PORT": "28100",
            "ASTRA_GATEWAY_ENABLED": "false",
        },
    )
    seed_revision(new_config, "rev-a")
    probes = FakeProbes()
    probes.fail_service_once = True
    result = make_deployer(new_config, tmp_path, commit=new_commit, probes=probes).install()
    assert not result.ok
    assert result.record["rollback"]["ok"] is True
    restored_runtime = parse_env_file(new_config.runtime_env_file)
    assert restored_runtime["OPENAI_MODEL"] == "model-a"
    assert restored_runtime["NEXUS_TASK_API_PORT"] == "18100"
    assert restored_runtime["ASTRA_GATEWAY_ENABLED"] == "true"
    assert new_config.opencode_config_file.read_text(encoding="utf-8") == old_opencode
    assert (new_config.unit_dir / "nexus-gateway.service").is_file()
    assert new_config.current_link.resolve() == config.release_dir(old_commit).resolve()


def test_release_reuse_rejects_polluted_current_release(tmp_path, monkeypatch):
    repo, commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=commit)
    builder = ReleaseBuilder(config, SubprocessRunner(), log=lambda _: None)
    monkeypatch.setattr(
        builder,
        "_build",
        lambda release: (release / ".nexus-release-complete").write_text("ok\n", encoding="utf-8"),
    )
    release = builder.prepare()
    config.current_link.parent.mkdir(parents=True, exist_ok=True)
    config.current_link.symlink_to(release.path)
    (release.path / "marker.txt").write_text("polluted\n", encoding="utf-8")
    with pytest.raises(PrepareError, match="不可复用"):
        builder.prepare()


def test_prune_keeps_current_and_previous_success(tmp_path):
    repo, commit = make_repo(tmp_path, compatible=False)
    config = make_config(tmp_path, repo=repo, commit=commit, extra={"NEXUS_KEEP_RELEASES": "1"})
    records = DeploymentRecords(config)
    for name in ("a" * 40, "b" * 40, "c" * 40, "d" * 40):
        path = config.releases_dir / name
        path.mkdir(parents=True)
        (path / "marker").write_text(name, encoding="utf-8")
    records.write_success(
        commit="c" * 40,
        previous_commit="b" * 40,
        config_digest="x",
        database_revision="rev",
        checks=[],
    )
    config.current_link.parent.mkdir(parents=True, exist_ok=True)
    config.current_link.symlink_to(config.releases_dir / ("c" * 40))
    make_deployer(config, tmp_path)._prune_releases()
    remaining = {path.name for path in config.releases_dir.iterdir()}
    assert {"c" * 40, "b" * 40} <= remaining


def test_secret_canaries_never_enter_logs_or_records(tmp_path):
    repo, commit = make_repo(tmp_path)
    config = make_config(
        tmp_path,
        repo=repo,
        commit=commit,
        extra={
            "ASTRA_REDIS_URL": "redis://:redis-canary@127.0.0.1:6379/1",
            "ASTRA_DATABASE_PASSWORD": "db-canary",
        },
    )
    config.secrets_file.write_text(
        "ASTRA_DATABASE_PASSWORD=db-canary\nOPENAI_API_KEY=model-canary\n",
        encoding="utf-8",
    )
    config.secrets_file.chmod(0o600)
    from nexus_deploy.configgen import non_secret_summary, redact_text, sanitize_payload

    summary = non_secret_summary(
        {
            "ASTRA_REDIS_URL": {
                "before": "redis://:redis-canary@127.0.0.1:6379/1",
                "after": "redis://:redis-canary@127.0.0.1:6379/1",
            },
            "OPENAI_API_KEY": {"before": "model-canary", "after": "model-canary"},
        }
    )
    joined = "\n".join(summary)
    assert "redis-canary" not in joined
    assert "model-canary" not in joined
    assert "redis-canary" not in redact_text("failed redis://:redis-canary@h", ["redis-canary"])
    assert "model-canary" not in json.dumps(
        sanitize_payload({"message": "model-canary"}, ["model-canary"])
    )

    class FailingBuilder(FakeBuilder):
        def prepare(self):
            raise PrepareError("failed with model-canary")

    deployer = Deployer(
        config,
        runner=FakeRunner(),
        builder=FailingBuilder(config.release_dir(commit), commit),
        systemd=FakeSystemd(config.unit_dir),
        backup=FakeBackup(),
        probes=FakeProbes(),
        check_platform=False,
        check_git=False,
        check_ports=False,
        sleep=lambda _: None,
        retry_seconds=0,
    )
    result = deployer.install()
    assert not result.ok
    serialized = json.dumps(result.record, ensure_ascii=False)
    assert "model-canary" not in serialized
    assert "db-canary" not in serialized


def test_health_classifies_failed_and_healthy(tmp_path):
    repo, commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=commit)
    seed_revision(config, "rev-a")
    assert make_deployer(config, tmp_path).install().ok
    healthy = make_deployer(config, tmp_path).health()
    assert healthy.classification == "healthy"

    probes = FakeProbes()
    probes.fail_always = True
    failed = make_deployer(config, tmp_path, probes=probes).health()
    assert failed.classification == "failed"


def test_remote_worker_uses_provisioning_ndjson(tmp_path):
    repo, commit = make_repo(tmp_path)
    calls: list[tuple[str, str]] = []

    class WorkerHandler(_JsonHandler):
        def do_GET(self):
            calls.append(("GET", self.path))
            self._send_json(404, {"detail": "missing"})

        def do_POST(self):
            calls.append(("POST", self.path))
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            if self.path == "/api/employees":
                self._send_json(201, {"id": "nexus-remote"})
            elif self.path.endswith("/environment/deploy"):
                body = (
                    json.dumps({"type": "step", "name": "SSH", "state": "passed"})
                    + "\n"
                    + json.dumps({"type": "result", "success": True, "message": "部署完成"})
                    + "\n"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send_json(404, {"detail": "missing"})

        def do_PUT(self):
            calls.append(("PUT", self.path))
            self._send_json(200, {})

        def _send_json(self, status, payload):
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server, base = _server(WorkerHandler)
    try:
        port = server.server_address[1]
        config = make_config(
            tmp_path,
            repo=repo,
            commit=commit,
            extra={
                "NEXUS_TASK_API_PORT": str(port),
                "REMOTE_WORKER": "10.0.0.8",
                "REMOTE_WORKER_SSH_PASSWORD": "ssh-secret",
            },
        )
        from nexus_deploy.optional import run_remote_worker

        assert run_remote_worker(config) == 0
        assert ("POST", "/api/employees") in calls
        assert any(path.endswith("/environment/deploy") for _, path in calls)
    finally:
        server.shutdown()
        server.server_close()


def test_opencode_and_assistant_have_independent_units(tmp_path):
    repo, commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=commit)
    catalog = build_catalog(config, Release(config.release_dir(commit), commit, {}))
    opencode = catalog.by_name("opencode-server")
    assistant = catalog.by_name("assistant-opencode")
    assert opencode.unit == "nexus-opencode-server"
    assert "app.runtime.opencode_server" in opencode.command
    assert assistant.unit == "nexus-assistant-opencode"
    assert "app.assistant.server" in assistant.command
    assert opencode.port_key == "opencode"
    assert assistant.port_key == "assistant_opencode"

    external = make_config(
        tmp_path / "external",
        repo=repo,
        commit=commit,
        extra={
            "ASTRA_OPENCODE_SERVER_URL": "http://10.0.0.9:14096",
            "ASTRA_ASSISTANT_OPENCODE_URL": "http://10.0.0.9:14097",
        },
    )
    external_catalog = build_catalog(external, Release(external.release_dir(commit), commit, {}))
    assert external_catalog.by_name("opencode-server").unit == ""
    assert external_catalog.by_name("assistant-opencode").unit == ""
    assert external_catalog.by_name("opencode-server").base_url == "http://10.0.0.9:14096"

    disabled = make_config(
        tmp_path / "assistant-off",
        repo=repo,
        commit=commit,
        extra={"ASTRA_ASSISTANT_ENABLED": "false"},
    )
    assert "nexus-assistant-opencode" not in build_catalog(
        disabled, Release(disabled.release_dir(commit), commit, {})
    ).units()


def test_opencode_health_requires_healthy_and_version(tmp_path):
    repo, commit = make_repo(tmp_path)
    handler = type(
        "OpenCodeHealth",
        (_JsonHandler,),
        {"payload": {"healthy": True, "version": "1.18.35"}},
    )
    server, base = _server(handler)
    try:
        config = make_config(tmp_path, repo=repo, commit=commit)
        catalog = build_catalog(config, Release(config.release_dir(commit), commit, {}))
        service = catalog.by_name("opencode-server")
        service = type(service)(**{**service.__dict__, "base_url": base})
        suite = ProbeSuite(config, _SequenceRunner([""]))
        assert suite.service(service, catalog, {}).ok is True
        handler.payload = {"healthy": False, "version": "1.18.35"}
        assert suite.service(service, catalog, {}).ok is False
        handler.payload = {"healthy": True, "version": "9.9.9"}
        result = suite.service(service, catalog, {})
        assert result.ok is False
        assert "版本" in result.detail
    finally:
        server.shutdown()
        server.server_close()


def test_generated_files_are_not_world_readable(tmp_path):
    config = make_config(tmp_path, extra={"ASTRA_LLM_WIKI_ENABLED": "false"})
    ensure_secrets(config, config.user_values)
    write_runtime_env(config)
    write_opencode_config(config)
    for path in (config.secrets_file, config.runtime_env_file, config.opencode_config_file):
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


def test_wiki_optional_secrets_are_generated_once(tmp_path):
    repo, commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=commit)
    from nexus_deploy.optional import _ensure_wiki_secrets

    _ensure_wiki_secrets(config)
    first = parse_env_file(config.secrets_file)
    assert first["ASTRA_LLM_WIKI_API_TOKEN"]
    assert first["ASTRA_LLM_WIKI_PROJECT_ID"]
    _ensure_wiki_secrets(config)
    assert parse_env_file(config.secrets_file) == first


def test_manual_rollback_same_schema_and_verified_restore(tmp_path):
    repo, commit_a = make_repo(tmp_path / "a")
    config = make_config(tmp_path, repo=repo, commit=commit_a)
    seed_revision(config, "rev-a")
    assert make_deployer(config, tmp_path).install().ok

    repo_b, commit_b = make_repo(tmp_path / "b", compatible=False)
    config_b = make_config(tmp_path, repo=repo_b, commit=commit_b)
    seed_revision(config_b, "rev-b")
    assert make_deployer(config_b, tmp_path, commit=commit_b, compatible=False).install().ok

    failed = make_deployer(config_b, tmp_path, commit=commit_b, compatible=False).rollback(commit_a)
    assert failed.ok is False
    assert "--restored-revision" in failed.message

    # Same schema is safe without a restore precondition.
    seed_revision(config_b, "rev-a")
    same = make_deployer(config_b, tmp_path, commit=commit_b, compatible=False).rollback(commit_a)
    assert same.ok is True
    assert config_b.current_link.resolve() == config_b.release_dir(commit_a).resolve()


def test_restart_is_serialized_with_deploy_lock(tmp_path):
    repo, commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=commit)
    seed_revision(config, "rev-a")
    assert make_deployer(config, tmp_path).install().ok
    with DeploymentRecords(config).lock():
        with pytest.raises(LockError):
            make_deployer(config, tmp_path).restart()


def test_partial_stop_failure_recovers_snapshot(tmp_path):
    repo, old_commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=old_commit)
    seed_revision(config, "rev-a")
    assert make_deployer(config, tmp_path).install().ok
    old_target = config.current_link.resolve()

    new_repo, new_commit = make_repo(tmp_path / "new")
    new_config = make_config(tmp_path, repo=new_repo, commit=new_commit)
    seed_revision(new_config, "rev-a")

    class FailingStopSystemd(FakeSystemd):
        def __init__(self, unit_dir):
            super().__init__(unit_dir)
            self.failed_once = False

        def stop(self, units):
            super().stop(units)
            if not self.failed_once:
                self.failed_once = True
                raise RuntimeError("injected partial stop failure")

    deployer = make_deployer(
        new_config, tmp_path, commit=new_commit, systemd=FailingStopSystemd(new_config.unit_dir)
    )
    result = deployer.install()
    assert not result.ok
    assert result.stage == "stop"
    assert result.record["rollback"]["attempted"] is True
    assert result.record["rollback"]["ok"] is True, result.record["rollback"]
    assert new_config.current_link.resolve() == old_target


def test_readiness_failure_record_keeps_probe_details(tmp_path):
    repo, commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=commit)
    probes = FakeProbes()
    probes.fail_name = "task-api"
    result = make_deployer(config, tmp_path, probes=probes).install()
    assert not result.ok
    assert result.record["checks"]
    assert any(item["name"] == "Task API" for item in result.record["checks"])
    assert result.record["backup"]["status"] == "ok"


def test_revision_read_failure_is_not_treated_as_compatible(tmp_path):
    repo, commit = make_repo(tmp_path)
    config = make_config(tmp_path, repo=repo, commit=commit)
    deployer = make_deployer(config, tmp_path)
    assert deployer._database_compatible(None, None, None) is False
    assert deployer._database_compatible("a", None, None) is False


def test_linux_ss_failure_and_missing_pid_are_not_free(tmp_path, monkeypatch):
    from unittest.mock import patch

    class StaticRunner:
        def __init__(self, returncode, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

        def run(self, command, *, cwd=None, env=None, timeout=1800):
            return CommandResult([str(part) for part in command], self.returncode, self.stdout, self.stderr)

    failure = PortInspector(StaticRunner(1, "", "ss: command not found"))
    no_pid = PortInspector(
        StaticRunner(
            0,
            "LISTEN 0 4096 192.168.1.9:52345 0.0.0.0:*\n",
        )
    )
    owned = PortInspector(
        StaticRunner(0, "LISTEN 0 4096 192.168.1.9:52345 0.0.0.0:* users:((\"python\",pid=123,fd=3))\n")
    )
    with patch("nexus_deploy.ports.platform.system", return_value="Linux"):
        failed = failure.inspect(52345, unit="nexus-api")
        unknown = no_pid.inspect(52345, unit="nexus-api", main_pid=123)
        matched = owned.inspect(52345, unit="nexus-api", main_pid=123)
    assert failed.owned is False and failed.free is False
    assert "ss 检查失败" in failed.detail
    assert unknown.owned is False and unknown.free is False
    assert "未给出 PID" in unknown.detail
    assert matched.owned is True


def test_gateway_probe_key_is_created_once_and_persisted(tmp_path):
    repo, commit = make_repo(tmp_path)
    calls = {"post": 0}

    class KeyHandler(_JsonHandler):
        def do_POST(self):
            calls["post"] += 1
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            self.payload = {"key": "probe-created-secret", "id": "gk_1"}
            self.status = 201
            self.do_GET()

    server, _base = _server(KeyHandler)
    try:
        port = server.server_address[1]
        config = make_config(
            tmp_path,
            repo=repo,
            commit=commit,
            extra={"NEXUS_TASK_API_PORT": str(port), "NEXUS_GATEWAY_PROBE_KEY": ""},
        )
        deployer = make_deployer(config, tmp_path)
        assert deployer._ensure_gateway_probe_key({}).ok is True
        assert calls["post"] == 1
        stored = parse_env_file(config.secrets_file)
        assert stored["NEXUS_GATEWAY_PROBE_KEY"] == "probe-created-secret"
        assert stat.S_IMODE(config.secrets_file.stat().st_mode) == 0o600
        assert deployer._ensure_gateway_probe_key(stored).ok is True
        assert calls["post"] == 1
    finally:
        server.shutdown()
        server.server_close()


def test_mysql_backup_also_copies_nexus_api_sqlite(tmp_path):
    repo, commit = make_repo(tmp_path)
    config = make_config(
        tmp_path,
        repo=repo,
        commit=commit,
        extra={
            "ASTRA_DATABASE_BACKEND": "mysql",
            "NEXUS_MYSQL_BACKUP_ACKNOWLEDGED": "1",
        },
    )
    config.nexus_api_data_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.nexus_api_sqlite_path) as connection:
        connection.execute("CREATE TABLE employees (id TEXT)")
        connection.execute("INSERT INTO employees VALUES ('e2')")

    class DumpRunner(_SequenceRunner):
        def run(self, command, *, cwd=None, env=None, timeout=1800):
            result = super().run(command, cwd=cwd, env=env, timeout=timeout)
            Path(command[command.index("--result-file") + 1]).write_text(
                "CREATE TABLE sample (id int);\n", encoding="utf-8"
            )
            return result

    result = BackupManager(
        config, DumpRunner([""]), which=lambda name: f"/usr/bin/{name}"
    ).backup(commit=commit)
    api = Path(result.extra["sqlite_databases"][0]["path"])
    assert api.is_file()
    with sqlite3.connect(api) as copy:
        assert copy.execute("SELECT id FROM employees").fetchone()[0] == "e2"


def test_gateway_probe_key_missing_does_not_block_preflight(tmp_path):
    repo, commit = make_repo(tmp_path)
    config = make_config(
        tmp_path, repo=repo, commit=commit, extra={"NEXUS_GATEWAY_PROBE_KEY": ""}
    )
    deployer = make_deployer(config, tmp_path)
    checks = deployer._preflight()
    assert any(item["name"] == "Gateway 探针 key" for item in checks)
