import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import paramiko

_SSH_THREADS = ThreadPoolExecutor(max_workers=8, thread_name_prefix="astra-ssh")


async def _run_blocking(function, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_SSH_THREADS, function, *args)


class _AsyncStream:
    """Expose a Paramiko channel file through the async reader methods we use."""

    def __init__(self, stream: Any):
        self._stream = stream

    async def readline(self) -> bytes:
        return await _run_blocking(self._stream.readline)

    async def read(self, size: int = -1) -> bytes:
        return await _run_blocking(self._stream.read, size)

    async def close(self) -> None:
        await _run_blocking(self._stream.close)


class RemoteExecChannel:
    """Small asyncio-Process-compatible facade for a Paramiko exec channel."""

    def __init__(self, channel: Any):
        self._channel = channel
        self.stdout = _AsyncStream(channel.makefile("rb", -1))
        self.stderr = _AsyncStream(channel.makefile_stderr("rb", -1))
        self._returncode: int | None = None

    def is_active(self) -> bool:
        return not self._channel.closed and not self._channel.exit_status_ready()

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def wait(self) -> int:
        if self._returncode is None:
            self._returncode = await _run_blocking(self._channel.recv_exit_status)
        return self._returncode

    async def terminate(self) -> None:
        if not self._channel.closed:
            await asyncio.to_thread(self._channel.close)
        if self._returncode is None:
            self._returncode = -15

    async def close(self) -> None:
        await self.terminate()


class SSHSession:
    """One password-authenticated Paramiko connection used for remote execution."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        connect_timeout: int = 10,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.connect_timeout = connect_timeout
        self._client = paramiko.SSHClient()
        self._client.load_system_host_keys()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    async def connect(self) -> None:
        await _run_blocking(
            lambda: self._client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=self.connect_timeout,
                banner_timeout=self.connect_timeout,
                auth_timeout=self.connect_timeout,
                allow_agent=False,
                look_for_keys=False,
            )
        )
        transport = self._client.get_transport()
        if transport is not None:
            transport.set_keepalive(30)

    def is_active(self) -> bool:
        transport = self._client.get_transport()
        return bool(transport is not None and transport.is_active())

    async def open_exec(self, command: str, *, keep_stdin: bool = False) -> RemoteExecChannel:
        def open_channel():
            transport = self._client.get_transport()
            if transport is None or not transport.is_active():
                raise RuntimeError("SSH 连接未建立或已断开")
            channel = transport.open_session(timeout=self.connect_timeout)
            channel.exec_command(command)
            # Remote wrappers must never wait for interactive stdin.
            if not keep_stdin:
                channel.shutdown_write()
            return channel

        return RemoteExecChannel(await _run_blocking(open_channel))

    async def open_forward(self, port: int, origin: tuple[str, int]):
        transport = self._client.get_transport()
        if transport is None or not transport.is_active():
            raise ConnectionError("SSH 连接已断开")
        channel = await _run_blocking(
            lambda: transport.open_channel(
                "direct-tcpip", ("127.0.0.1", port), origin, timeout=self.connect_timeout
            )
        )
        channel.setblocking(False)
        return channel

    async def forward_local(self, port: int):
        return await LocalForward.create(self, port)

    async def hold_lock(self, paths: list[str]) -> RemoteExecChannel:
        """An SSH-owned flock is released on disconnect; no remote lock daemon."""
        import shlex

        command = " ".join("flock -n " + shlex.quote(path) for path in sorted(paths))
        command += " sh -c " + shlex.quote("printf 'NEXUS_LOCKED\\n'; read -r release")
        channel = await self.open_exec(command, keep_stdin=True)
        try:
            line = await asyncio.wait_for(channel.stdout.readline(), 10)
            if line.strip() != b"NEXUS_LOCKED":
                raise RuntimeError("同一 Worker 或工作区正在执行部署/更新，未取得远端锁")
            return channel
        except BaseException:
            await channel.close()
            raise

    async def run_command(self, command: str, *, stdin: bytes | None = None) -> tuple[int, str, str]:
        channel = await self.open_exec(command, keep_stdin=stdin is not None)

        def exchange():
            # One thread drains both outputs while feeding stdin, so concurrent
            # commands cannot exhaust the pool with blocked stream readers.
            remote = channel._channel
            remote.setblocking(False)
            pending = memoryview(stdin or b"")
            out, err = bytearray(), bytearray()
            eof_sent = stdin is None
            while True:
                if pending and remote.send_ready():
                    sent = remote.send(pending[:65536])
                    if not sent:
                        raise ConnectionError("SSH 命令输入中断")
                    pending = pending[sent:]
                if not pending and not eof_sent:
                    remote.shutdown_write()
                    eof_sent = True
                if remote.recv_ready():
                    out.extend(remote.recv(65536))
                if remote.recv_stderr_ready():
                    err.extend(remote.recv_stderr(65536))
                if remote.exit_status_ready() and not remote.recv_ready() and not remote.recv_stderr_ready():
                    code = remote.recv_exit_status()
                    if code < 0:
                        raise ConnectionError("SSH 未返回退出码，远端执行结果未知")
                    return code, out, err
                time.sleep(0.01)

        try:
            exit_code, stdout, stderr = await _run_blocking(exchange)
            return (
                exit_code,
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
            )
        finally:
            await channel.close()

    async def read_file(self, path: str) -> bytes | None:
        def read():
            with self._client.open_sftp() as sftp:
                try:
                    with sftp.open(path, "rb") as stream:
                        content = stream.read(1024 * 1024 + 1)
                        if len(content) > 1024 * 1024:
                            raise ValueError("远程配置超过 1 MiB，请人工处理")
                        return content
                except FileNotFoundError:
                    return None

        return await _run_blocking(read)

    async def write_file(self, path: str, content: bytes, *, mode: int = 0o600) -> None:
        def write():
            with self._client.open_sftp() as sftp:
                with sftp.open(path, "wb") as stream:
                    stream.write(content)
                sftp.chmod(path, mode)

        await _run_blocking(write)

    async def close(self) -> None:
        await asyncio.to_thread(self._client.close)


class LocalForward:
    """一个 loopback 监听器，所有 HTTP/SSE TCP 通道复用同一 SSH Transport。"""

    def __init__(self, ssh: SSHSession, remote_port: int):
        self.ssh, self.remote_port = ssh, remote_port
        self.connections: set[asyncio.Task] = set()

    @classmethod
    async def create(cls, ssh, remote_port):
        forward = cls(ssh, remote_port)
        forward.server = await asyncio.start_server(forward._accept, "127.0.0.1", 0)
        return forward

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        channel = None
        channel_fd = None
        pumps = []
        loop = asyncio.get_running_loop()
        try:
            channel = await self.ssh.open_forward(self.remote_port, writer.get_extra_info("peername"))
            readable = asyncio.Event()
            channel_fd = channel.fileno()
            loop.add_reader(channel_fd, readable.set)

            async def upstream():
                while data := await reader.read(65536):
                    view = memoryview(data)
                    while view:
                        try:
                            sent = channel.send(view)
                        except TimeoutError:
                            await asyncio.sleep(0.01)
                            continue
                        if not sent:
                            return
                        view = view[sent:]
                channel.shutdown_write()

            async def downstream():
                while True:
                    await readable.wait()
                    readable.clear()
                    while True:
                        try:
                            data = channel.recv(65536)
                        except TimeoutError:
                            break
                        if not data:
                            return
                        writer.write(data)
                        await writer.drain()

            pumps = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for completed in done:
                completed.result()
        except (OSError, EOFError, paramiko.SSHException):
            pass  # HTTP/SSE observes EOF; its owner reconnects and reconciles.
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            if channel is not None:
                if channel_fd is not None:
                    loop.remove_reader(channel_fd)
                try:
                    channel.close()
                except (OSError, EOFError):
                    pass
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self.connections.discard(task)

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        connections = list(self.connections)
        for connection in connections:
            connection.cancel()
        await asyncio.gather(*connections, return_exceptions=True)
