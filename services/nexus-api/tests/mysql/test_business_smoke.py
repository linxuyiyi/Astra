"""真实 MySQL + Nexus/AgentScope 进程冒烟，不调用模型推理。

单独运行：ASTRA_TEST_MYSQL_URL、AGENTSCOPE_TEST_MYSQL_URL、AGENTSCOPE_TEST_PYTHON。
数据库名必须包含 test；两个数据库和 Redis DB 14/15 专用于测试。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[4]
SERVICE = ROOT / "services/nexus-api"
AGENT = ROOT / "services/agent-service"
REQUIRED = ("ASTRA_TEST_MYSQL_URL", "AGENTSCOPE_TEST_MYSQL_URL", "AGENTSCOPE_TEST_PYTHON")
pytestmark = pytest.mark.skipif(
    not all(os.environ.get(key) for key in REQUIRED), reason="需要独立 MySQL 测试环境"
)


def unused_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


def stop(process) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def wait_ready(process, url: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError("服务在就绪前退出；检查测试临时目录内日志")
        try:
            if httpx.get(url, timeout=1, headers={"X-User-ID": "nexus"}).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError("服务就绪超时；检查测试临时目录内日志")


@pytest.fixture
def embedding_upstream():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, payload))
            response = json.dumps(
                {
                    "object": "list",
                    "model": payload["model"],
                    "data": [{"object": "embedding", "index": 0, "embedding": [0.1, -0.3]}],
                    "usage": {"prompt_tokens": 3, "total_tokens": 3},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_mysql_api_restart_and_im_claim(tmp_path, embedding_upstream):
    url = make_url(os.environ["ASTRA_TEST_MYSQL_URL"])
    agent_url = make_url(os.environ["AGENTSCOPE_TEST_MYSQL_URL"])
    assert url.get_backend_name() == agent_url.get_backend_name() == "mysql"
    assert "test" in url.database and "test" in agent_url.database
    assert url.database != agent_url.database
    engine = create_engine(url.set(drivername="mysql+pymysql"), pool_pre_ping=True)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT VERSION()")).scalar().startswith("8.0.36")
        assert "STRICT_TRANS_TABLES" in connection.execute(text("SELECT @@sql_mode")).scalar()

    api_port, agent_port, gateway_port = unused_port(), unused_port(), unused_port()
    gateway = f"http://127.0.0.1:{gateway_port}"
    base = f"http://127.0.0.1:{api_port}"
    scope = f"http://127.0.0.1:{agent_port}"
    configs = tmp_path / "configs"
    configs.mkdir()
    env = {
        **os.environ,
        "ASTRA_DATABASE_URL": os.environ["ASTRA_TEST_MYSQL_URL"],
        "AGENTSCOPE_SQL_URL": os.environ["AGENTSCOPE_TEST_MYSQL_URL"],
        "ASTRA_SEED_DEMO": "false",
        "ASTRA_CONFIG_DIR": str(configs),
        "ASTRA_DATA_DIR": str(tmp_path / "data"),
        "NEXUS_DATA_DIR": str(tmp_path / "data"),
        "ASTRA_WORKSPACE_ROOT": str(tmp_path / "workers"),
        "ASTRA_REDIS_URL": "redis://127.0.0.1:6379/14",
        "ASTRA_LLM_WIKI_ENABLED": "false",
        "ASTRA_GATEWAY_MASTER_KEY": Fernet.generate_key().decode(),
        "NEXUS_GATEWAY_API_KEY": "agw_" + uuid4().hex,
        "NEXUS_GATEWAY_OPENAI_BASE": gateway + "/v1",
        "ASTRA_GATEWAY_CONFIG_POLL_INTERVAL": "1",
        "NEXUS_TASK_PLATFORM_URL": base,
        "NEXUS_AGENTSCOPE_BASE_URL": scope,
        "NEXUS_API_BASE": base,
        "AGENTSCOPE_HOST": "127.0.0.1",
        "AGENTSCOPE_PORT": str(agent_port),
        "AGENTSCOPE_REDIS_DB": "15",
        "AGENTSCOPE_WORKSPACE_DIR": str(tmp_path / "agent-workspaces"),
        "AGENTSCOPE_QDRANT_PATH": str(tmp_path / "qdrant"),
        "AGENTSCOPE_BLOB_DIR": str(tmp_path / "blobs"),
        "NEXUS_WORKER_GATEWAY_ADMIN_KEY": "mysql-smoke-admin",
        "PYTHONUNBUFFERED": "1",
    }
    for args, cwd in [
        ([sys.executable, "-m", "alembic", "upgrade", "head"], SERVICE),
        ([os.environ["AGENTSCOPE_TEST_PYTHON"], "agentscope_sql.py", "init"], AGENT),
        ([sys.executable, "-m", "app.deployment", "gateway-key"], SERVICE),
    ]:
        outcome = subprocess.run(args, cwd=cwd, env=env, capture_output=True, timeout=60)
        assert outcome.returncode == 0, "数据库初始化失败；请单独执行初始化检查"

    from sqlalchemy import inspect

    from app.sql_storage import metadata

    with engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        assert set(metadata().tables) == tables - {"alembic_version"}
        for name in tables:
            for foreign_key in inspector.get_foreign_keys(name):
                assert foreign_key["referred_table"] in tables
        non_innodb = text(
            "SELECT COUNT(*) FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND ENGINE!='InnoDB'"
        )
        assert connection.execute(non_innodb).scalar() == 0

    processes = []
    logs = []

    def start_all():
        for name, command, cwd, health in [
            (
                "gateway",
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.gateway.server:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(gateway_port),
                ],
                SERVICE,
                gateway + "/healthz",
            ),
            ("agent", [os.environ["AGENTSCOPE_TEST_PYTHON"], "agent_service.py"], AGENT, scope + "/health"),
            (
                "nexus",
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "nexus_api.service:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(api_port),
                ],
                SERVICE,
                base + "/api/health",
            ),
        ]:
            log = (tmp_path / f"{name}.log").open("ab")
            logs.append(log)
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=log)
            processes.append(process)
            wait_ready(process, health)

    try:
        start_all()
        suffix = uuid4().hex[:10]
        with httpx.Client(base_url=base, timeout=30, headers={"X-Verified-UID": "mysql-test"}) as client:
            route = client.post(
                "/api/gateway/routes", json={"model": f"mysql-{suffix}", "pool_id": "gp_default"}
            )
            assert route.status_code == 201, route.text
            # 真实 SQL 控制面、Redis 探测状态和独立网关进程，只有上游返回固定测试向量。
            vendor_model = "vendor-embed-" + suffix
            embedding_alias = "embed-" + suffix
            node = client.post(
                "/api/gateway/nodes",
                json={
                    "name": embedding_alias,
                    "pool_id": "gp_default",
                    "base_url": embedding_upstream[0],
                    "auth_type": "NONE",
                    "models": [vendor_model],
                    "probe_strategy": "EMBEDDING",
                },
            )
            assert node.status_code == 201, node.text
            for _ in range(2):
                probe = client.post(f"/api/gateway/nodes/{node.json()['id']}/probe", json={})
                assert probe.status_code == 200, probe.text
            enabled = client.patch(
                f"/api/gateway/nodes/{node.json()['id']}",
                json={"config_state": "ENABLED", "scheduling_state": "ACTIVE"},
            )
            assert enabled.status_code == 200, enabled.text
            embedding_route = client.post(
                "/api/gateway/routes",
                json={
                    "model": embedding_alias,
                    "target_model": vendor_model,
                    "pool_id": "gp_default",
                },
            )
            assert embedding_route.status_code == 201, embedding_route.text
            deadline = time.monotonic() + 15
            while True:
                result = httpx.post(
                    gateway + "/v1/embeddings",
                    headers={"Authorization": "Bearer " + env["NEXUS_GATEWAY_API_KEY"]},
                    json={"model": embedding_alias, "input": "真实 HTTP 代理验证", "dimensions": 2},
                    timeout=5,
                )
                if result.status_code == 200 or time.monotonic() >= deadline:
                    break
                time.sleep(0.3)
            assert result.status_code == 200, result.text
            assert result.json()["model"] == embedding_alias
            assert result.json()["data"][0]["embedding"] == [0.1, -0.3]
            assert embedding_upstream[1][-1][0] == "/v1/embeddings"
            assert embedding_upstream[1][-1][1]["model"] == vendor_model
            employee_response = client.post(
                "/api/v1/employees",
                json={
                    "name": f"数据库验证{suffix}",
                    "domain": f"MySQL验证{suffix}",
                    "description": "长文本验证" * 18000,
                    "instructions": "测试数据库持久化",
                    "model_route_id": route.json()["id"],
                },
            )
            assert employee_response.status_code == 201, employee_response.text[:500]
            payload = employee_response.json()
            assert payload["agentscope"]["ok"], payload["agentscope"]
            employee = payload["employee"]
            assert employee["agentscope_agent_id"] and employee["mailbox_session_id"]
            private = client.post(
                f"/api/v1/employees/{employee['id']}/conversations", json={"name": "数据库私聊"}
            )
            assert private.status_code == 201, private.text
            again = client.post(
                f"/api/v1/employees/{employee['id']}/conversations", json={"name": "重复入口"}
            )
            assert again.json()["id"] == private.json()["id"]
            channel = client.post(
                "/api/v1/channels",
                json={
                    "name": f"数据库团队{suffix}",
                    "description": "SQL团队",
                    "member_ids": [employee["id"]],
                },
            )
            assert channel.status_code == 201, channel.text
            task = client.post(
                "/api/tasks",
                json={
                    "task_type": "ISSUE",
                    "category_id": "testing-platform",
                    "title": f"SQL任务{suffix}",
                    "description": "验证任务存储",
                    "acceptance_criteria": [{"content": "重启保留"}],
                    "priority": "HIGH",
                    "completion_policy": "AUTO",
                },
            )
            assert task.status_code == 201, task.text
            task_id = task.json()["id"]
            assert client.post(f"/api/tasks/{task_id}/stop").json()["status"] == "CANCELED"
        for process in reversed(processes):
            stop(process)
        processes.clear()
        start_all()
        with httpx.Client(base_url=base, timeout=30, headers={"X-Verified-UID": "mysql-test"}) as client:
            saved = client.get("/api/v1/employees", params={"q": suffix})
            assert saved.status_code == 200, saved.text
            restored = next(item for item in saved.json()["items"] if item["id"] == employee["id"])
            assert restored["agentscope_agent_id"] == employee["agentscope_agent_id"]
            assert restored["mailbox_session_id"] == employee["mailbox_session_id"]
            assert len(restored["description"]) == len("长文本验证" * 18000)
            assert client.get(f"/api/tasks/{task_id}").json()["status"] == "CANCELED"
            # 常驻事件流数量超过默认同步连接池容量，仍能查询员工目录。
            streams = []
            try:
                for _ in range(16):
                    response = client.send(
                        client.build_request("GET", f"/api/v1/channels/{channel.json()['id']}/events"),
                        stream=True,
                    )
                    assert response.status_code == 200
                    streams.append(response)
                assert client.get("/api/v1/employees", timeout=3).status_code == 200
            finally:
                for response in streams:
                    response.close()
            channels = client.get("/api/v1/channels").json()["items"]
            assert any(item["id"] == channel.json()["id"] for item in channels)
        native = httpx.get(
            f"{scope}/sessions/",
            params={"agent_id": employee["agentscope_agent_id"]},
            headers={"X-User-ID": "nexus"},
            timeout=10,
        )
        assert native.status_code == 200, native.text
        assert any(
            item["session"]["id"] == employee["mailbox_session_id"] for item in native.json()["sessions"]
        )
        assert not list((tmp_path / "data").glob("*.db"))
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM t_nexus_employees WHERE id=:id"), {"id": employee["id"]}
                ).scalar()
                == 1
            )

        # 两个独立连接同时投递相同入站事件；只允许一个副作用。
        from app.sql_storage import connect
        from nexus_api.im import accept_inbound, bind_bot, open_external
        from nexus_api.main import reserve_peer_delivery

        with connect(os.environ["ASTRA_TEST_MYSQL_URL"]) as connection:
            reserve_peer_delivery(connection, suffix, "sender", "target", "检查 API A")
            reserve_peer_delivery(connection, suffix, "sender", "target", "检查 API a")
            with pytest.raises(Exception) as duplicate:
                reserve_peer_delivery(connection, suffix, "sender", "target", "检查 API A")
            assert duplicate.value.status_code == 409

        with connect(os.environ["ASTRA_TEST_MYSQL_URL"]) as connection:
            bind_bot(connection, employee["id"], "mysql-test", suffix)
            open_external(connection, employee["id"], suffix, f"im-{suffix}")
        barrier = threading.Barrier(2)
        calls = []

        def inbound():
            with connect(os.environ["ASTRA_TEST_MYSQL_URL"]) as connection:
                barrier.wait(timeout=5)

                async def accepted(*_args):
                    calls.append(1)
                    await asyncio.sleep(0.05)

                return asyncio.run(
                    accept_inbound(
                        connection,
                        provider="mysql-test",
                        event_id=f"evt-{suffix}",
                        bot_id=suffix,
                        sender_uid="mysql-test",
                        text="幂等验证",
                        external_key=suffix,
                        accept=accepted,
                    )
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(inbound) for _ in range(2)]
            results = []
            for future in futures:
                try:
                    results.append(future.result(timeout=15))
                except Exception as exc:
                    from nexus_api.im import ImError

                    assert isinstance(exc, ImError), type(exc).__name__
        assert len(calls) == 1
        assert any(result["accepted"] for result in results)
    finally:
        for process in reversed(processes):
            stop(process)
        for log in logs:
            log.close()
        engine.dispose()
