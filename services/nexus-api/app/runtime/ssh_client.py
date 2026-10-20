import asyncio
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

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def wait(self) -> int:
        if self._returncode is None:
            self._returncode = await _run_blocking(self._channel.recv_exit_status)
        return self._returncode

    async def terminate(self) -> None:
        if not self._channel.closed:
            await _run_blocking(self._channel.close)
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

    async def open_exec(self, command: str) -> RemoteExecChannel:
        def open_channel():
            transport = self._client.get_transport()
            if transport is None or not transport.is_active():
                raise RuntimeError("SSH 连接未建立或已断开")
            channel = transport.open_session(timeout=self.connect_timeout)
            channel.exec_command(command)
            # Remote wrappers must never wait for interactive stdin.
            channel.shutdown_write()
            return channel

        return RemoteExecChannel(await _run_blocking(open_channel))

    async def run_command(self, command: str) -> tuple[int, str, str]:
        channel = await self.open_exec(command)
        try:
            stdout, stderr, exit_code = await asyncio.gather(
                channel.stdout.read(),
                channel.stderr.read(),
                channel.wait(),
            )
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
        await _run_blocking(self._client.close)
