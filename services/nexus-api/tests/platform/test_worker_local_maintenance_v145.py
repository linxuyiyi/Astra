"""Worker 本地维护指令契约：原生 Session 校验、健康检查、绑定校验与控制闸门。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

import app.worker_agent as worker_agent
from app.worker_agent import supervisor as supervisor_mod
from app.worker_agent.connection import (
    AgentConfig,
    WorkerConnectionManager,
    handle_command,
    load_agent_config,
)
from app.worker_agent.process_env import child_environ
from app.worker_agent.supervisor import (
    RuntimeSupervisor,
    _check_local_health,
    assert_native_idle,
)
from app.worker_gateway.protocol import envelope
from tests.platform.test_worker_gateway_faults import _auth_reply


@contextmanager
def _server(routes: dict[str, tuple[int, object]]):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status, body = routes.get(
                urlsplit(self.path).path, (404, {"error": "not found"})
            )
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _restart_message(workspace: Path, port: int, request_id: str = "req-restart"):
    message = envelope(
        request_id=request_id,
        worker_instance_id="wrk_agent",
        environment_id=7,
        session_id=None,
        message_type="command",
        payload={
            "name": "restart_opencode",
            "data": {"workspace": str(workspace), "port": port},
        },
    )
    message.epoch = 3
    message.expires_at = int(time.time()) + 60
    return message


def _update_message(workspace: Path, port: int, request_id: str = "req-update"):
    message = envelope(
        request_id=request_id,
        worker_instance_id="wrk_agent",
        environment_id=7,
        session_id=None,
        message_type="command",
        payload={
            "name": "update_workspace",
            "data": {"workspace": str(workspace), "port": port, "branch": "main"},
        },
    )
    message.epoch = 3
    message.expires_at = int(time.time()) + 60
    return message


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace


@pytest.mark.parametrize("kind", ["busy", "retry"])
def test_native_busy_or_retry_blocks_restart(tmp_path: Path, kind: str):
    workspace = _workspace(tmp_path)
    with _server({"/session/status": (200, {"ses": {"type": kind}})}) as port:
        with pytest.raises(RuntimeError, match="正在执行"):
            assert_native_idle(str(workspace), port)


@pytest.mark.parametrize(
    "body",
    [
        {"ses": {"type": "unknown"}},
        {"ses": "idle"},
        [],
    ],
)
def test_unknown_or_malformed_native_state_is_conservatively_rejected(
    tmp_path: Path, body
):
    workspace = _workspace(tmp_path)
    with _server({"/session/status": (200, body)}) as port:
        with pytest.raises(RuntimeError, match="无法确认"):
            assert_native_idle(str(workspace), port)


def test_unreadable_native_status_blocks_restart(tmp_path: Path):
    workspace = _workspace(tmp_path)
    with _server({"/session/status": (500, {"error": "boom"})}) as port:
        with pytest.raises(RuntimeError, match="无法确认"):
            assert_native_idle(str(workspace), port)


@pytest.mark.parametrize("body", [{}, {"one": {"type": "idle"}}])
def test_native_idle_returns_true_when_listener_is_idle(tmp_path: Path, body):
    with _server({"/session/status": (200, body)}) as port:
        assert assert_native_idle(str(_workspace(tmp_path)), port) is True


def test_timeout_is_not_treated_as_stopped(tmp_path: Path, monkeypatch):
    workspace = _workspace(tmp_path)

    class TimedOut:
        def settimeout(self, _timeout):
            return None

        def connect(self, _address):
            raise TimeoutError("timed out")

        def close(self):
            return None

    monkeypatch.setattr(supervisor_mod.socket, "socket", lambda *_args, **_kwargs: TimedOut())
    with pytest.raises(RuntimeError, match="无法确认 OpenCode 监听状态"):
        assert_native_idle(str(workspace), 4096)


@pytest.mark.parametrize(
    "route",
    [(401, {"healthy": False}), (404, {"error": "not found"}), (200, {"healthy": False})],
)
def test_health_check_requires_2xx_json_and_healthy_true(route):
    with _server({"/global/health": route}) as port:
        assert _check_local_health(port) is False


def test_health_check_accepts_only_healthy_true():
    with _server({"/global/health": (200, {"healthy": True, "version": "1.18.33"})}) as port:
        assert _check_local_health(port) is True


@pytest.mark.parametrize("name", ["restart_opencode", "update_workspace"])
def test_command_data_must_match_worker_binding(tmp_path: Path, name: str):
    workspace = _workspace(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    build = _restart_message if name == "restart_opencode" else _update_message

    result, error = handle_command(
        build(other, 14096),
        RuntimeSupervisor(),
        workspace_root=str(workspace),
        opencode_url="http://127.0.0.1:14096",
    )
    assert result is None
    assert "工作区" in str(error)

    result, error = handle_command(
        build(workspace, 14097),
        RuntimeSupervisor(),
        workspace_root=str(workspace),
        opencode_url="http://127.0.0.1:14096",
    )
    assert result is None
    assert "端口" in str(error)

    result, error = handle_command(
        build(workspace, 14096),
        RuntimeSupervisor(),
        workspace_root=str(workspace),
        opencode_url="http://10.0.0.9:14096",
    )
    assert result is None
    assert "未绑定" in str(error)


def test_switch_channel_command_is_retired(tmp_path: Path):
    workspace = _workspace(tmp_path)
    message = envelope(
        request_id="req-switch",
        worker_instance_id="wrk_agent",
        environment_id=7,
        session_id=None,
        message_type="command",
        payload={"name": "switch_channel", "data": {"workspace": str(workspace), "port": 14096}},
    )
    result, error = handle_command(
        message,
        RuntimeSupervisor(),
        workspace_root=str(workspace),
        opencode_url="http://127.0.0.1:14096",
    )
    assert result is None
    assert error is not None


def test_version_contract_comes_from_the_module_not_env():
    assert worker_agent.__version__ == "1.4.5"
    config = load_agent_config(
        {
            "NEXUS_WORKER_TOKEN": "wtk_test",
            "NEXUS_WORKER_GATEWAY_URL": "ws://nexus/api/worker-gateway/connect",
            "NEXUS_WORKER_INSTANCE_ID": "wrk_test",
            "NEXUS_WORKER_ENVIRONMENT_ID": "7",
            "NEXUS_WORKER_AGENT_VERSION": "0.1.0",
            "NEXUS_WORKER_OPENCODE_URL": "http://127.0.0.1:14096",
            "NEXUS_WORKER_WORKSPACE_ROOT": "/srv/project",
        }
    )
    assert config.agent_version == worker_agent.__version__
    assert config.opencode_url == "http://127.0.0.1:14096"
    assert config.workspace_root == "/srv/project"
    assert AgentConfig(
        gateway_url="ws://nexus",
        worker_instance_id="wrk",
        environment_id=1,
        token="wtk_test",
    ).agent_version == worker_agent.__version__


def test_load_agent_config_does_not_read_old_astra_keys():
    with pytest.raises(SystemExit) as error:
        load_agent_config(
            {
                "ASTRA_WORKER_TOKEN": "wtk_old",
                "ASTRA_WORKER_GATEWAY_URL": "ws://old",
                "ASTRA_WORKER_INSTANCE_ID": "wrk_old",
                "ASTRA_WORKER_ENVIRONMENT_ID": "7",
            }
        )
    assert "NEXUS_WORKER_TOKEN" in str(error.value)
    assert "wtk_old" not in str(error.value)


def test_child_environment_never_contains_nexus_or_astra_credentials():
    cleaned = child_environ(
        {
            "PATH": "/usr/bin",
            "SAFE_VALUE": "kept",
            "NEXUS_WORKER_CHILD_ENV_ALLOW": (
                "SAFE_VALUE,NEXUS_WORKER_TOKEN,NEXUS_REMOTE_GATEWAY_URL,ASTRA_WORKER_TOKEN"
            ),
            "NEXUS_WORKER_TOKEN": "wtk_secret",
            "NEXUS_REMOTE_GATEWAY_URL": "http://10.0.0.8:8110",
            "ASTRA_WORKER_TOKEN": "wtk_old",
            "ASTRA_GATEWAY_MASTER_KEY": "master",
        }
    )
    assert cleaned["PATH"] == "/usr/bin"
    assert cleaned["SAFE_VALUE"] == "kept"
    assert not [name for name in cleaned if name.startswith(("NEXUS_", "ASTRA_"))]


class _Transport:
    def __init__(self, inbound: list[dict]) -> None:
        self.inbound = list(inbound)
        self.sent: list[dict] = []
        self.socket_release = threading.Event()

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    async def recv(self) -> dict:
        if self.inbound:
            return self.inbound.pop(0)
        while not self.socket_release.is_set():
            await asyncio.sleep(0.01)
        raise ConnectionError("closed")

    async def close(self) -> None:
        return


@pytest.mark.asyncio
async def test_thread_gate_stops_restart_before_the_real_side_effect(
    tmp_path: Path, monkeypatch
):
    workspace = _workspace(tmp_path)
    sup = RuntimeSupervisor(enabled=True, workspace_root=str(workspace), port=14096)
    sup._loop = asyncio.get_running_loop()
    manager = WorkerConnectionManager(
        AgentConfig(
            gateway_url="ws://nexus.internal/api/worker-gateway/connect",
            worker_instance_id="wrk_agent",
            environment_id=7,
            token="wtk_test",
            ping_seconds=3600,
            report_interval_seconds=0.0,
            opencode_url="http://127.0.0.1:14096",
            workspace_root=str(workspace),
        ),
        supervisor=sup,
    )
    entered = threading.Event()
    release = threading.Event()
    restarted: list[bool] = []

    def slow_native_idle(_workspace, _port, *, gate=None):
        entered.set()
        release.wait(5)
        if gate is not None:
            gate()
        return False

    monkeypatch.setattr(supervisor_mod, "assert_native_idle", slow_native_idle)

    async def fake_terminate():
        restarted.append(True)

    monkeypatch.setattr(sup, "_terminate_child", fake_terminate)
    transport = _Transport([_auth_reply(), _restart_message(workspace, 14096).to_dict()])
    task = asyncio.create_task(manager.run_session(transport))

    for _ in range(100):
        if entered.is_set():
            break
        await asyncio.sleep(0.02)
    manager.epoch = 4
    release.set()

    for _ in range(100):
        if not manager.control_tasks:
            break
        await asyncio.sleep(0.02)

    assert restarted == []
    assert not [
        item for item in transport.sent if item["payload"].get("stage") == "result"
    ]
    transport.socket_release.set()
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(task, 5)
