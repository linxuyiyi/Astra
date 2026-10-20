"""受管进程用 pid 和 create_time 确认身份，停止时连同进程组一起退出。"""

from __future__ import annotations

import importlib.util
import os
import signal
import socket
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SERVICE_SCRIPT = ROOT / "ops" / "dev" / "astra_service.py"


def _load_service_module():
    spec = importlib.util.spec_from_file_location("astra_service_stop_under_test", SERVICE_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


service = _load_service_module()
procman = service.procman


def _settings(tmp_path: Path):
    from types import SimpleNamespace

    return SimpleNamespace(
        gateway_enabled=True,
        mcp_transport="streamable-http",
        resolved_runtime_dir=tmp_path / "run",
        resolved_log_dir=tmp_path / "logs",
    )


def test_identity_matches_create_time_and_detects_pid_reuse(monkeypatch):
    info = {
        "pid": 9,
        "pgid": 9,
        "create_time": "1729000000.010",
        "cwd": "/srv/backend",
        "command": service.gateway_command("dev"),
    }
    live = {"pgid": 9, "create_time": "1729000000.010", "cwd": "/srv/backend"}
    monkeypatch.setattr(procman, "process_exists", lambda pid: True)
    monkeypatch.setattr(procman, "is_zombie", lambda pid: False)
    monkeypatch.setattr(procman, "read_process_identity", lambda pid: dict(live))

    assert procman.identify_process(info, 9) == "ours"
    live["create_time"] = "1729000001.010"
    assert procman.identify_process(info, 9) == "reused"
    live["create_time"] = info["create_time"]
    live["cwd"] = "/somewhere/else"
    assert procman.identify_process(info, 9) == "ours"
    monkeypatch.setattr(procman, "read_process_identity", lambda pid: None)
    assert procman.identify_process(info, 9) == "unknown"
    monkeypatch.setattr(procman, "process_exists", lambda pid: False)
    assert procman.identify_process(info, 9) == "gone"


def test_stop_keeps_state_and_does_not_signal_unconfirmed_processes(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    backend = {
        "pid": 4242,
        "pgid": 4242,
        "create_time": "10.000",
        "cwd": str(tmp_path),
        "command": service.gateway_command("dev"),
        "log": str(tmp_path / "backend.log"),
    }
    frontend = {
        "pid": 4243,
        "pgid": 4243,
        "create_time": "11.000",
        "cwd": str(tmp_path),
        "command": service.frontend_command("dev", "npm", lambda executable, args: [executable, *args]),
        "log": str(tmp_path / "frontend.log"),
    }
    service.save_state(settings, "dev", {"backend": backend, "frontend": frontend})
    monkeypatch.setattr(procman, "process_exists", lambda pid: True)
    monkeypatch.setattr(procman, "is_zombie", lambda pid: False)

    def identify(info, pid):
        return "ours" if pid == 4242 else "unknown"

    monkeypatch.setattr(procman, "identify_process", identify)
    signaled: list[int] = []

    def refuse(info, **kwargs):
        signaled.append(int(info["pid"]))
        return False

    monkeypatch.setattr(procman, "stop_managed", refuse)
    monkeypatch.setattr(procman, "reap", lambda pid, pgid: None)

    assert service.stop(settings) == 1
    remaining = service.managed_processes(settings)
    assert set(remaining) == {"backend", "frontend"}
    assert signaled == [4242]


def test_reused_pid_is_not_signaled(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    service.save_state(
        settings,
        "dev",
        {
            "gateway": {
                "pid": 50,
                "pgid": 50,
                "create_time": "1.000",
                "cwd": str(tmp_path),
                "command": service.gateway_command("dev"),
                "log": str(tmp_path / "gateway.log"),
            }
        },
    )
    monkeypatch.setattr(procman, "process_exists", lambda pid: True)
    monkeypatch.setattr(procman, "is_zombie", lambda pid: False)
    monkeypatch.setattr(procman, "identify_process", lambda info, pid: "reused")
    monkeypatch.setattr(procman, "reap", lambda pid, pgid: None)

    def refuse(info, **kwargs):
        raise AssertionError("reused pid must not be signaled")

    monkeypatch.setattr(procman, "stop_managed", refuse)
    assert service.stop(settings) == 0
    assert service.managed_processes(settings) == {}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until(predicate, timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX")
def test_real_stop_frees_port_and_start_can_bind_again(tmp_path, monkeypatch):
    script = tmp_path / "hold_port.py"
    script.write_text(
        "\n".join(
            [
                "import os",
                "import time",
                "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer",
                "",
                "port = int(__import__('sys').argv[2])",
                "",
                "class Handler(BaseHTTPRequestHandler):",
                "    def do_GET(self):",
                "        self.send_response(200)",
                "        self.end_headers()",
                "        self.wfile.write(b'ok')",
                "",
                "    def log_message(self, format, *args):",
                "        return",
                "",
                "pid = os.fork()",
                "if pid == 0:",
                "    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()",
                "while True:",
                "    time.sleep(1)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    port = _free_port()
    command = [sys.executable, str(script), "--port", str(port)]
    calls: list[tuple[int, int]] = []
    real_killpg = os.killpg

    def spy(pgid, sig):
        calls.append((pgid, sig))
        return real_killpg(pgid, sig)

    monkeypatch.setattr(procman.os, "killpg", spy)
    info = None
    try:
        info = service.spawn(settings, "backend", command, tmp_path)
        assert info["pgid"] == info["pid"]
        live = procman.read_process_identity(info["pid"])
        assert live is not None
        assert procman.create_times_match(info["create_time"], live["create_time"])
        service.save_state(settings, "dev", {"backend": info})
        assert _wait_until(lambda: service.procman.port_in_use(port))
        assert service.stop(settings) == 0
        assert calls[0] == (info["pgid"], signal.SIGTERM)
        assert not procman.port_in_use(port)
        assert not service.state_path(settings).exists()
        assert not service.process_exists(int(info["pid"]))

        restarted = service.spawn(settings, "backend", command, tmp_path)
        info = restarted
        service.save_state(settings, "dev", {"backend": restarted})
        assert _wait_until(lambda: procman.port_in_use(port))
        assert service.stop(settings) == 0
        assert not procman.port_in_use(port)
    finally:
        if info and (service.process_exists(int(info["pid"])) or procman.port_in_use(port)):
            procman.stop_managed(info)


@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX")
def test_dead_leader_is_not_signaled_and_state_is_kept(tmp_path):
    script = tmp_path / "hold_port.py"
    script.write_text(
        "\n".join(
            [
                "import os",
                "import time",
                "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer",
                "",
                "port = int(__import__('sys').argv[2])",
                "",
                "class Handler(BaseHTTPRequestHandler):",
                "    def do_GET(self):",
                "        self.send_response(200)",
                "        self.end_headers()",
                "",
                "    def log_message(self, format, *args):",
                "        return",
                "",
                "if os.fork() == 0:",
                "    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()",
                "while True:",
                "    time.sleep(1)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    port = _free_port()
    command = [sys.executable, str(script), "--port", str(port)]
    info = None
    try:
        info = service.spawn(settings, "backend", command, tmp_path)
        service.save_state(settings, "dev", {"backend": info})
        assert _wait_until(lambda: procman.port_in_use(port))
        os.kill(int(info["pid"]), signal.SIGKILL)
        os.waitpid(int(info["pid"]), 0)
        assert procman.port_in_use(port)
        assert service.stop(settings) == 1
        assert service.managed_processes(settings)["backend"]["pid"] == info["pid"]
        assert procman.port_in_use(port)
    finally:
        if info:
            os.killpg(int(info["pgid"]), signal.SIGKILL)
