"""无 systemd 单守护锁、孤儿清理、OpenCodeSupervisor 自愈与增量推送测试。"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.worker_tools.inspection import inspect_runtime
from app.worker_tools.process_env import child_environ
from app.worker_tools.supervisor import (
    acquire_daemon_lock,
    cleanup_orphan_opencode,
)


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

        # 同一宿主机重复执行 python -m app.worker_tools 应检测到排他锁并幂等退出 0
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
            [sys.executable, "-m", "app.worker_tools", "--env-file", str(env_file)],
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
        from app.worker_tools.supervisor import acquire_daemon_lock
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

    monkeypatch.setattr("app.worker_tools.inspection._verify_remote_read", fake_verify_remote)
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


@pytest.mark.asyncio
async def test_process_supervisor_recovers_confirmed_exit_without_a_network_agent(tmp_path):
    import asyncio

    from app.worker_tools.supervisor import OpenCodeSupervisor

    binary = tmp_path / "opencode-test"
    binary.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
with open("starts.log", "a") as stream:
    stream.write(str(os.getpid()) + "\\n")
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        raw = json.dumps({"healthy": True, "version": "1.18.35"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def log_message(self, *args): pass
HTTPServer.allow_reuse_address = True
HTTPServer(("127.0.0.1", int(sys.argv[-1])), Handler).serve_forever()
"""
    )
    binary.chmod(0o755)
    supervisor = OpenCodeSupervisor(
        enabled=True,
        opencode_bin=str(binary),
        port=_free_port(),
        workspace_root=str(tmp_path),
        agent_worker_id="process-test",
        environ={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
        startup_wait_seconds=2,
        ready_timeout_seconds=2,
        health_interval_seconds=0.05,
        backoff_min_seconds=0.05,
        backoff_max_seconds=0.1,
    )
    try:
        await supervisor.start()
        initial = supervisor.pid
        assert initial is not None
        os.kill(initial, signal.SIGKILL)
        for _ in range(100):
            if supervisor.pid and supervisor.pid != initial:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("OpenCode 进程退出后没有恢复")
        assert supervisor.restart_count >= 1
        for _ in range(100):
            if len((tmp_path / "starts.log").read_text().splitlines()) >= 2:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("新的 OpenCode 子进程没有启动")
    finally:
        await supervisor.stop()
    assert supervisor.pid is None
