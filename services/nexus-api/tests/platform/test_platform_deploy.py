"""统一部署契约：配置、来源、安全升级、真实子进程和身份验收。"""

import hashlib
import io
import os
import socket
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.platform.deployment_platform import DEPLOY, deploy, lifecycle
from tests.platform.deployment_platform import configuration as cfg


@pytest.fixture(autouse=True)
def local_connection_inventory(monkeypatch):
    # macOS 不允许当前普通用户枚举所有系统进程，测试只观察自身真实 socket。
    import psutil

    monkeypatch.setattr(
        psutil,
        "net_connections",
        lambda kind: [
            SimpleNamespace(status=c.status, laddr=c.laddr, pid=os.getpid())
            for c in psutil.Process().net_connections(kind=kind)
        ],
    )


def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "usable_address", lambda ip: ip == "7.1.2.3")
    return cfg.initialize(
        {
            "ASTRA_DATABASE_URL": "sqlite+aiosqlite:///" + str(tmp_path / "nexus.db"),
            "AGENTSCOPE_SQL_URL": "sqlite+aiosqlite:///" + str(tmp_path / "agent.db"),
            "ASTRA_REDIS_URL": "redis://127.0.0.1:6379/15",
            "NEXUS_DEPLOY_PUBLIC_IP": "7.1.2.3",
            "NEXUS_DEPLOY_WORKER_ALLOW": "7.0.0.0/8,10.1.0.0/16",
        },
        DEPLOY.parents[1],
        tmp_path,
        cfg.Dialog(False),
    )


def test_configuration_reinstall_keeps_keys_and_one_canonical_environment(tmp_path, monkeypatch):
    values = settings(tmp_path, monkeypatch)
    values["NEXUS_KB_EMBEDDING_JSON"] = '{"model_route_id":"embedding","dimensions":1024}'
    values["NEXUS_REMOTE_NPM_REGISTRY"] = "https://npm.company.test/"
    values["NEXUS_EXPERIENCE_REVIEW_GATEWAY_KEY"] = "agw_existing_review"
    file = tmp_path / "nexus.env"
    assert cfg.save_env(file, values)
    before = file.stat().st_mtime_ns
    loaded = cfg.load_config([file], {}, cfg.Dialog(False))
    again = cfg.initialize(loaded, DEPLOY.parents[1], tmp_path, cfg.Dialog(False))
    assert again == values
    assert not cfg.save_env(file, again)
    assert file.stat().st_mtime_ns == before
    assert file.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.backup-*"))
    assert values["NEXUS_REMOTE_GATEWAY_LOCAL_URL"] == "http://7.1.2.3:18130/v1"
    assert values["NEXUS_WORKER_AGENT_GATEWAY_URL"] == "ws://7.1.2.3:18130/api/worker-gateway/connect"


def test_backup_precedes_config_replacement_and_shell_input_is_never_executed(tmp_path):
    file = tmp_path / "nexus.env"
    original = {
        "NEXUS_VALUE": "literal$(touch sentinel) # comment apostrophe's \\regex",
        "ASTRA_DATABASE_URL": "mysql+asyncmy://u:p%40ss@db/nexus?charset=utf8mb4",
    }
    cfg.save_env(file, original)
    assert cfg.parse_env(file, cfg.Dialog(False)) == original
    cfg.save_env(file, dict(original, NEXUS_VALUE="changed"))
    backups = list(tmp_path.glob("nexus.env.backup-*"))
    assert len(backups) == 1
    assert cfg.parse_env(backups[0], cfg.Dialog(False)) == original
    assert not (tmp_path / "sentinel").exists()


def test_duplicate_and_conflicting_values_require_explicit_diagnosis(tmp_path):
    file = tmp_path / "input.env"
    file.write_text("NEXUS_VALUE=first\nNEXUS_VALUE=second\n")
    with pytest.raises(cfg.DeploymentError, match="交互"):
        cfg.parse_env(file, cfg.Dialog(False))
    dialog = cfg.Dialog()
    dialog.ask = Mock(return_value="r")
    assert cfg.parse_env(file, dialog)["NEXUS_VALUE"] == "second"
    file.write_text("NEXUS_VALUE=first\n")
    with pytest.raises(cfg.DeploymentError, match="交互"):
        cfg.load_config([file], {"NEXUS_VALUE": "second"}, cfg.Dialog(False))


def test_invalid_existing_key_never_automatically_regenerated(tmp_path, monkeypatch):
    values = settings(tmp_path, monkeypatch)
    values["ASTRA_GATEWAY_MASTER_KEY"] = "broken-key"
    with pytest.raises(cfg.DeploymentError, match="不能自动再生"):
        cfg.initialize(values, DEPLOY.parents[1], tmp_path, cfg.Dialog(False))


def test_shared_database_and_unrestricted_worker_acl_are_rejected(tmp_path, monkeypatch):
    values = settings(tmp_path, monkeypatch)
    values["AGENTSCOPE_SQL_URL"] = values["ASTRA_DATABASE_URL"]
    with pytest.raises(cfg.DeploymentError, match="独立数据库"):
        cfg.initialize(values, DEPLOY.parents[1], tmp_path, cfg.Dialog(False))
    values["AGENTSCOPE_SQL_URL"] += "agent"
    values["NEXUS_DEPLOY_WORKER_ALLOW"] = "0.0.0.0/0"
    with pytest.raises(cfg.DeploymentError, match="禁止"):
        cfg.initialize(values, DEPLOY.parents[1], tmp_path, cfg.Dialog(False))


def test_incorrect_internal_address_must_be_explicitly_corrected(tmp_path, monkeypatch):
    values = settings(tmp_path, monkeypatch)
    values["NEXUS_GATEWAY_OPENAI_BASE"] = "https://public.invalid/v1"
    with pytest.raises(cfg.DeploymentError, match="交互"):
        cfg.initialize(values, DEPLOY.parents[1], tmp_path, cfg.Dialog(False))
    dialog = cfg.Dialog()
    dialog.ask = Mock(return_value="r")
    result = cfg.initialize(values, DEPLOY.parents[1], tmp_path, dialog)
    assert result["NEXUS_GATEWAY_OPENAI_BASE"] == "http://127.0.0.1:18110/v1"


def test_source_profiles_share_versions_but_keep_independent_urls():
    public = deploy.source_manifest(DEPLOY / "sources/public.json")
    company = deploy.source_manifest(DEPLOY / "sources/huawei.json")
    assert public["node_version"] == company["node_version"] == "22.23.3"
    assert public["python_version"] == company["python_version"] == "3.11.17"
    assert public["python_url"] != company["python_url"]
    for filename in ("deploy.py", "configuration.py", "lifecycle.py", "install_nexus.sh"):
        code = (DEPLOY / filename).read_text()
        assert "mirrors.tools.huawei.com" not in code
        assert "repo.huaweicloud.com" not in code
        assert "systemctl" not in code
        assert "systemd" not in code


@pytest.fixture
def artifact_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/binary")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"verified-package")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_actual_download_verifies_digest_and_does_not_follow_redirects(artifact_server, tmp_path):
    target = tmp_path / "package"
    deploy.fetch(artifact_server + "/binary", target, digest=hashlib.sha256(b"verified-package").hexdigest())
    assert target.read_bytes() == b"verified-package"
    with pytest.raises(cfg.DeploymentError, match="SHA256"):
        deploy.fetch(artifact_server + "/binary", target, digest="0" * 64)
    with pytest.raises(cfg.DeploymentError, match="重定向"):
        deploy.fetch(artifact_server + "/redirect", target)


def test_untrusted_archive_cannot_escape_installation_directory(tmp_path):
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as stream:
        item = tarfile.TarInfo("../escape")
        item.size = 1
        stream.addfile(item, io.BytesIO(b"x"))
    with pytest.raises(cfg.DeploymentError, match="不安全"):
        deploy.unpack(archive, tmp_path / "install")
    assert not (tmp_path / "escape").exists()


def test_platform_process_lifecycle_actually_stops_old_process_and_rejects_pid_reuse(tmp_path, monkeypatch):
    values = settings(tmp_path, monkeypatch)
    values.update(NEXUS_DEPLOY_RELEASE="first", NEXUS_DEPLOY_NGINX_BINARY="nginx")
    runtime_env = dict(os.environ, **values)
    manager = lifecycle.Manager(DEPLOY.parents[1], tmp_path, runtime_env)
    command = [sys.executable, "-c", "import time;time.sleep(60)"]
    manager.specs = [{"name": "isolated", "command": command, "cwd": str(tmp_path)}]
    monkeypatch.setattr(manager, "health", lambda: None)
    manager.start()
    record = dict(manager.records["isolated"])
    try:
        manager.check_identity(manager.specs[0])
        manager.records["isolated"]["create_time"] = "1.000"
        with pytest.raises(cfg.DeploymentError, match="身份不符"):
            manager.stop()
        assert manager.procman.leader_live(record["pid"])
        manager.records["isolated"] = record
        manager.stop()
        assert not manager.procman.leader_live(record["pid"])
        manager.env["NEXUS_DEPLOY_RELEASE"] = "second"
        manager.start()
        assert manager.records["isolated"]["pid"] != record["pid"]
        assert manager.records["isolated"]["release"] == "second"
    finally:
        manager.records["isolated"]["create_time"] = (
            lifecycle.procman_for(DEPLOY.parents[1]).read_process_identity(
                manager.records["isolated"]["pid"]
            )["create_time"]
            if manager.records
            else record["create_time"]
        )
        manager.stop()


def test_unknown_port_is_not_treated_as_an_existing_install(tmp_path, monkeypatch):
    values = settings(tmp_path, monkeypatch)
    values.update(NEXUS_DEPLOY_RELEASE="test", NEXUS_DEPLOY_NGINX_BINARY="nginx")
    manager = lifecycle.Manager(DEPLOY.parents[1], tmp_path, dict(os.environ, **values))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        manager.specs = [{"name": "unowned", "port": sock.getsockname()[1]}]
        with pytest.raises(cfg.DeploymentError, match="非受管进程"):
            manager.preflight()


def test_legacy_python_process_record_is_verified_and_migrated_before_stop(tmp_path, monkeypatch):
    values = settings(tmp_path, monkeypatch)
    values.update(NEXUS_DEPLOY_RELEASE="new", NEXUS_DEPLOY_NGINX_BINARY="nginx")
    procman = lifecycle.procman_for(DEPLOY.parents[1])
    record = procman.spawn_process(
        [sys.executable, "-c", "import time;time.sleep(60)"], tmp_path, tmp_path / "legacy.log"
    )
    cfg.atomic_write(
        tmp_path / "service-ctl.json", __import__("json").dumps({"processes": {"agent": record}})
    )
    manager = lifecycle.Manager(DEPLOY.parents[1], tmp_path, dict(os.environ, **values))
    try:
        assert manager.records["agent-service"]["pid"] == record["pid"]
        assert (tmp_path / "service-ctl.json").is_file()
        manager.stop()
        assert not procman.leader_live(record["pid"])
    finally:
        if procman.leader_live(record["pid"]):
            procman.release_record(record, label="test-legacy")


def test_real_git_upgrade_is_fast_forward_only_and_uses_an_immutable_release(tmp_path, monkeypatch):
    import subprocess

    remote, checkout = tmp_path / "origin.git", tmp_path / "checkout"

    def git(*args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "--bare", str(remote))
    git("init", "--initial-branch=v1.4.9", str(checkout))
    git("config", "user.email", "deploy-test@example.invalid", cwd=checkout)
    git("config", "user.name", "Deployment Test", cwd=checkout)
    (checkout / "release.txt").write_text("first")
    git("add", ".", cwd=checkout)
    git("commit", "-m", "first", cwd=checkout)
    git("remote", "add", "origin", str(remote), cwd=checkout)
    git("push", "origin", "v1.4.9", cwd=checkout)
    args = SimpleNamespace(
        root=checkout, state=tmp_path / "state", action="deploy", env_file=None, sources=None
    )
    controller = deploy.Deployer(args, cfg.Dialog(False))
    controller.checkout()
    first_release = controller.release_root
    assert (first_release / "release.txt").read_text() == "first"
    author = tmp_path / "author"
    git("clone", "-b", "v1.4.9", str(remote), str(author))
    git("config", "user.email", "deploy-test@example.invalid", cwd=author)
    git("config", "user.name", "Deployment Test", cwd=author)
    (author / "release.txt").write_text("second")
    git("commit", "-am", "second", cwd=author)
    git("push", "origin", "v1.4.9", cwd=author)

    def restart(_binary, command):
        assert command[1] == str(checkout / "ops/deploy/deploy.py")
        raise SystemExit("execute new deployment code")

    monkeypatch.setattr(deploy.os, "execv", restart)
    with pytest.raises(SystemExit, match="new deployment code"):
        controller.checkout()
    controller.checkout()
    assert (controller.release_root / "release.txt").read_text() == "second"
    assert (first_release / "release.txt").read_text() == "first"
    (checkout / "local.txt").write_text("unpublished")
    git("add", ".", cwd=checkout)
    git("commit", "-m", "local only", cwd=checkout)
    with pytest.raises(cfg.DeploymentError, match="领先"):
        controller.checkout()


def test_deployment_command_timeout_stops_its_own_children(tmp_path):
    import psutil

    args = SimpleNamespace(
        root=DEPLOY.parents[1], state=tmp_path, action="deploy", env_file=None, sources=None
    )
    controller = deploy.Deployer(args, cfg.Dialog(False))
    pidfile = tmp_path / "child.pid"
    program = (
        "import subprocess,sys,time;from pathlib import Path;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "Path(sys.argv[1]).write_text(str(child.pid));time.sleep(60)"
    )
    with pytest.raises(cfg.DeploymentError, match="进程组"):
        controller.run([sys.executable, "-c", program, str(pidfile)], timeout=0.3)
    pid = int(pidfile.read_text())
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


def test_source_repair_preserves_explicit_worker_source_configuration(tmp_path):
    args = SimpleNamespace(
        root=DEPLOY.parents[1], state=tmp_path, action="deploy", env_file=None, sources=None
    )
    controller = deploy.Deployer(args, cfg.Dialog(False))
    controller.sources = deploy.source_manifest(DEPLOY / "sources/public.json")
    previous = controller.sources["pip_index"]
    controller.values["NEXUS_REMOTE_PIP_INDEX_URL"] = previous
    controller.dialog.ask = Mock(side_effect=["m", "https://pip.company.test/simple"])
    controller.repair_source("pip_index", cfg.DeploymentError("unreachable"))
    assert controller.sources["pip_index"] == "https://pip.company.test/simple"
    assert controller.values["NEXUS_REMOTE_PIP_INDEX_URL"] == previous


def test_container_resources_use_nested_cgroup_limits_instead_of_host_totals(tmp_path, monkeypatch):
    proc, cgroup = tmp_path / "proc", tmp_path / "cgroup"
    (proc / "self").mkdir(parents=True)
    (cgroup / "container").mkdir(parents=True)
    (proc / "meminfo").write_text("MemTotal:       33554432 kB\n")
    (proc / "self/cgroup").write_text("0::/container\n")
    (cgroup / "memory.max").write_text("8589934592")
    (cgroup / "container/memory.max").write_text("2147483648")
    (cgroup / "container/cpu.max").write_text("200000 100000")
    monkeypatch.setattr(deploy.os, "cpu_count", lambda: 32)
    if hasattr(deploy.os, "sched_getaffinity"):
        monkeypatch.setattr(deploy.os, "sched_getaffinity", lambda _pid: set(range(32)))
    assert deploy.linux_resources(proc, cgroup) == {"memory_mb": 2048, "cpus": 2}
