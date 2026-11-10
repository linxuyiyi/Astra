"""Worker 本地维护指令契约：原生 Session 校验、健康检查、绑定校验与控制闸门。"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.worker_tools import supervisor as supervisor_mod
from app.worker_tools.process_env import child_environ
from app.worker_tools.supervisor import (
    _check_local_health,
    assert_native_idle,
)


@contextmanager
def _server(routes: dict[str, tuple[int, object]]):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status, body = routes.get(urlsplit(self.path).path, (404, {"error": "not found"}))
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
def test_unknown_or_malformed_native_state_is_conservatively_rejected(tmp_path: Path, body):
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
