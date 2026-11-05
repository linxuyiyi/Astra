"""隔离组件端到端；显式提供 Nginx 二进制才执行，不宣称 Linux 安装器验收。"""

import os
import shutil
import socket
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.platform.deployment_platform import DEPLOY, deploy, lifecycle
from tests.platform.deployment_platform import configuration as cfg

NGINX = os.environ.get("NEXUS_TEST_NGINX") or shutil.which("nginx")
pytestmark = pytest.mark.skipif(not NGINX, reason="需要显式可执行的 Nginx，静态检查不算端到端")


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def certificate(tmp_path):
    cert, key = tmp_path / "server.crt", tmp_path / "server.key"
    openssl = shutil.which("openssl")
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def test_isolated_real_core_services_migrations_restart_and_health(tmp_path, monkeypatch):
    """已有依赖、独立 SQLite/Redis/端口，不替代 Linux 自动安装验收。"""
    import psutil

    root = DEPLOY.parents[1]
    apipy = root / "services/nexus-api/.venv/bin/python"
    agentpy = root / "services/agent-service/.venv-agentscope/bin/python"
    if not agentpy.is_file() or not shutil.which("redis-server"):
        pytest.skip("缺少已有 AgentScope 依赖或 Redis 二进制")

    def inventory(kind):
        found = []
        for process in [psutil.Process(), *psutil.Process().children(recursive=True)]:
            if not process.is_running():
                continue
            for connection in process.net_connections(kind=kind):
                found.append(
                    SimpleNamespace(status=connection.status, laddr=connection.laddr, pid=process.pid)
                )
        return found

    monkeypatch.setattr(psutil, "net_connections", inventory)
    monkeypatch.setattr(cfg, "usable_address", lambda value: value == "127.0.0.1")
    redis_port = port()
    redis_log = (tmp_path / "redis.log").open("wb")
    redis = subprocess.Popen(
        [
            shutil.which("redis-server"),
            "--bind",
            "127.0.0.1",
            "--port",
            str(redis_port),
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            str(tmp_path),
        ],
        stdout=redis_log,
        stderr=subprocess.STDOUT,
    )
    values = {
        "ASTRA_DATABASE_URL": "sqlite+aiosqlite:///" + str(tmp_path / "nexus.db"),
        "AGENTSCOPE_SQL_URL": "sqlite+aiosqlite:///" + str(tmp_path / "agent.db"),
        "ASTRA_REDIS_URL": f"redis://127.0.0.1:{redis_port}/1",
        "NEXUS_DEPLOY_PUBLIC_IP": "127.0.0.1",
        "NEXUS_DEPLOY_WORKER_ALLOW": "127.0.0.1/32",
    }
    for field in (
        "NEXUS_API_PORT",
        "AGENTSCOPE_PORT",
        "ASTRA_SERVICE_GATEWAY_PORT",
        "NEXUS_MCP_PORT",
        "NEXUS_TASK_MCP_PORT",
        "NEXUS_SCHEDULE_MCP_PORT",
        "NEXUS_DEPLOY_HTTPS_PORT",
        "NEXUS_DEPLOY_WORKER_PORT",
    ):
        values[field] = str(port())
    values = cfg.initialize(values, root, tmp_path, cfg.Dialog(False))
    values.update(NEXUS_DEPLOY_RELEASE="isolated-first", NEXUS_DEPLOY_NGINX_BINARY=NGINX)
    cert, key = certificate(tmp_path)
    values.update(
        NEXUS_DEPLOY_TLS_CERT=str(cert),
        NEXUS_DEPLOY_TLS_KEY=str(key),
        NEXUS_DEPLOY_NGINX_CACHE=str(tmp_path / "nginx-cache"),
    )
    (tmp_path / "nginx-cache").mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "LANG": "en_US.UTF-8", **values}
    cfg.save_env(tmp_path / "nexus.env", values)
    for dirname in ("logs", "runtime", "nginx", "data"):
        (tmp_path / dirname).mkdir(exist_ok=True)
    (tmp_path / "nginx/logs").mkdir(exist_ok=True)
    (tmp_path / "nginx/mime.types").write_text("types { text/html html; }\n")
    (tmp_path / "nginx.conf").write_text(deploy.nginx_config(root, tmp_path, values))
    manager = lifecycle.Manager(root, tmp_path, env)
    commands = [
        ([str(apipy), "-m", "alembic", "upgrade", "head"], root / "services/nexus-api"),
        ([str(agentpy), "agentscope_sql.py", "init"], root / "services/agent-service"),
        ([str(apipy), "-m", "app.deployment", "gateway-key"], root / "services/nexus-api"),
    ]
    try:
        for command, directory in commands:
            result = subprocess.run(
                command, cwd=directory, env=env, capture_output=True, text=True, timeout=90, check=False
            )
            assert result.returncode == 0, result.stdout + result.stderr
        manager.start()
        first = {name: info["pid"] for name, info in manager.records.items()}
        assert len(first) == 11
        controller = deploy.Deployer(
            SimpleNamespace(root=root, state=tmp_path, action="deploy"), cfg.Dialog(False)
        )
        controller.values = dict(values)
        controller.env = dict(env)
        controller.validate_connections()
        # 验证已有无效连接不会因配置存在而被复用，明确输入修正值后重新连接。
        controller.values["ASTRA_REDIS_URL"] = f"redis://127.0.0.1:{port()}/1"
        controller.env.update(controller.values)
        controller.dialog.ask = Mock(side_effect=["m", "ASTRA_REDIS_URL", values["ASTRA_REDIS_URL"]])
        controller.validate_connections()
        assert controller.values["ASTRA_REDIS_URL"] == values["ASTRA_REDIS_URL"]
        controller.external_health()
        manager.health()
        manager.stop()
        assert all(not manager.procman.leader_live(pid) for pid in first.values())
        manager.env["NEXUS_DEPLOY_RELEASE"] = "isolated-second"
        (tmp_path / "nginx.conf").write_text(deploy.nginx_config(root, tmp_path, manager.env))
        manager.start()
        assert all(info["pid"] != first[name] for name, info in manager.records.items())
        assert all(info["release"] == "isolated-second" for info in manager.records.values())
        manager.health()
    finally:
        manager.stop()
        redis.terminate()
        redis.communicate(timeout=10)
        redis_log.close()
