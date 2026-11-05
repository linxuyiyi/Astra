"""无 systemd 单守护锁、孤儿清理与 OpenCode 子进程保活监管器。

核心边界：
1. 排他单实例由内核 ``fcntl.flock(LOCK_EX | LOCK_NB)`` + ``FD_CLOEXEC`` 保证，不依赖 PID 文件判活。
2. 孤儿清理仅识别携带 ``NEXUS_AGENT_WORKER_ID=<id>`` 的同 UID 残留进程，绝不误杀未知端口占用者。
3. ``OpenCodeSupervisor`` 只负责本地 ``opencode serve`` 子进程生命周期、退避拉起与错误摘要上报；
   通信断连不重启子进程，子进程异常退出也绝不反推或猜测平台 Task/Run 终态。
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import os
import signal
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO

from app.worker_agent.process_env import child_environ

Gate = Callable[[], None]


def acquire_daemon_lock(state_dir: Path) -> IO[bytes] | None:
    """获取环境守护排他锁；已被存活守护持有时返回 None，供调用方幂等退出 0。"""

    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(state_dir, 0o700)
    except OSError:
        pass
    lock_path = state_dir / "worker.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        os.set_inheritable(fd, False)
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                os.close(fd)
                return None
            raise
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        return os.fdopen(fd, "r+b", buffering=0)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def cleanup_orphan_opencode(
    agent_worker_id: str,
    *,
    proc_root: Path = Path("/proc"),
    uid: int | None = None,
    wait_seconds: float = 3.0,
    kill_fn: Callable[[int, int], None] | None = None,
) -> list[int]:
    """扫描 /proc 清理当前环境遗留的受管 OpenCode 孤儿进程。"""

    if not proc_root.is_dir():
        return []
    expected_env = str(agent_worker_id)
    current_uid = os.getuid() if uid is None else uid
    self_pid = os.getpid()
    matched: list[int] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == self_pid:
            continue
        try:
            if entry.stat().st_uid != current_uid:
                continue
            raw_env = (entry / "environ").read_bytes()
        except OSError:
            continue
        if _extract_proc_env(raw_env, "NEXUS_AGENT_WORKER_ID") != expected_env:
            continue
        matched.append(pid)

    if not matched:
        return []

    for pid in matched:
        _signal_orphan(pid, signal.SIGTERM, kill_fn=kill_fn)

    deadline = time.monotonic() + max(wait_seconds, 0.0)
    remaining = list(matched)
    while remaining and time.monotonic() < deadline:
        remaining = [pid for pid in remaining if _pid_alive(pid, proc_root=proc_root, kill_fn=kill_fn)]
        if remaining:
            time.sleep(0.05)

    for pid in remaining:
        _signal_orphan(pid, signal.SIGKILL, kill_fn=kill_fn)

    return sorted(matched)


def _extract_proc_env(raw: bytes, key: str) -> str | None:
    prefix = f"{key}=".encode()
    for item in raw.split(b"\x00"):
        if item.startswith(prefix):
            return item[len(prefix) :].decode("utf-8", errors="replace")
    return None


def _signal_orphan(
    pid: int,
    sig: signal.Signals,
    *,
    kill_fn: Callable[[int, int], None] | None = None,
) -> None:
    if kill_fn is not None:
        try:
            kill_fn(pid, int(sig))
        except OSError:
            pass
        return
    try:
        pgid = os.getpgid(pid)
        if pgid > 1 and pgid != os.getpgrp():
            os.killpg(pgid, sig)
            return
    except OSError:
        pass
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def _pid_alive(
    pid: int,
    *,
    proc_root: Path,
    kill_fn: Callable[[int, int], None] | None,
) -> bool:
    if kill_fn is not None:
        return (proc_root / str(pid)).exists()
    if proc_root == Path("/proc"):
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False
    return (proc_root / str(pid)).exists()


def probe_listener(port: int, *, timeout: float = 1.0) -> bool:
    """只有明确的 ECONNREFUSED 才能证明端口上没有监听者。"""

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.connect(("127.0.0.1", port))
    except ConnectionRefusedError:
        return False
    except OSError as exc:
        raise RuntimeError("无法确认 OpenCode 监听状态，拒绝执行维护") from exc
    finally:
        probe.close()
    return True


def assert_native_idle(workspace: str, port: int, *, gate: Gate | None = None) -> bool:
    """只读检查本机原生 Session 是否空闲；返回端口当前是否存在监听者。"""

    if gate is not None:
        gate()
    if not probe_listener(port):
        return False
    query = urllib.parse.urlencode({"directory": workspace})
    url = f"http://127.0.0.1:{port}/session/status?{query}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            body = response.read(65536)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise RuntimeError("无法确认 OpenCode 原生 Session 状态，拒绝执行维护") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("无法确认 OpenCode 原生 Session 状态，拒绝执行维护") from exc
    try:
        states = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("无法确认 OpenCode 原生 Session 状态，拒绝执行维护") from exc
    if not isinstance(states, dict):
        raise RuntimeError("无法确认 OpenCode 原生 Session 状态，拒绝执行维护")
    busy = False
    malformed = False
    for item in states.values():
        if not isinstance(item, dict):
            malformed = True
            continue
        kind = item.get("type")
        if kind in {"busy", "retry"}:
            busy = True
        elif kind != "idle":
            malformed = True
    if busy:
        raise RuntimeError("OpenCode 存在正在执行的 Session，请先停止执行再维护环境")
    if malformed:
        raise RuntimeError("无法确认 OpenCode 原生 Session 状态，拒绝执行维护")
    return True


def _check_local_health(port: int, *, timeout: float = 1.0) -> bool:
    url = f"http://127.0.0.1:{port}/global/health"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read(65536).decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("healthy") is True


class OpenCodeSupervisor:
    """本地 OpenCode Server 受管子进程监管器。"""

    def __init__(
        self,
        *,
        enabled: bool = False,
        opencode_bin: str = "opencode",
        port: int = 14096,
        workspace_root: str = "",
        agent_worker_id: str | None = None,
        environ: Mapping[str, str] | None = None,
        on_state_change: Callable[[], None] | None = None,
        health_interval_seconds: float = 5.0,
        backoff_min_seconds: float = 1.0,
        backoff_max_seconds: float = 30.0,
        healthy_reset_seconds: float = 60.0,
        stop_timeout_seconds: float = 5.0,
        ready_timeout_seconds: float = 15.0,
        startup_wait_seconds: float = 10.0,
    ) -> None:
        self.enabled = enabled
        self.opencode_bin = opencode_bin
        self.port = int(port)
        self.workspace_root = workspace_root
        self.agent_worker_id = agent_worker_id
        self._environ = dict(environ) if environ is not None else None
        self._on_state_change = on_state_change
        self.health_interval_seconds = health_interval_seconds
        self.backoff_min_seconds = backoff_min_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.healthy_reset_seconds = healthy_reset_seconds
        self.stop_timeout_seconds = stop_timeout_seconds
        self.ready_timeout_seconds = ready_timeout_seconds
        self.startup_wait_seconds = startup_wait_seconds

        self.restart_count = 0
        self.last_error: str | None = None
        self._tail: deque[str] = deque(maxlen=30)
        self._proc: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._wake_monitor = asyncio.Event()
        self._restart_lock = asyncio.Lock()
        self._restarting = False
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_state_change_callback(self, callback: Callable[[], None] | None) -> None:
        self._on_state_change = callback

    def notify_state_change(self) -> None:
        if self._on_state_change is None:
            return
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._on_state_change)
        else:
            self._on_state_change()

    def on_connection_lost(self) -> None:
        """网关网络断连不重启本地 OpenCode 子进程。"""

        return

    def on_process_exit(self, _code: int) -> None:
        self.restart_count += 1

    @property
    def pid(self) -> int | None:
        if self._proc is None or self._proc.returncode is not None:
            return None
        return self._proc.pid

    async def start(self) -> None:
        if not self.enabled:
            return
        self._loop = asyncio.get_running_loop()
        self._stopping.clear()
        spawned = await self._spawn_child()
        if spawned:
            await self._wait_until_healthy_or_exit(timeout=self.startup_wait_seconds)
        self._monitor_task = asyncio.create_task(self._supervise_loop())

    async def stop(self) -> None:
        self._stopping.set()
        self._wake_monitor.set()
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
            self._monitor_task = None
        await self._terminate_child()

    async def restart(self, *, gate: Gate | None = None) -> None:
        if not self.enabled:
            raise RuntimeError("当前 Worker 未启用内置 OpenCode 监管器，无法重启")
        async with self._restart_lock:
            await asyncio.to_thread(assert_native_idle, self.workspace_root, self.port, gate=gate)
            if gate is not None:
                gate()
            self._restarting = True
            self._wake_monitor.set()
            try:
                await self._terminate_child()
                if gate is not None:
                    gate()
                self._tail.clear()
                spawned = await self._spawn_child()
                if not spawned:
                    self.notify_state_change()
                    raise RuntimeError(self.last_error or "OpenCode 启动失败")
                healthy = await self._wait_until_healthy_or_exit(
                    timeout=self.ready_timeout_seconds,
                    gate=gate,
                )
                if not healthy:
                    self.notify_state_change()
                    raise RuntimeError(self.last_error or "OpenCode 重载后健康检查失败")
                self.restart_count += 1
                self.last_error = None
                self.notify_state_change()
            finally:
                self._restarting = False
                self._wake_monitor.set()

    def restart_from_thread(self, *, gate: Gate | None = None, timeout: float = 30.0) -> None:
        if not self.enabled:
            raise RuntimeError("当前 Worker 未启用内置 OpenCode 监管器，无法重启")
        if self._loop is None or not self._loop.is_running():
            raise RuntimeError("OpenCode 监管器事件循环未运行，无法重启")
        future = asyncio.run_coroutine_threadsafe(self.restart(gate=gate), self._loop)
        try:
            future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise

    async def _spawn_child(self) -> bool:
        workspace = Path(self.workspace_root) if self.workspace_root else None
        if workspace is None or not workspace.is_dir():
            self.last_error = "Workspace 不存在，无法启动 OpenCode"
            return False
        try:
            occupied = await asyncio.to_thread(probe_listener, self.port, timeout=0.5)
        except RuntimeError as exc:
            self.last_error = str(exc)
            return False
        if occupied:
            self.last_error = f"端口 {self.port} 已被未知进程占用，拒绝覆盖启动"
            return False

        env = child_environ(self._environ, env_id=self.agent_worker_id)
        try:
            proc = await asyncio.create_subprocess_exec(
                self.opencode_bin,
                "serve",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(self.port),
                cwd=str(workspace),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            self.last_error = f"启动 OpenCode 失败：{exc}"
            return False

        self._proc = proc
        self._stdout_task = asyncio.create_task(self._read_output(proc))
        return True

    async def _read_output(self, proc: asyncio.subprocess.Process) -> None:
        if proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if text:
                    self._tail.append(text[-500:])
        except asyncio.CancelledError:
            return

    def _format_failure(self, prefix: str) -> str:
        tail = "\n".join(self._tail).strip()
        return f"{prefix}：{tail}" if tail else prefix

    async def _wait_until_healthy_or_exit(
        self,
        *,
        timeout: float,
        gate: Gate | None = None,
    ) -> bool:
        deadline = time.monotonic() + max(timeout, 0.1)
        while time.monotonic() < deadline and not self._stopping.is_set():
            if gate is not None:
                gate()
            proc = self._proc
            if proc is None:
                return False
            if proc.returncode is not None:
                if self._stdout_task is not None:
                    await asyncio.gather(self._stdout_task, return_exceptions=True)
                self.last_error = self._format_failure(f"OpenCode 进程异常退出 (exit={proc.returncode})")
                return False
            healthy = await asyncio.to_thread(_check_local_health, self.port, timeout=0.8)
            if healthy:
                self.last_error = None
                return True
            await asyncio.sleep(0.1)
        if not self._stopping.is_set():
            self.last_error = self._format_failure("OpenCode 启动健康检查超时")
        return False

    async def _terminate_child(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            _terminate_process_group(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.stop_timeout_seconds)
            except TimeoutError:
                _terminate_process_group(proc.pid, signal.SIGKILL)
                await asyncio.gather(proc.wait(), return_exceptions=True)
        if self._stdout_task is not None:
            try:
                await asyncio.wait_for(self._stdout_task, timeout=0.5)
            except TimeoutError:
                self._stdout_task.cancel()
                await asyncio.gather(self._stdout_task, return_exceptions=True)
            self._stdout_task = None

    async def _supervise_loop(self) -> None:
        attempt = 0
        healthy_since: float | None = (
            time.monotonic() if self.last_error is None and self._proc is not None else None
        )
        consecutive_unhealthy = 0

        while not self._stopping.is_set():
            if self._restarting:
                self._wake_monitor.clear()
                await self._wait_event_or_timeout(self._wake_monitor, 0.2)
                healthy_since = time.monotonic() if self.last_error is None else None
                consecutive_unhealthy = 0
                attempt = 0
                continue

            proc = self._proc
            if proc is None or proc.returncode is not None:
                if proc is not None and proc.returncode is not None:
                    if self._stdout_task is not None:
                        await asyncio.gather(self._stdout_task, return_exceptions=True)
                        self._stdout_task = None
                    self._proc = None
                    self.restart_count += 1
                    self.last_error = self._format_failure(f"OpenCode 进程异常退出 (exit={proc.returncode})")
                    healthy_since = None
                    self.notify_state_change()

                delay = min(
                    self.backoff_max_seconds,
                    self.backoff_min_seconds * (2**attempt),
                )
                attempt = min(attempt + 1, 30)
                if await self._sleep_unless_stopping(delay):
                    return
                if self._restarting:
                    continue
                self._tail.clear()
                spawned = await self._spawn_child()
                if not spawned:
                    self.notify_state_change()
                    continue
                healthy = await self._wait_until_healthy_or_exit(timeout=self.ready_timeout_seconds)
                if healthy:
                    self.last_error = None
                    healthy_since = time.monotonic()
                    consecutive_unhealthy = 0
                self.notify_state_change()
                continue

            self._wake_monitor.clear()
            exited = await self._wait_proc_or_interval(proc, self.health_interval_seconds)
            if self._stopping.is_set() or self._restarting:
                continue
            if exited:
                continue

            healthy = await asyncio.to_thread(_check_local_health, self.port, timeout=1.0)
            if healthy:
                consecutive_unhealthy = 0
                if healthy_since is None:
                    healthy_since = time.monotonic()
                elif time.monotonic() - healthy_since >= self.healthy_reset_seconds:
                    attempt = 0
                if self.last_error is not None:
                    self.last_error = None
                    self.notify_state_change()
                continue

            consecutive_unhealthy += 1
            if consecutive_unhealthy >= 3:
                consecutive_unhealthy = 0
                healthy_since = None
                self.restart_count += 1
                self.last_error = self._format_failure("OpenCode 健康探活连续超时")
                self.notify_state_change()
                await self._terminate_child()

    async def _wait_proc_or_interval(self, proc: asyncio.subprocess.Process, interval: float) -> bool:
        proc_task = asyncio.create_task(proc.wait())
        wake_task = asyncio.create_task(self._wake_monitor.wait())
        stop_task = asyncio.create_task(self._stopping.wait())
        try:
            done, _ = await asyncio.wait(
                {proc_task, wake_task, stop_task},
                timeout=max(interval, 0.05),
                return_when=asyncio.FIRST_COMPLETED,
            )
            return proc_task in done
        finally:
            for task in (proc_task, wake_task, stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(proc_task, wake_task, stop_task, return_exceptions=True)

    async def _sleep_unless_stopping(self, delay: float) -> bool:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=max(delay, 0.01))
            return True
        except TimeoutError:
            return self._stopping.is_set()

    @staticmethod
    async def _wait_event_or_timeout(event: asyncio.Event, timeout: float) -> None:
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            return


def _terminate_process_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
        return
    except OSError:
        pass
    try:
        os.kill(pid, sig)
    except OSError:
        pass


RuntimeSupervisor = OpenCodeSupervisor
