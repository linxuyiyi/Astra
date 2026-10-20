"""Employee-local persistent Serve lifecycle; the assistant uses its own client."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from app.executors.errors import OpenCodeServerUnavailable
from app.executors.opencode_server import OpenCodeServerClient, OpenCodeServerExecutor
from app.executors.process import local_command, process_group_options
from app.runtime.opencode_workspace import discover_workspace_config

# Only terminate processes started by this worker, never an unknown port owner.
_processes: dict[int, subprocess.Popen] = {}
SERVE_LOCK_DIR = "nexus-employee-serve"


@asynccontextmanager
async def port_lock(port: int, timeout: float):
    directory = Path(tempfile.gettempdir()) / SERVE_LOCK_DIR
    directory.mkdir(mode=0o700, exist_ok=True)
    handle = (directory / f"{port}.lock").open("a+b")
    handle.write(b"0")
    handle.flush()
    deadline = asyncio.get_running_loop().time() + timeout
    acquired = False
    try:
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("等待员工 Serve 启动锁超时") from None
                await asyncio.sleep(0.1)
        yield directory / f"{port}.json"
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class LocalOpenCodeServerExecutor(OpenCodeServerExecutor):
    def __init__(self, environment, settings, **kwargs):
        self.environment = environment
        self.workspace = settings.employee_workspace_path(environment.workspace_root).resolve()
        super().__init__(
            settings,
            model_name=environment.model_name,
            client=OpenCodeServerClient(
                settings,
                base_url=f"http://127.0.0.1:{environment.opencode_server_port}",
            ),
            **kwargs,
        )

    async def ensure_ready(self):
        port = self.environment.opencode_server_port
        try:
            if port in {self.settings.opencode_server_port, self.settings.assistant_opencode_port}:
                raise RuntimeError("端口与平台公共/助手 Serve 冲突，请人工配置员工独立端口")
            lock_timeout = self.settings.opencode_server_start_timeout_seconds + 10
            async with port_lock(port, lock_timeout) as owner_path:
                owner = {"employee_id": self.environment.employee_id, "workspace": str(self.workspace)}
                healthy = False
                try:
                    await self.client.health()
                    healthy = True
                except Exception:
                    pass
                if healthy:
                    if owner_path.exists() and json.loads(owner_path.read_text(encoding="utf-8")) != owner:
                        raise RuntimeError("端口冲突：该 Serve 属于另一员工或工作空间")
                    paths = await self.client.request("GET", "/path")
                    if Path(paths.get("directory", "")).resolve() != self.workspace:
                        raise RuntimeError("端口冲突：Serve 的启动 Workspace 与员工配置不一致")
                    owner_path.write_text(json.dumps(owner), encoding="utf-8")
                    return
                process = _processes.get(port)
                if (
                    process
                    and process.poll() is None
                    and owner_path.exists()
                    and json.loads(owner_path.read_text(encoding="utf-8")) == owner
                ):
                    process.terminate()
                    await asyncio.to_thread(process.wait, timeout=5)
                with socket.socket() as probe:
                    if os.name != "nt":
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    try:
                        probe.bind(("127.0.0.1", port))
                    except OSError as exc:
                        raise RuntimeError(f"端口冲突：127.0.0.1:{port} 被其他进程占用") from exc
                if not self.workspace.is_dir():
                    raise RuntimeError("Workspace 不存在")
                if discover_workspace_config(self.workspace) is None:
                    raise RuntimeError(
                        f"Workspace 未提供 OpenCode 配置：{self.workspace} 及其父目录/全局 "
                        "~/.config/opencode/ 下均无 opencode.json；Nexus 不会生成临时配置，"
                        "请补齐工作区配置后重试"
                    )
                command = local_command(
                    self.environment.opencode_path,
                    [
                        "serve",
                        "--hostname",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ],
                )
                environment = os.environ.copy()
                environment.pop("OPENCODE_SERVER_PASSWORD", None)
                environment.pop("OPENCODE_SERVER_USERNAME", None)
                # The workspace owns its OpenCode config; Astra must never inject
                # one through OPENCODE_CONFIG or it would shadow company providers.
                environment.pop("OPENCODE_CONFIG", None)
                log_path = self.workspace / ".nexus-opencode-server.log"
                with log_path.open("ab") as log:
                    process = subprocess.Popen(
                        command,
                        cwd=self.workspace,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        **process_group_options(),
                    )
                _processes[port] = process
                owner_path.write_text(json.dumps(owner), encoding="utf-8")
                deadline = (
                    asyncio.get_running_loop().time() + self.settings.opencode_server_start_timeout_seconds
                )
                last_error = "尚未就绪"
                while asyncio.get_running_loop().time() < deadline:
                    if process.poll() is not None:
                        with log_path.open("rb") as log:
                            log.seek(max(0, log_path.stat().st_size - 2000))
                            detail = log.read().decode("utf-8", errors="replace")
                        raise RuntimeError(f"Serve 退出码 {process.returncode}：{detail}；日志：{log_path}")
                    try:
                        await self.client.health()
                        return
                    except Exception as exc:
                        last_error = str(exc)
                    await asyncio.sleep(0.2)
                raise TimeoutError(f"Serve 启动超时：{last_error}；日志：{log_path}")
        except Exception as exc:
            raise OpenCodeServerUnavailable(
                f"员工 {self.environment.employee_id}；环境 {self.environment.name} (LOCAL)；"
                f"Workspace={self.workspace}；Server Port={port}；"
                f"启动方式=本机后台进程；{exc}"
            ) from exc

    async def _execute(self, task, workspace: Path, *, session_id=None):
        await self.ensure_ready()
        return await super()._execute(task, self.workspace, session_id=session_id)
