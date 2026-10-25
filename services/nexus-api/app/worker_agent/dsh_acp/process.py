"""监督单绑定 ACP 子进程。JSON-RPC 由固定官方 Python SDK 实现。"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from collections import Counter
from collections.abc import Awaitable, Callable
from importlib.metadata import version
from pathlib import Path
from typing import Any

from app.worker_agent.dsh_acp.profile import ACP_VERSION, DSH_VERSION
from app.worker_agent.process_env import child_environ

UpdateSink = Callable[[str, dict[str, Any]], Awaitable[None]]
PermissionSink = Callable[[str, list[dict], dict], Awaitable[str | None]]


class AcpProcessSupervisor:
    """进程只归属当前对象，不通过任意 PID、端口或名称查杀其他进程。"""

    def __init__(
        self, executable: Path, workspace: Path, home: Path, patch: Path,
        *, update_sink: UpdateSink | None = None, permission_sink: PermissionSink | None = None,
        permission_timeout: float = 60, control_timeout: float = 30,
    ) -> None:
        self.executable = executable
        self.workspace = workspace.resolve(strict=True)
        self.home = home.resolve(strict=True)
        self.patch = patch.resolve(strict=True)
        if not self.workspace.is_dir() or not self.home.is_dir() or self.patch.parent != self.home:
            raise ValueError("工作区或独立配置目录无效")
        self.update_sink = update_sink
        self.permission_sink = permission_sink
        self.permission_timeout = permission_timeout
        self.control_timeout = control_timeout
        self.process: asyncio.subprocess.Process | None = None
        self.connection = None
        self.session_id: str | None = None
        self.initialization: dict = {}
        self.update_counts: Counter = Counter()
        self.permission_count = 0
        self.stderr_bytes = 0
        self._drain: asyncio.Task | None = None
        self._prompt: asyncio.Task | None = None
        self._lock_fd: int | None = None
        self._permission_tasks: set[asyncio.Task] = set()
        self._tool_calls: dict[str, dict] = {}
        self.stop_confirmed = False

    async def start(self) -> dict:
        from acp import connect_to_agent

        if self.process is not None:
            raise RuntimeError("此监督器已启动")
        if version("agent-client-protocol") != ACP_VERSION:
            raise RuntimeError("ACP 客户端版本不匹配")
        env = child_environ()
        # 不继承已有 DSH Session/Profile、厂商 key 或 Worker token。
        env.update(DSH_HOME=str(self.home), DSH_TELEMETRY_DISABLED="1")
        check = await asyncio.create_subprocess_exec(
            str(self.executable), "--version", env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(check.communicate(), self.control_timeout)
        except BaseException:
            if check.returncode is None:
                check.kill()
            await check.wait()
            raise
        if check.returncode or out.decode().strip() != DSH_VERSION:
            raise RuntimeError("DSH 可执行版本不匹配")
        self._lock_home()
        options = {"start_new_session": True} if os.name == "posix" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
        }
        try:
            self.process = await asyncio.create_subprocess_exec(
                str(self.executable), "--profile", "acp", "--patch", str(self.patch),
                cwd=self.workspace, env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=1024 * 1024, **options,
            )
            self._drain = asyncio.create_task(self._drain_stderr())
            self.connection = connect_to_agent(
                self, self.process.stdin, self.process.stdout, use_unstable_protocol=True,
            )
            result = await asyncio.wait_for(
                self.connection.initialize(protocol_version=1), self.control_timeout,
            )
            self.initialization = result.model_dump(mode="json", by_alias=True, exclude_none=True)
            if result.protocol_version != 1:
                raise RuntimeError("ACP 协议版本不匹配")
            return self.initialization
        except BaseException:
            await self.dispose()
            raise

    def _lock_home(self) -> None:
        if os.name != "posix":
            # Windows 的跨进程文件锁/进程树实测单独门禁，不伪报可监督。
            raise RuntimeError("Windows DSH Session 所有权尚未验证")
        import fcntl

        fd = os.open(self.home / ".nexus-acp.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise RuntimeError("DSH_HOME 已有活跃所有者，拒绝第二个 Host") from None
        self._lock_fd = fd

    async def _drain_stderr(self) -> None:
        # 仅统计字节；第三方异常可能含凭据/提示词，绝不复制进平台日志。
        assert self.process and self.process.stderr
        while chunk := await self.process.stderr.read(8192):
            self.stderr_bytes += len(chunk)

    async def create_or_resume(self, *, session_id: str | None = None, mcp_servers=None) -> dict:
        if self.connection is None or self.session_id is not None:
            raise RuntimeError("连接尚未初始化或已有 Session")
        if session_id:
            response = await self.connection.resume_session(
                cwd=str(self.workspace), session_id=session_id, mcp_servers=mcp_servers or [],
            )
            self.session_id = session_id
        else:
            response = await self.connection.new_session(
                cwd=str(self.workspace), mcp_servers=mcp_servers or [],
            )
            self.session_id = response.session_id
        return response.model_dump(mode="json", by_alias=True, exclude_none=True)

    def submit(self, text: str) -> asyncio.Task:
        from acp.schema import TextContentBlock

        if self.connection is None or self.session_id is None:
            raise RuntimeError("Session 尚未绑定")
        if self._prompt and not self._prompt.done():
            raise RuntimeError("该 Session 已有受理中的 prompt")
        self._prompt = asyncio.create_task(self.connection.prompt(
            session_id=self.session_id, prompt=[TextContentBlock(type="text", text=text)],
        ))
        return self._prompt

    async def cancel(self, timeout: float = 10) -> dict:
        if self.connection is None or self.session_id is None:
            raise RuntimeError("Session 尚未绑定")
        prompt = self._prompt
        if prompt is None or prompt.done():
            # ACP 还允许自主活动，不能从本地没有 future 推断原生 Session 已停止。
            await self.dispose()
            return {"confirmed": self.stop_confirmed, "method": "owned_process_terminated"}
        try:
            await asyncio.wait_for(self.connection.cancel(session_id=self.session_id), timeout)
            response = await asyncio.wait_for(asyncio.shield(prompt), timeout)
            return {"confirmed": True, "method": "prompt_settled", "stop_reason": response.stop_reason}
        except Exception:
            # 仅发送通知不是停止证据。关闭本绑定的进程组后才报告强制停止。
            await self.dispose()
            return {"confirmed": self.stop_confirmed, "method": "owned_process_terminated"}

    async def request_permission(self, options, session_id: str, tool_call, **kwargs):
        from acp.schema import RequestPermissionResponse

        self.permission_count += 1
        selection = None
        call_id = tool_call.tool_call_id
        # 固定版 DSH permission 只携带 toolCallId，详情来自先到的 generic tool_call。
        # 不认识的 call 不能授权，不能只凭一个裸 ID 让前端默认批准。
        known_call = self._tool_calls.get(call_id)
        if session_id == self.session_id and known_call and self.permission_sink:
            task = asyncio.current_task()
            self._permission_tasks.add(task)
            try:
                selection = await asyncio.wait_for(self.permission_sink(
                    session_id,
                    [item.model_dump(mode="json", by_alias=True) for item in options],
                    {**known_call, **tool_call.model_dump(mode="json", by_alias=True, exclude_none=True)},
                ), self.permission_timeout)
            except Exception:
                selection = None
            finally:
                self._permission_tasks.discard(task)
        allowed = {item.option_id for item in options if item.kind in {"allow_once", "reject_once"}}
        if selection not in allowed:
            return RequestPermissionResponse(outcome={"outcome": "cancelled"})
        return RequestPermissionResponse(outcome={"outcome": "selected", "optionId": selection})

    async def session_update(self, session_id: str, update, **kwargs) -> None:
        if self.session_id is not None and session_id != self.session_id:
            raise RuntimeError("收到不属于当前绑定的 Session 事件")
        item = update.model_dump(mode="json", by_alias=True, exclude_none=True)
        kind = item.get("sessionUpdate", "unknown")
        self.update_counts[kind] += 1
        call_id = item.get("toolCallId")
        if kind == "tool_call" and isinstance(call_id, str):
            if len(self._tool_calls) >= 128:
                raise RuntimeError("活跃工具调用过多，无法安全关联授权")
            self._tool_calls[call_id] = item
        elif kind == "tool_call_update" and isinstance(call_id, str):
            if item.get("status") in {"completed", "failed"}:
                self._tool_calls.pop(call_id, None)
            elif call_id in self._tool_calls:
                self._tool_calls[call_id].update(item)
        if self.update_sink:
            await self.update_sink(session_id, item)

    def on_connect(self, connection) -> None:
        pass

    async def dispose(self) -> None:
        # SDK 关闭会取消回调与未完成调用；先禁止新的权限决策。
        self.permission_sink = None
        self._tool_calls.clear()
        for task in self._permission_tasks.copy():
            task.cancel()
        process = self.process
        if process and process.returncode is None:
            if process.stdin:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    if process.returncode is not None:
                        break
                    # start_new_session 的子进程句柄仍存活时，其 PGID 必须等于 PID。
                    with contextlib.suppress(ProcessLookupError):
                        if os.getpgid(process.pid) != process.pid:
                            raise RuntimeError("无法确认 ACP 进程组归属，拒绝终止") from None
                        os.killpg(process.pid, sig)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(process.wait(), 3)
        if self.connection:
            await self.connection.close()
            self.connection = None
        if self._drain:
            self._drain.cancel()
            await asyncio.gather(self._drain, return_exceptions=True)
        if process and process.returncode is not None:
            # 父进程退出不证明整组已退出；未知/残留子进程不释放本目录的所有权锁。
            async with asyncio.timeout(3):
                while True:
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        self.stop_confirmed = True
                        break
                    await asyncio.sleep(.05)
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
