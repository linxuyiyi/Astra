"""隔离组件端到端；显式提供 Nginx 二进制才执行，不宣称 Linux 安装器验收。"""

import base64
import hashlib
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


def test_real_nginx_tls_websocket_sse_and_worker_access_boundaries(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            if self.headers.get("Upgrade", "").lower() == "websocket":
                accept = base64.b64encode(
                    hashlib.sha1(
                        (self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                    ).digest()
                ).decode()
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                return
            if self.path == "/api/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b"data: first\n\n")
                self.wfile.flush()
                time.sleep(1)
                self.wfile.write(b"data: second\n\n")
                self.wfile.flush()
                self.close_connection = True
                return
            status = (
                401
                if self.path.startswith("/v1/") and self.headers.get("Authorization") != "Bearer test-secret"
                else 200
            )
            self.send_response(status)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    cert, key = certificate(tmp_path)
    https, http = port(), port()
    env = {
        "NEXUS_API_PORT": str(upstream.server_port),
        "ASTRA_SERVICE_GATEWAY_PORT": str(upstream.server_port),
        "NEXUS_MCP_PORT": str(upstream.server_port),
        "NEXUS_DEPLOY_HTTPS_PORT": str(https),
        "NEXUS_DEPLOY_WORKER_PORT": str(http),
        "NEXUS_DEPLOY_PUBLIC_IP": "127.0.0.1",
        "NEXUS_DEPLOY_WORKER_ALLOW": "127.0.0.1/32",
        "NEXUS_DEPLOY_TLS_CERT": str(cert),
        "NEXUS_DEPLOY_TLS_KEY": str(key),
    }
    prefix = tmp_path / "nginx"
    prefix.mkdir()
    (prefix / "logs").mkdir()
    (prefix / "mime.types").write_text("types { text/html html; }\n")
    config = tmp_path / "nginx.conf"
    config.write_text(deploy.nginx_config(DEPLOY.parents[1], tmp_path, env))
    args = [NGINX, "-p", str(prefix) + "/", "-c", str(config)]
    (tmp_path / "logs").mkdir()
    (tmp_path / "nginx-cache").mkdir()
    subprocess.run([*args, "-t"], check=True, capture_output=True)
    nginx = subprocess.Popen([*args, "-g", "daemon off;"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(cert))),
    )
    try:
        for _ in range(40):
            try:
                with opener.open(f"https://127.0.0.1:{https}/", timeout=1) as response:
                    assert response.status == 200
                break
            except OSError:
                assert nginx.poll() is None, nginx.stderr.read().decode()
                time.sleep(0.1)
        else:
            raise AssertionError("Nginx 未启动")
        for path, expected in (("/api/agent-workers", 404), ("/v1/models", 401)):
            with pytest.raises(urllib.error.HTTPError) as result:
                opener.open(f"http://127.0.0.1:{http}" + path, timeout=3)
            assert result.value.code == expected
        with opener.open(
            urllib.request.Request(
                f"http://127.0.0.1:{http}/v1/models", headers={"Authorization": "Bearer test-secret"}
            ),
            timeout=3,
        ) as response:
            assert response.status == 200
        started = time.monotonic()
        with opener.open(f"https://127.0.0.1:{https}/api/events", timeout=3) as response:
            assert response.readline() == b"data: first\n"
            assert time.monotonic() - started < 0.8
            assert b"data: second" in response.read()
        with socket.create_connection(("127.0.0.1", http), timeout=3) as connection:
            connection.sendall(
                b"GET /api/worker-gateway/connect HTTP/1.1\r\nHost: localhost\r\n"
                b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                b"Sec-WebSocket-Version: 13\r\n\r\n"
            )
            assert b"101 Switching Protocols" in connection.recv(8192)
        # 改为不允许本测试客户端的 CIDR，并移除仅供本机验收的显式自地址 allow。
        text = (
            config.read_text()
            .replace("allow 127.0.0.1/32;", "allow 192.0.2.0/24;")
            .replace("allow 127.0.0.1;", "")
        )
        config.write_text(text)
        subprocess.run([*args, "-t"], check=True, capture_output=True)
        subprocess.run([*args, "-s", "reload"], check=True, capture_output=True)
        time.sleep(0.3)
        with pytest.raises(urllib.error.HTTPError) as denied:
            opener.open(f"http://127.0.0.1:{http}/v1/models", timeout=3)
        assert denied.value.code == 403
    finally:
        nginx.terminate()
        nginx.communicate(timeout=10)
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)


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
