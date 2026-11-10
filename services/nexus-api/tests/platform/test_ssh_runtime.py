"""真实 SSH TCP 转发及原生 HTTP/SSE，全部使用隔离 loopback 服务。"""

import asyncio
import json
import select
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import paramiko
import pytest

from app.config.settings import get_settings
from app.executors.opencode_server import OpenCodeServerClient
from app.runtime.ssh_client import SSHSession
from app.runtime.worker_connection import WorkerConnections


@contextmanager
def ssh_server():
    key = paramiko.RSAKey.generate(2048)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.1)
    stopped = threading.Event()
    transports, sockets, threads, channels = [], [], [], []

    class Server(paramiko.ServerInterface):
        def __init__(self):
            self.destinations = {}

        def check_auth_password(self, username, password):
            return paramiko.AUTH_SUCCESSFUL

        def get_allowed_auths(self, username):
            return "password"

        def check_channel_direct_tcpip_request(self, channel_id, origin, destination):
            assert destination[0] == "127.0.0.1"
            self.destinations[channel_id] = destination
            return paramiko.OPEN_SUCCEEDED

        def check_channel_request(self, kind, channel_id):
            if kind == "session":
                self.destinations[channel_id] = None
                return paramiko.OPEN_SUCCEEDED
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        def check_channel_exec_request(self, channel, command):
            if command not in {b"echo-stdin", b"no-exit-status"}:
                return False

            def echo():
                try:
                    content = channel.makefile("rb").read()
                    channel.sendall(content)
                    channel.send_stderr(b"diagnostic")
                    if command == b"echo-stdin":
                        channel.send_exit_status(0)
                finally:
                    channel.close()

            child = threading.Thread(target=echo, daemon=True)
            threads.append(child)
            child.start()
            return True

    def forward(channel, destination):
        remote = None
        try:
            remote = socket.create_connection(destination)
            sockets.append(remote)
            while not stopped.is_set():
                ready, _, _ = select.select([channel, remote], [], [], 0.1)
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (remote if source is channel else channel).sendall(data)
        except (OSError, EOFError):
            pass
        finally:
            if remote is not None:
                remote.close()
            try:
                channel.close()
            except EOFError:
                pass

    def serve_transport(connection):
        server = Server()
        transport = paramiko.Transport(connection)
        transports.append(transport)
        transport.add_server_key(key)
        try:
            transport.start_server(server=server)
            while not stopped.is_set() and transport.is_active():
                channel = transport.accept(0.1)
                if channel:
                    channels.append(channel)
                    if server.destinations[channel.get_id()] is None:
                        continue
                    thread = threading.Thread(
                        target=forward, args=(channel, server.destinations[channel.get_id()]), daemon=True
                    )
                    threads.append(thread)
                    thread.start()
        except (OSError, EOFError, paramiko.SSHException):
            pass
        finally:
            transport.close()

    def accept():
        while not stopped.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            thread = threading.Thread(target=serve_transport, args=(connection,), daemon=True)
            threads.append(thread)
            thread.start()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1], transports
    finally:
        stopped.set()
        listener.close()
        for transport in transports:
            transport.close()
        for remote in sockets:
            remote.close()
        thread.join(2)
        for child in threads:
            child.join(2)


@contextmanager
def native_server(identity):
    calls, subscriptions = [], []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            calls.append(self.path)
            if self.path.startswith("/event?"):
                subscriptions.append(self.path)
                payload = {"type": "session.status", "properties": {"sessionID": identity}}
                raw = ("data: " + json.dumps(payload) + "\n\n").encode()
                # An ended stream forces a new subscription, without replay state.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
            else:
                raw = json.dumps(
                    {"healthy": True, "version": get_settings().opencode_server_version, "worker": identity}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, calls, subscriptions
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


async def open_ssh(port):
    ssh = SSHSession(host="127.0.0.1", port=port, username="test", password="test")
    await ssh.connect()
    return ssh


async def test_real_ssh_forward_http_sse_concurrency_and_cleanup():
    with ssh_server() as (ssh_port, transports), native_server("one") as (port, calls, subscriptions):
        ssh = await open_ssh(ssh_port)
        forward = await ssh.forward_local(port)
        client = OpenCodeServerClient(base_url=forward.base_url)
        try:
            health = await asyncio.gather(*(client.health() for _ in range(12)))
            assert all(item["healthy"] for item in health)
            for _ in range(2):
                events = [event async for event in client.events("/workspace/任务一")]
                assert events[0]["properties"]["sessionID"] == "one"
            assert len(subscriptions) == 2
            assert all("directory=" in path for path in subscriptions)
            assert len(transports) == 1
        finally:
            url = forward.base_url
            await forward.close()
            await ssh.close()
        assert not forward.connections
        async with httpx.AsyncClient() as http:
            with pytest.raises(httpx.ConnectError):
                await http.get(url + "/global/health")
        assert len(calls) == 14


async def test_real_ssh_exec_feeds_stdin_and_drains_concurrent_commands():
    with ssh_server() as (port, transports):
        ssh = await open_ssh(port)
        content = b"payload\n" * 40000
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(ssh.run_command("echo-stdin", stdin=content) for _ in range(10))), 15
            )
            assert all(result == (0, content.decode(), "diagnostic") for result in results)
            assert len(transports) == 1
        finally:
            await ssh.close()


async def test_missing_ssh_exit_status_keeps_remote_result_unknown():
    with ssh_server() as (port, _):
        ssh = await open_ssh(port)
        try:
            with pytest.raises(ConnectionError, match="远端执行结果未知"):
                await ssh.run_command("no-exit-status", stdin=b"in flight")
        finally:
            await ssh.close()


async def test_pool_reuses_per_worker_and_recovers_actual_ssh_disconnect(monkeypatch):
    from app.runtime.environment import ExecutionEnvironmentRuntime

    with (
        ssh_server() as (ssh_port, transports),
        native_server("a") as (a, _, _),
        native_server("b") as (b, _, _),
    ):

        async def open_session(self, worker):
            return await open_ssh(ssh_port)

        monkeypatch.setattr(ExecutionEnvironmentRuntime, "open_session", open_session)
        pool = WorkerConnections()
        workers = [
            SimpleNamespace(
                id=identity,
                host="127.0.0.1",
                port=ssh_port,
                username="test",
                ssh_password="test",
                opencode_port=port,
            )
            for identity, port in (("a", a), ("b", b))
        ]
        try:
            same = await asyncio.gather(*(pool.get(workers[0]) for _ in range(8)))
            assert all(connection is same[0] for connection in same)
            await asyncio.gather(*(pool.probe(worker) for worker in workers))
            assert pool.online("a") and pool.online("b")
            clients = [(await pool.get(worker)).client() for worker in workers]
            results = await asyncio.gather(*(client.request("GET", "/identity") for client in clients))
            assert [result["worker"] for result in results] == ["a", "b"]
            original = await pool.get(workers[0])
            # Cut the actual TCP/SSH transport; no native process is touched.
            transports[0].close()
            for _ in range(100):
                if not original.ssh.is_active():
                    break
                await asyncio.sleep(0.01)
            await pool.probe(workers[0])
            assert await pool.get(workers[0]) is not original
            assert pool.online("a") and pool.online("b")
            assert (await clients[1].request("GET", "/identity"))["worker"] == "b"
        finally:
            await pool.aclose()
        assert not pool.connections


async def test_actual_opencode_native_api_over_ssh(tmp_path):
    """使用当前配置指定的真实 OpenCode；不调用外部模型。"""
    import os
    import shutil
    import subprocess

    binary = shutil.which(get_settings().opencode_binary)
    if not binary:
        pytest.skip("当前环境未安装配置指定的 OpenCode")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "opencode.json"
    config.write_text("{}")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = {
        **os.environ,
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "OPENCODE_CONFIG": str(config),
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
    }
    for name in ("OPENCODE_SERVER_PASSWORD", "OPENCODE_SERVER_USERNAME"):
        environment.pop(name, None)
    with (tmp_path / "opencode.log").open("wb") as log:
        process = subprocess.Popen(
            [binary, "serve", "--hostname", "127.0.0.1", "--port", str(port)],
            cwd=workspace,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            with ssh_server() as (ssh_port, _):
                ssh = await open_ssh(ssh_port)
                forward = await ssh.forward_local(port)
                client = OpenCodeServerClient(base_url=forward.base_url)
                try:
                    for _ in range(100):
                        try:
                            health = await client.health()
                            break
                        except Exception:
                            if process.poll() is not None:
                                pytest.fail("真实 OpenCode 未启动，请查看隔离测试日志")
                            await asyncio.sleep(0.1)
                    else:
                        pytest.fail("真实 OpenCode 健康检查超时")
                    assert health["version"] == get_settings().opencode_server_version
                    subscribed = asyncio.Event()

                    async def created_event():
                        async for event in client.events(workspace, on_subscribed=subscribed.set):
                            if event["type"] == "session.created":
                                return event["properties"]["info"]["id"]

                    listener = asyncio.create_task(created_event())
                    try:
                        await asyncio.wait_for(subscribed.wait(), 5)
                        session = await client.create_session(
                            workspace,
                            title="SSH 验证",
                            model_name=None,
                            agent=None,
                        )
                        assert await asyncio.wait_for(listener, 5) == session["id"]
                    finally:
                        listener.cancel()
                        await asyncio.gather(listener, return_exceptions=True)
                    assert await client.status(workspace, session["id"]) == "idle"
                    assert await client.children(workspace, session["id"]) == []
                    assert await client.questions(workspace) == []
                    assert await client.permissions(workspace) == []
                    assert await client.abort(workspace, session["id"])
                    # Local transport release never terminates the real server.
                    await forward.close()
                    await ssh.close()
                    assert process.poll() is None
                    ssh = await open_ssh(ssh_port)
                    forward = await ssh.forward_local(port)
                    recovered = OpenCodeServerClient(base_url=forward.base_url)
                    assert await recovered.status(workspace, session["id"]) == "idle"
                    assert process.poll() is None
                finally:
                    await forward.close()
                    await ssh.close()
        finally:
            process.terminate()
            await asyncio.to_thread(process.wait, 10)


async def test_failed_http_probe_preserves_live_ssh_and_workspace(monkeypatch):
    from app.runtime.environment import ExecutionEnvironmentRuntime

    with ssh_server() as (ssh_port, transports), native_server("worker") as (port, _, _):
        async def open_session(self, worker):
            return await open_ssh(ssh_port)

        monkeypatch.setattr(ExecutionEnvironmentRuntime, "open_session", open_session)
        worker = SimpleNamespace(
            id="worker", host="127.0.0.1", port=ssh_port, username="test",
            ssh_password="test", opencode_port=port,
        )
        pool = WorkerConnections()
        try:
            await pool.probe(worker)
            original = await pool.get(worker)
            client = original.client()
            native_health = OpenCodeServerClient.health

            async def unavailable(self):
                raise TimeoutError("busy health endpoint")

            monkeypatch.setattr(OpenCodeServerClient, "health", unavailable)
            with pytest.raises(TimeoutError):
                await pool.probe(worker)
            assert not pool.online(worker.id)
            assert await pool.get(worker) is original and original.ssh.is_active()
            assert (await client.request("GET", "/identity"))["worker"] == "worker"
            monkeypatch.setattr(OpenCodeServerClient, "health", native_health)
            await pool.probe(worker)
            assert pool.online(worker.id) and len(transports) == 1
            assert await pool.get(worker) is original
        finally:
            await pool.aclose()
