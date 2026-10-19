import asyncio
import socket
import sys
from unittest.mock import AsyncMock

import pytest

from app.config.settings import Settings
from app.executors import opencode_local_server as local
from app.executors.opencode_server import OpenCodeServerUnavailable
from app.models import OpenCodeExecutionEnvironment, Task
from app.runtime.environment import ExecutionEnvironmentRuntime, checks_status


@pytest.fixture
def serve(tmp_path, monkeypatch):
    monkeypatch.setattr(local.tempfile, "gettempdir", lambda: str(tmp_path))
    binary = tmp_path / "opencode-fixture"
    binary.write_text(
        f"#!{sys.executable}\n"
        + """
import http.server, json, os, sys
if "--version" in sys.argv:
    print("1.18.35")
    sys.exit(0)
port = int(sys.argv[sys.argv.index("--port") + 1])
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        data = {"directory": os.getcwd()} if self.path == "/path" else {
            "healthy": not os.path.exists("unhealthy"), "version": "1.18.35"}
        self.wfile.write(json.dumps(data).encode())
    def log_message(self, *args): pass
http.server.HTTPServer.allow_reuse_address = True
http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
"""
    )
    binary.chmod(0o755)
    ports = []

    def create(name):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        ports.append(port)
        root = tmp_path / name
        root.mkdir()
        # The workspace, not Astra, owns OpenCode config.
        (root / "opencode.json").write_text("{}", encoding="utf-8")
        env = OpenCodeExecutionEnvironment(
            employee_id=name,
            mode="LOCAL",
            name=name,
            workspace_root=str(root),
            opencode_path=str(binary),
            opencode_server_port=port,
            model_name="test/model",
        )
        return local.LocalOpenCodeServerExecutor(
            env,
            Settings(seed_demo=False, opencode_server_start_timeout_seconds=3),
        )

    yield create
    for port in ports:
        process = local._processes.pop(port, None)
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


async def test_two_employees_start_and_reuse_independent_serve(serve):
    first, second = serve("员工 A"), serve("员工 B")
    await asyncio.gather(first.ensure_ready(), second.ensure_ready())
    assert first.client.base_url != second.client.base_url
    for executor in (first, second):
        assert executor.client.base_url != executor.settings.resolved_opencode_server_url
        pid = local._processes[executor.environment.opencode_server_port].pid
        await asyncio.gather(executor.ensure_ready(), executor.ensure_ready())
        assert local._processes[executor.environment.opencode_server_port].pid == pid
        assert (await executor.client.request("GET", "/path"))["directory"] == str(executor.workspace)


async def test_simultaneous_cold_start_spawns_once(serve):
    executor = serve("concurrent")
    await asyncio.gather(executor.ensure_ready(), executor.ensure_ready())
    assert (await executor.client.health())["healthy"]


async def test_foreign_port_fails_with_employee_context(serve):
    executor = serve("conflict")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", executor.environment.opencode_server_port))
        executor.client.health = AsyncMock(side_effect=OpenCodeServerUnavailable("unavailable"))
        with pytest.raises(RuntimeError, match="端口冲突") as error:
            await executor.ensure_ready()
    assert "Workspace=" in str(error.value)
    assert "员工 conflict" in str(error.value)
    assert executor.environment.opencode_server_port not in local._processes


async def test_healthy_serve_cannot_be_shared_by_employees(serve):
    first, second = serve("owner"), serve("other")
    await first.ensure_ready()
    second.environment.opencode_server_port = first.environment.opencode_server_port
    second.client.base_url = first.client.base_url
    with pytest.raises(RuntimeError, match="另一员工"):
        await second.ensure_ready()


async def test_unstarted_serve_environment_is_available(serve):
    executor = serve("check")
    checks = await ExecutionEnvironmentRuntime(executor.settings).test(executor.environment)
    assert checks_status(checks) == "AVAILABLE"
    assert "未启动" in checks[-1].detail
    assert executor.environment.opencode_server_port not in local._processes


async def test_reserved_assistant_and_global_ports_rejected(serve):
    executor = serve("reserved")
    for port in (executor.settings.opencode_server_port, executor.settings.assistant_opencode_port):
        executor.environment.opencode_server_port = port
        with pytest.raises(RuntimeError, match="公共/助手"):
            await executor.ensure_ready()


async def test_start_failure_does_not_execute_session(serve):
    executor = serve("bad-binary")
    executor.environment.opencode_path = "/missing/opencode"
    executor.client.create_session = AsyncMock()
    with pytest.raises(RuntimeError, match="未找到 Agent"):
        await executor.start(Task(id="task"), executor.workspace)
    executor.client.create_session.assert_not_awaited()




async def test_unhealthy_owned_process_is_restarted(serve):
    executor = serve("restart")
    await executor.ensure_ready()
    previous = local._processes[executor.environment.opencode_server_port]
    health = executor.client.health
    calls = 0

    async def interrupted_health():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OpenCodeServerUnavailable("unhealthy")
        return await health()

    executor.client.health = interrupted_health
    await executor.ensure_ready()
    assert previous.poll() is not None
    assert local._processes[executor.environment.opencode_server_port].pid != previous.pid


async def test_unready_server_times_out_without_session(serve):
    executor = serve("timeout")
    executor.settings.opencode_server_start_timeout_seconds = 1
    executor.client.health = AsyncMock(side_effect=OpenCodeServerUnavailable("still unhealthy"))
    executor.client.create_session = AsyncMock()
    with pytest.raises(RuntimeError, match="启动超时"):
        await executor.start(Task(id="timeout"), executor.workspace)
    executor.client.create_session.assert_not_awaited()




async def test_missing_workspace_config_refuses_to_start_serve(serve, monkeypatch):
    executor = serve("no-config")
    (executor.workspace / "opencode.json").unlink()
    monkeypatch.setattr(local, "discover_workspace_config", lambda workspace: None)
    with pytest.raises(RuntimeError, match="未提供 OpenCode 配置"):
        await executor.ensure_ready()
    assert executor.environment.opencode_server_port not in local._processes


async def test_missing_workspace_fails_environment_check(serve):
    executor = serve("missing-workspace")
    (executor.workspace / "opencode.json").unlink()
    executor.workspace.rmdir()
    checks = await ExecutionEnvironmentRuntime(executor.settings).test(executor.environment)
    assert checks_status(checks) == "UNAVAILABLE"
    assert not executor.workspace.exists()


async def test_deployment_config_is_passed_to_local_serve(serve, monkeypatch, tmp_path):
    executor = serve("deployment-config")
    (executor.workspace / "opencode.json").unlink()
    deployment = tmp_path / "deploy-opencode.json"
    deployment.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("NEXUS_OPENCODE_CONFIG", str(deployment))
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    captured = {}
    real_popen = local.subprocess.Popen

    def capture(*args, **kwargs):
        captured.update(kwargs.get("env") or {})
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(local.subprocess, "Popen", capture)
    await executor.ensure_ready()
    assert captured["OPENCODE_CONFIG"] == str(deployment)


async def test_workspace_config_is_not_shadowed_by_deployment_env(serve, monkeypatch, tmp_path):
    executor = serve("workspace-config")
    deployment = tmp_path / "deploy-opencode.json"
    deployment.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("NEXUS_OPENCODE_CONFIG", str(deployment))
    monkeypatch.setenv("OPENCODE_CONFIG", str(deployment))
    captured = {}
    real_popen = local.subprocess.Popen

    def capture(*args, **kwargs):
        captured.update(kwargs.get("env") or {})
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(local.subprocess, "Popen", capture)
    await executor.ensure_ready()
    assert "OPENCODE_CONFIG" not in captured
