"""无 systemd 单守护锁、孤儿清理、OpenCodeSupervisor 自愈与增量推送测试。"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from app.worker_agent.connection import AgentConfig, WorkerConnectionManager, handle_command
from app.worker_agent.inspection import inspect_runtime
from app.worker_agent.process_env import child_environ
from app.worker_agent.supervisor import (
    OpenCodeSupervisor,
    acquire_daemon_lock,
    cleanup_orphan_opencode,
    probe_listener,
)
from app.worker_gateway.protocol import envelope


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _init_git_repo(path: Path, *, remote: str = "https://example.test/org/repo.git") -> str:
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.test"],
        ["config", "user.name", "Worker"],
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    (path / "README.md").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "init"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", remote],
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_flock_single_instance_is_idempotent_and_fd_cloexec_survives_orphan(tmp_path: Path):
    """重复启动幂等返回 0；FD_CLOEXEC 保证守护被 SIGKILL 后残留子进程不会霸占锁。"""

    state_dir = tmp_path / "state"
    handle = acquire_daemon_lock(state_dir)
    assert handle is not None
    try:
        assert os.get_inheritable(handle.fileno()) is False

        # 同一宿主机重复执行 python -m app.worker_agent 应检测到排他锁并幂等退出 0
        env_file = tmp_path / "worker.env"
        env_file.write_text(
            "\n".join(
                [
                    "NEXUS_WORKER_TOKEN=wtk_secret",
                    "NEXUS_WORKER_GATEWAY_URL=ws://127.0.0.1:9/connect",
                    "NEXUS_AGENT_WORKER_ID=wrk_test",
                    "NEXUS_AGENT_WORKER_ID=42",
                    f"NEXUS_WORKER_STATE_DIR={state_dir}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        root = Path(__file__).resolve().parents[2]
        duplicate = subprocess.run(
            [sys.executable, "-m", "app.worker_agent", "--env-file", str(env_file)],
            cwd=root,
            env={**os.environ, "PYTHONPATH": str(root)},
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert duplicate.returncode == 0
    finally:
        handle.close()

    # 验证 FD_CLOEXEC：父守护获取锁并派生子进程后若父守护被 SIGKILL，残留子进程不继承锁句柄
    holder_code = textwrap.dedent(
        f"""
        import subprocess, sys, time
        from pathlib import Path
        from app.worker_agent.supervisor import acquire_daemon_lock
        lock = acquire_daemon_lock(Path({str(state_dir)!r}))
        assert lock is not None
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        print(child.pid, flush=True)
        time.sleep(30)
        """
    )
    root = Path(__file__).resolve().parents[2]
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    orphan_pid = int(holder.stdout.readline().strip())
    try:
        holder.kill()
        holder.wait(timeout=5)
        # 尽管 orphan_pid 仍存活，新守护必须能立即拿到 worker.lock
        reacquired = acquire_daemon_lock(state_dir)
        assert reacquired is not None
        reacquired.close()
    finally:
        try:
            os.kill(orphan_pid, signal.SIGKILL)
        except OSError:
            pass


def test_cleanup_orphan_opencode_only_kills_matching_env_id(tmp_path: Path):
    """孤儿清理只清理携带匹配 NEXUS_AGENT_WORKER_ID 的同 UID 进程，绝不误杀其他进程。"""

    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    uid = os.getuid()

    def write_proc(pid: int, env_pairs: dict[str, str]) -> Path:
        entry = proc_root / str(pid)
        entry.mkdir()
        payload = b"\x00".join(f"{k}={v}".encode() for k, v in env_pairs.items()) + b"\x00"
        (entry / "environ").write_bytes(payload)
        return entry

    target_entry = write_proc(91001, {"PATH": "/usr/bin", "NEXUS_AGENT_WORKER_ID": "7"})
    stubborn_entry = write_proc(91002, {"PATH": "/usr/bin", "NEXUS_AGENT_WORKER_ID": "7"})
    other_env = write_proc(91003, {"PATH": "/usr/bin", "NEXUS_AGENT_WORKER_ID": "8"})
    unrelated = write_proc(91004, {"PATH": "/usr/bin"})

    signals_sent: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        signals_sent.append((pid, sig))
        if pid == 91001 and sig == int(signal.SIGTERM):
            # 模拟收到 SIGTERM 立即退出
            for child in target_entry.iterdir():
                child.unlink()
            target_entry.rmdir()
        elif pid == 91002 and sig == int(signal.SIGKILL):
            for child in stubborn_entry.iterdir():
                child.unlink()
            stubborn_entry.rmdir()

    cleaned = cleanup_orphan_opencode(
        7,
        proc_root=proc_root,
        uid=uid,
        wait_seconds=0.12,
        kill_fn=fake_kill,
    )
    assert cleaned == [91001, 91002]
    assert (91001, int(signal.SIGTERM)) in signals_sent
    assert (91002, int(signal.SIGTERM)) in signals_sent
    assert (91002, int(signal.SIGKILL)) in signals_sent
    assert all(pid not in {91003, 91004} for pid, _ in signals_sent)
    assert other_env.exists()
    assert unrelated.exists()


def test_child_environ_injects_env_id_without_leaking_worker_secrets():
    env = child_environ(
        {
            "PATH": "/usr/bin",
            "NEXUS_WORKER_TOKEN": "wtk_top_secret",
            "NEXUS_WORKER_GATEWAY_URL": "ws://internal/connect",
        },
        env_id=19,
    )
    assert env["NEXUS_AGENT_WORKER_ID"] == "19"
    assert "NEXUS_WORKER_TOKEN" not in env
    assert "NEXUS_WORKER_GATEWAY_URL" not in env


class _CaptureTransport:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self._incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.closed = False

    def push(self, frame: dict[str, Any]) -> None:
        self._incoming.put_nowait(frame)

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        payload = message.get("payload") or {}
        if message.get("type") == "command" and payload.get("name") == "authenticate":
            self.push(
                {
                    "request_id": message["request_id"],
                    "agent_worker_id": message["agent_worker_id"],
                    "session_id": None,
                    "type": "response",
                    "payload": {
                        "stage": "result",
                        "ok": True,
                        "epoch": 3,
                        "ping_seconds": 3600,
                        "ping_timeout_seconds": 10,
                    },
                }
            )

    async def recv(self) -> dict[str, Any]:
        return await self._incoming.get()

    async def close(self) -> None:
        self.closed = True


async def test_restart_timeout_cancels_pending_restart_before_maintenance_releases(monkeypatch):
    supervisor = OpenCodeSupervisor(enabled=True)
    supervisor._loop = asyncio.get_running_loop()
    canceled = asyncio.Event()

    async def blocked_restart(*, gate=None):
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()

    monkeypatch.setattr(supervisor, "restart", blocked_restart)
    with pytest.raises(TimeoutError):
        await asyncio.to_thread(supervisor.restart_from_thread, timeout=0.02)
    await asyncio.wait_for(canceled.wait(), 1)


async def test_supervisor_crash_recovery_reports_last_error_and_recovers_cleanly(tmp_path: Path):
    """子进程崩溃后立即上报降级与最近错误摘要，退避重拉起恢复健康后上报就绪，停止时清理子进程。"""

    workspace = tmp_path / "workspace"
    _init_git_repo(workspace)
    port = _free_port()
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    crash_trigger = marker_dir / "crash.now"
    env_dump = marker_dir / "child_env.json"

    fake_opencode = tmp_path / "fake_opencode"
    fake_opencode.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            f"""
            import json, os, sys, threading, time
            from http.server import BaseHTTPRequestHandler, HTTPServer
            from pathlib import Path

            port = int(sys.argv[sys.argv.index("--port") + 1])
            Path({str(env_dump)!r}).write_text(json.dumps(dict(os.environ)), encoding="utf-8")
            trigger = Path({str(crash_trigger)!r})

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path.startswith("/global/health"):
                        body = json.dumps({{"healthy": True, "version": "1.18.35"}}).encode()
                    elif self.path.startswith("/session/status"):
                        body = b"{{}}"
                    elif self.path.startswith("/config"):
                        body = json.dumps({{
                            "model": "nexus-gateway/glm-5",
                            "provider": {{"nexus-gateway": {{"options": {{
                                "baseURL": "http://127.0.0.1:8110/v1", "apiKey": "agw_test"
                            }}}}}},
                            "mcp": {{"nexus-library": {{"url": "http://127.0.0.1:8201/mcp"}}}},
                        }}).encode()
                    elif self.path.startswith("/mcp"):
                        body = json.dumps({{"nexus-library": {{"status": "connected"}}}}).encode()
                    elif self.path.startswith("/skill"):
                        body = b"[]"
                    else:
                        body = b"{{}}"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                def log_message(self, *args):
                    return

            server = HTTPServer(("127.0.0.1", port), Handler)
            def watch():
                while True:
                    if trigger.exists():
                        trigger.unlink()
                        print("FATAL: simulated opencode panic on worker", file=sys.stderr, flush=True)
                        os._exit(17)
                    time.sleep(0.05)
            threading.Thread(target=watch, daemon=True).start()
            server.serve_forever()
            """
        ),
        encoding="utf-8",
    )
    fake_opencode.chmod(0o755)

    supervisor = OpenCodeSupervisor(
        enabled=True,
        opencode_bin=str(fake_opencode),
        agent_worker_id="12",
        port=port,
        workspace_root=str(workspace),
        environ={
            **os.environ,
            "NEXUS_WORKER_TOKEN": "wtk_never_leak",
        },
        health_interval_seconds=0.2,
        backoff_min_seconds=0.35,
        backoff_max_seconds=1.0,
        ready_timeout_seconds=5.0,
        startup_wait_seconds=5.0,
    )
    config = AgentConfig(
        gateway_url="ws://nexus.internal/api/worker-gateway/connect",
        agent_worker_id="wrk_sup",
        token="wtk_never_leak",
        ping_seconds=3600,
        opencode_url=f"http://127.0.0.1:{port}",
        workspace_root=str(workspace),
        report_interval_seconds=10.0,
    )
    manager = WorkerConnectionManager(config, supervisor=supervisor)
    transport = _CaptureTransport()

    await supervisor.start()
    session_task = asyncio.create_task(manager.run_session(transport))
    try:
        for _ in range(50):
            if transport.sent:
                break
            await asyncio.sleep(0.05)
        auth_frame = transport.sent[0]
        assert auth_frame["payload"]["name"] == "authenticate"
        assert auth_frame["payload"]["snapshot"]["opencode"]["status"] == "healthy"
        assert auth_frame["payload"]["snapshot"]["opencode"]["last_error"] is None

        # 验证子进程拿到了 NEXUS_AGENT_WORKER_ID 但未泄露 Worker Token
        child_env = json.loads(env_dump.read_text(encoding="utf-8"))
        assert child_env["NEXUS_AGENT_WORKER_ID"] == "12"
        assert "NEXUS_WORKER_TOKEN" not in child_env

        # 触发子进程崩溃：应先上报带 last_error 的降级 report，随后退避重启成功并上报恢复健康 report
        crash_trigger.write_text("boom", encoding="utf-8")

        reports: list[dict[str, Any]] = []
        for _ in range(80):
            reports = [
                frame["payload"]["snapshot"]
                for frame in transport.sent
                if frame.get("type") == "heartbeat" and frame["payload"].get("kind") == "report"
            ]
            has_degraded = any(
                snap["opencode"]["last_error"]
                and "simulated opencode panic" in snap["opencode"]["last_error"]
                for snap in reports
            )
            has_recovered = (
                has_degraded
                and reports[-1]["opencode"]["status"] == "healthy"
                and reports[-1]["opencode"]["last_error"] is None
            )
            if has_recovered:
                break
            await asyncio.sleep(0.05)

        assert supervisor.restart_count >= 1
        assert any(
            snap["opencode"]["last_error"] and "simulated opencode panic" in snap["opencode"]["last_error"]
            for snap in reports
        )
        assert reports[-1]["opencode"]["status"] == "healthy"
        assert reports[-1]["opencode"]["last_error"] is None
    finally:
        session_task.cancel()
        await asyncio.gather(session_task, return_exceptions=True)
        await supervisor.stop()

    # 守护停止后子进程组与端口必须已释放
    assert probe_listener(port) is False


def test_periodic_inspect_skips_external_probes_while_deep_check_verifies_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """日常周期自检不跑 git ls-remote 与模型网关请求；deep=True 按需校验两者。"""

    workspace = tmp_path / "ws"
    _init_git_repo(workspace)
    remote_calls: list[str] = []

    def fake_verify_remote(ws: str, branch: str, _secrets: set[str]):
        remote_calls.append(f"{ws}:{branch}")
        return True, None

    monkeypatch.setattr("app.worker_agent.inspection._verify_remote_read", fake_verify_remote)
    monkeypatch.setenv("NEXUS_GIT_CREDENTIAL_REVISION", "cred-v2")

    # 直接复用轻量 HTTP 桩验证 deep=False vs deep=True
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    calls: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            if self.path.startswith("/global/health"):
                status, payload = 200, {"healthy": True, "version": "1.18.35"}
            elif self.path.startswith("/session/status"):
                status, payload = 200, {}
            elif self.path.startswith("/config"):
                status, payload = (
                    200,
                    {
                        "model": "nexus-gateway/glm-5",
                        "provider": {
                            "nexus-gateway": {
                                "options": {
                                    "baseURL": f"http://127.0.0.1:{self.server.server_port}/v1",
                                    "apiKey": "agw_secret",
                                }
                            }
                        },
                        "mcp": {"nexus-library": {"url": "http://127.0.0.1:8201/mcp"}},
                    },
                )
            elif self.path.startswith("/mcp"):
                status, payload = 200, {"nexus-library": {"status": "connected"}}
            elif self.path.startswith("/skill"):
                status, payload = 200, []
            elif self.path == "/v1/models":
                status, payload = 200, {"data": [{"id": "glm-5"}]}
            else:
                status, payload = 404, {}
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        shallow = inspect_runtime(base, str(workspace), deep=False)
        assert remote_calls == []
        assert "/v1/models" not in calls
        assert shallow["repository"]["authentication_verified"] is None
        assert shallow["repository"]["credential_revision"] == "cred-v2"

        deep = inspect_runtime(base, str(workspace), deep=True)
        assert len(remote_calls) == 1
        assert "/v1/models" in calls
        assert deep["repository"]["authentication_verified"] is True
        assert deep["model"]["gateway_verified"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_update_workspace_command_fast_forwards_clean_repo_and_rejects_dirty(tmp_path: Path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)

    seed = tmp_path / "seed"
    _init_git_repo(seed, remote=str(origin))
    subprocess.run(
        ["git", "-C", str(seed), "push", "-q", "-u", "origin", "main"],
        check=True,
        capture_output=True,
    )

    worker_ws = tmp_path / "worker_ws"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(worker_ws)],
        check=True,
        capture_output=True,
    )

    # 远端推进一次新提交
    (seed / "README.md").write_text("v2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "v2"], check=True)
    expected_commit = subprocess.run(
        ["git", "-C", str(seed), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True)

    port = _free_port()
    supervisor = OpenCodeSupervisor()
    msg = envelope(
        request_id="cmd-update-1",
        agent_worker_id="wrk_1",
        session_id=None,
        message_type="command",
        payload={
            "name": "update_workspace",
            "data": {"workspace": str(worker_ws), "port": port, "branch": "main"},
        },
    )
    result, err = handle_command(
        msg,
        supervisor,
        workspace_root=str(worker_ws),
        opencode_url=f"http://127.0.0.1:{port}",
    )
    assert err is None
    assert result is not None
    assert result["updated"] is True
    assert result["commit"] == expected_commit
    assert result["snapshot"]["repository"]["commit"] == expected_commit

    # 工作区脏文件时必须拒绝更新
    (worker_ws / "README.md").write_text("dirty\n", encoding="utf-8")
    result2, err2 = handle_command(
        msg,
        supervisor,
        workspace_root=str(worker_ws),
        opencode_url=f"http://127.0.0.1:{port}",
    )
    assert result2 is None
    assert err2 is not None and "未提交变更" in err2
