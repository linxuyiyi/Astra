"""Worker Agent 连接管理：主动连接、认证、心跳、退避重连。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import random
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from app.worker_agent import __version__
from app.worker_agent.files import (
    FILE_COMMAND_LIMIT,
    FILE_COMMANDS,
    FileCommandError,
    connection_gate,
    execute_file_command,
)
from app.worker_agent.supervisor import RuntimeSupervisor
from app.worker_agent.transport import WorkerTransportError
from app.worker_gateway.heartbeat import HeartbeatMonitor
from app.worker_gateway.protocol import (
    WorkerGatewayError,
    WorkerMessage,
    dump_message,
    envelope,
    frame_acceptable,
    parse_message,
)

Connect = Callable[[], Awaitable["FrameTransport"]]

CONTROL_COMMANDS = {"inspect_runtime", "status", "restart_opencode", "switch_channel"}
INSPECTION_COMMANDS = {"inspect_runtime", "status"}


class FrameTransport(Protocol):
    async def send(self, message: dict[str, Any]) -> None: ...

    async def recv(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class AgentConfig:
    gateway_url: str
    worker_instance_id: str
    environment_id: int
    token: str
    agent_version: str = __version__
    ping_seconds: float = 20
    ping_timeout_seconds: float = 10
    auth_timeout_seconds: float = 10
    heartbeat_misses: int = 2
    reconnect_min_seconds: float = 1
    reconnect_max_seconds: float = 30
    max_frame_bytes: int = 262144
    command_dedupe_limit: int = 256
    command_dedupe_ttl_seconds: float = 600
    opencode_url: str = "http://127.0.0.1:14096"
    workspace_root: str = ""


@dataclass(slots=True)
class _Executed:
    state: str
    result: dict[str, Any] | None
    error_code: str | None
    expires_at: float
    error: str | None = None


class CommandLedger:
    """按 request_id 记住有限时间内的执行结果。重复投递返回上次结果或已在执行。"""

    def __init__(self, limit: int, ttl_seconds: float) -> None:
        self.limit = limit
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, _Executed] = {}
        self._order: deque[str] = deque()

    def lookup(self, request_id: str, now: float) -> _Executed | None:
        self._purge(now)
        return self._items.get(request_id)

    def begin(self, request_id: str, now: float) -> None:
        self._put(request_id, _Executed("executing", None, None, now + self.ttl_seconds))

    def finish(
        self,
        request_id: str,
        result: dict[str, Any] | None,
        error_code: str | None,
        now: float,
        error: str | None = None,
    ) -> None:
        self._put(request_id, _Executed("done", result, error_code, now + self.ttl_seconds, error))

    def _put(self, request_id: str, item: _Executed) -> None:
        if request_id not in self._items:
            self._order.append(request_id)
        self._items[request_id] = item
        self._purge(item.expires_at - self.ttl_seconds)

    def _purge(self, now: float) -> None:
        while self._order and (
            self._order[0] not in self._items or self._items[self._order[0]].expires_at <= now
        ):
            self._items.pop(self._order.popleft(), None)
        while len(self._order) > self.limit:
            self._items.pop(self._order.popleft(), None)


def reconnect_delay(attempt: int, minimum: float, maximum: float, rng: random.Random) -> float:
    step = min(maximum, minimum * (2 ** max(attempt - 1, 0)))
    if step >= maximum:
        return maximum
    return min(maximum, step + rng.uniform(0, minimum))


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _bound_opencode_port(opencode_url: str) -> int | None:
    try:
        parts = urlsplit(opencode_url)
        if parts.scheme not in {"http", "https"} or parts.hostname not in _LOOPBACK_HOSTS:
            return None
        return parts.port
    except ValueError:
        return None


def _same_workspace(requested: str, configured: str) -> bool:
    if not requested or not configured:
        return False
    try:
        return os.path.realpath(requested) == os.path.realpath(configured)
    except OSError:
        return False


def _binding_error(data: dict[str, Any], workspace_root: str, opencode_url: str) -> str | None:
    """危险动作只能操作本 Worker 自己绑定的工作区和端口。"""

    try:
        port = int(data.get("port"))
    except (TypeError, ValueError):
        return "OpenCode 端口无效"
    bound = _bound_opencode_port(opencode_url)
    if bound is None:
        return "未绑定本机 OpenCode 端口"
    if port != bound:
        return "OpenCode 端口与 Worker 绑定不一致"
    if not _same_workspace(str(data.get("workspace") or ""), workspace_root):
        return "工作区与 Worker 绑定不一致"
    return None


def handle_command(
    message: WorkerMessage,
    supervisor: RuntimeSupervisor,
    *,
    workspace_root: str = "",
    opencode_url: str = "",
    gate: Callable[[], None] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    name = message.payload.get("name")
    data = message.payload.get("data")
    if not isinstance(data, dict):
        data = {}
    if name in {"pause_events", "resume_events", "stop_events"}:
        return {"action": name, "directory": str(data.get("directory") or "")}, None
    if name == "prepare_workspace":
        from app.worker_agent.workspace import prepare_workspace_local

        if not workspace_root:
            return None, "未配置工作区根，拒绝准备目录"
        try:
            return prepare_workspace_local(
                str(data.get("root") or ""),
                str(data.get("target") or ""),
                reuse_existing=bool(data.get("reuse_existing")),
                allowed_root=workspace_root,
            ), None
        except RuntimeError as exc:
            return None, str(exc)
    if name == "switch_channel":
        from app.worker_agent.channel_switch import apply_channel_switch

        error = _binding_error(data, workspace_root, opencode_url)
        if error is not None:
            return None, error
        port = int(data["port"])
        try:
            return apply_channel_switch(
                workspace=str(data.get("workspace") or ""), port=port, gate=gate
            ), None
        except (RuntimeError, OSError, ValueError) as exc:
            return None, str(exc)
    if name == "restart_opencode":
        from app.worker_agent.channel_switch import restart_local_opencode

        error = _binding_error(data, workspace_root, opencode_url)
        if error is not None:
            return None, error
        port = int(data["port"])
        try:
            restart_local_opencode(
                str(data.get("workspace") or ""), port, gate=gate
            )
        except (RuntimeError, OSError, ValueError) as exc:
            return None, str(exc)
        return {"restarted": True}, None
    if name == "ping":
        return {"pong": True}, None
    if name == "echo":
        return {"echo": data}, None
    if name == "inspect_runtime":
        from app.worker_agent.inspection import inspect_runtime_from_payload

        try:
            result = inspect_runtime_from_payload(opencode_url, workspace_root, data)
        except ValueError:
            return None, "invalid_data"
        return result, None
    if name == "status":
        from app.worker_agent.inspection import status_summary

        return {
            **status_summary(opencode_url),
            "restart_on_disconnect": False,
            "restarts": supervisor.restart_count,
        }, None
    return None, "unsupported_command"


class WorkerConnectionManager:
    def __init__(
        self,
        config: AgentConfig,
        *,
        supervisor: RuntimeSupervisor | None = None,
        rng: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self.supervisor = supervisor or RuntimeSupervisor()
        self.rng = rng or random.Random()
        self.sleep = sleep or asyncio.sleep
        self.clock = clock or time.time
        self.epoch: int | None = None
        self.epochs: list[int] = []
        self.delays: list[float] = []
        self.ledger = CommandLedger(config.command_dedupe_limit, config.command_dedupe_ttl_seconds)
        self.http_tasks: dict[str, asyncio.Task[None]] = {}
        self.file_tasks: dict[str, asyncio.Task[None]] = {}
        self.control_tasks: dict[str, asyncio.Task[None]] = {}
        self.file_slots = asyncio.Semaphore(FILE_COMMAND_LIMIT)
        self.inspection_slots = asyncio.Semaphore(1)
        self.event_tasks: dict[str, asyncio.Task[None]] = {}
        self.closed = threading.Event()
        self.event_seq: dict[str, int] = {}
        self.paused_directories: set[str] = set()
        self.stopped_directories: set[str] = set()
        self.resume_waiters: dict[str, asyncio.Event] = {}
        self.fetch_body = None
        self.fetch_upload = None
        self.local_request = None
        self.upload_body = None

    async def run(self, connect: Connect, stop: asyncio.Event, *, max_sessions: int | None = None) -> None:
        attempt = 1
        sessions = 0
        while not stop.is_set():
            try:
                await self.run_session(await connect())
                attempt = 1
            except (ConnectionError, TimeoutError, OSError):
                self.supervisor.on_connection_lost()
            sessions += 1
            if max_sessions is not None and sessions >= max_sessions:
                return
            if stop.is_set():
                return
            delay = reconnect_delay(
                attempt,
                self.config.reconnect_min_seconds,
                self.config.reconnect_max_seconds,
                self.rng,
            )
            self.delays.append(delay)
            attempt += 1
            await self.sleep(delay)

    async def run_session(self, transport: FrameTransport) -> None:
        config = self.config
        heartbeat: asyncio.Task[None] | None = None
        heartbeat_stop = asyncio.Event()
        self.closed = threading.Event()
        try:
            auth = envelope(
                request_id=f"auth-{config.worker_instance_id}",
                worker_instance_id=config.worker_instance_id,
                environment_id=config.environment_id,
                session_id=None,
                message_type="command",
                payload={
                    "name": "authenticate",
                    "token": config.token,
                    "agent_version": config.agent_version,
                },
            )
            try:
                await asyncio.wait_for(transport.send(auth.to_dict()), config.auth_timeout_seconds)
                reply = await asyncio.wait_for(self._recv(transport), config.auth_timeout_seconds)
            except TimeoutError as exc:
                raise WorkerTransportError("auth_timeout") from exc
            if reply.type != "response" or not reply.payload.get("ok"):
                raise WorkerTransportError(str(reply.payload.get("error_code") or "auth_failed"))
            epoch = int(reply.payload["epoch"])
            self.epoch = epoch
            self.epochs.append(epoch)
            self.event_seq.clear()
            ping_seconds = float(reply.payload.get("ping_seconds") or config.ping_seconds)
            ping_timeout = float(reply.payload.get("ping_timeout_seconds") or config.ping_timeout_seconds)
            monitor = HeartbeatMonitor(config.heartbeat_misses)
            heartbeat = asyncio.create_task(
                self._heartbeat(transport, ping_seconds, ping_timeout, monitor, heartbeat_stop)
            )
            while True:
                incoming = await self._next_message(transport, heartbeat)
                if incoming.type == "heartbeat":
                    if not frame_acceptable(incoming, epoch, self.clock()):
                        continue
                    if incoming.payload.get("kind") == "pong":
                        monitor.resolve(incoming.request_id)
                        continue
                    if incoming.payload.get("kind") == "ping":
                        await transport.send(self._heartbeat_reply(incoming, "pong").to_dict())
                    continue
                if incoming.type != "command":
                    continue
                name = incoming.payload.get("name")
                if name == "http":
                    self._start_http(transport, incoming, epoch)
                    continue
                if name in FILE_COMMANDS:
                    self._start_file(transport, incoming, epoch)
                    continue
                if name in CONTROL_COMMANDS:
                    self._start_control(transport, incoming, epoch)
                    continue
                if name == "subscribe_events":
                    self._start_events(transport, incoming, epoch)
                    continue
                if name == "cancel":
                    target = str((incoming.payload.get("data") or {}).get("target_request_id") or "")
                    running = self.http_tasks.get(target)
                    if running is not None:
                        running.cancel()
                await self._handle_command(transport, incoming, epoch)
        finally:
            self.closed.set()
            for task in list(self.http_tasks.values()):
                task.cancel()
            for task in list(self.event_tasks.values()):
                task.cancel()
            for task in list(self.file_tasks.values()):
                task.cancel()
            for task in list(self.control_tasks.values()):
                task.cancel()
            pending = [
                *self.http_tasks.values(),
                *self.event_tasks.values(),
                *self.file_tasks.values(),
                *self.control_tasks.values(),
            ]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self.control_tasks.clear()
            heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            try:
                await transport.close()
            except Exception:
                pass

    async def _next_message(self, transport: FrameTransport, heartbeat: asyncio.Task[None]) -> WorkerMessage:
        recv_task = asyncio.create_task(self._recv(transport))
        try:
            done, _pending = await asyncio.wait(
                {recv_task, heartbeat}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            recv_task.cancel()
            await asyncio.gather(recv_task, return_exceptions=True)
            raise
        if heartbeat in done:
            recv_task.cancel()
            await asyncio.gather(recv_task, return_exceptions=True)
            error = heartbeat.exception()
            if isinstance(error, (ConnectionError, TimeoutError, OSError)):
                raise error
            if error is not None:
                raise WorkerTransportError(error.__class__.__name__) from error
            raise WorkerTransportError("heartbeat_stopped")
        return recv_task.result()

    def _start_http(self, transport: FrameTransport, incoming: WorkerMessage, epoch: int) -> None:
        if not frame_acceptable(incoming, epoch, self.clock()):
            return
        current = self.http_tasks.get(incoming.request_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._execute_http(transport, incoming, epoch))
        self.http_tasks[incoming.request_id] = task

    async def _execute_http(self, transport: FrameTransport, incoming: WorkerMessage, epoch: int) -> None:
        try:
            if not frame_acceptable(incoming, epoch, self.clock()):
                return
            await transport.send(self._response(incoming, "ack", None, None).to_dict())
            if self.epoch != epoch:
                return
            data = incoming.payload.get("data") if isinstance(incoming.payload.get("data"), dict) else {}
            if self.fetch_body is None or self.local_request is None or self.upload_body is None:
                return
            body = await self.fetch_body(incoming.request_id, epoch)
            if self.epoch != epoch:
                return
            result = await self.local_request(
                str(data.get("method") or "GET"),
                str(data.get("path") or "/"),
                body,
            )
            if self.epoch != epoch:
                return
            await self.upload_body(incoming.request_id, epoch, result)
        except asyncio.CancelledError:
            return
        finally:
            task = asyncio.current_task()
            if self.http_tasks.get(incoming.request_id) is task:
                self.http_tasks.pop(incoming.request_id, None)

    def _start_control(
        self, transport: FrameTransport, incoming: WorkerMessage, epoch: int
    ) -> None:
        """Dedup before queueing; control probes never block the receive loop."""

        if not frame_acceptable(incoming, epoch, self.clock()):
            return
        now = self.clock()
        recorded = self.ledger.lookup(incoming.request_id, now)
        if recorded is not None and recorded.state == "executing":
            asyncio.create_task(transport.send(self._duplicate(incoming).to_dict()))
            return
        if recorded is not None and recorded.state == "done":
            asyncio.create_task(
                transport.send(
                    self._response(
                        incoming,
                        "result",
                        recorded.result,
                        recorded.error_code,
                        error=recorded.error,
                    ).to_dict()
                )
            )
            return
        current = self.control_tasks.get(incoming.request_id)
        if current is not None and not current.done():
            return
        self.ledger.begin(incoming.request_id, now)
        task = asyncio.create_task(
            self._execute_control(transport, incoming, epoch, closed=self.closed)
        )
        self.control_tasks[incoming.request_id] = task

    def _control_gate(
        self, epoch: int, deadline: int | None, closed: threading.Event
    ) -> Callable[[], None]:
        """危险动作每次副作用前检查当前 epoch、连接状态和命令有效期。"""

        def gate() -> None:
            if closed.is_set() or self.epoch != epoch:
                raise RuntimeError("控制命令已失效")
            if deadline is None or int(self.clock()) >= int(deadline):
                raise RuntimeError("控制命令已过期")

        return gate

    async def _execute_control(
        self,
        transport: FrameTransport,
        incoming: WorkerMessage,
        epoch: int,
        *,
        closed: threading.Event,
    ) -> None:
        name = str(incoming.payload.get("name") or "")
        gate = self._control_gate(epoch, incoming.expires_at, closed)
        try:
            if not frame_acceptable(incoming, epoch, self.clock()):
                self.ledger.finish(incoming.request_id, None, "epoch_conflict", self.clock())
                return
            await transport.send(self._response(incoming, "ack", None, None).to_dict())
            if closed.is_set() or self.epoch != epoch:
                self.ledger.finish(incoming.request_id, None, "epoch_conflict", self.clock())
                return
            if name in INSPECTION_COMMANDS:
                async with self.inspection_slots:
                    result, error_code = await asyncio.to_thread(
                        handle_command,
                        incoming,
                        self.supervisor,
                        workspace_root=self.config.workspace_root,
                        opencode_url=self.config.opencode_url,
                        gate=gate,
                    )
            else:
                result, error_code = await asyncio.to_thread(
                    handle_command,
                    incoming,
                    self.supervisor,
                    workspace_root=self.config.workspace_root,
                    opencode_url=self.config.opencode_url,
                    gate=gate,
                )
            if closed.is_set() or self.epoch != epoch:
                self.ledger.finish(incoming.request_id, None, "epoch_conflict", self.clock())
                return
            self._note_event_control(name, result)
            self.ledger.finish(incoming.request_id, result, error_code, self.clock())
            await transport.send(
                self._response(incoming, "result", result, error_code).to_dict()
            )
        except asyncio.CancelledError:
            return
        except (ConnectionError, OSError):
            return
        except Exception as exc:
            self.ledger.finish(incoming.request_id, None, "io", self.clock(), error=str(exc))
            try:
                await transport.send(
                    self._response(
                        incoming, "result", None, "io", error="控制命令执行失败"
                    ).to_dict()
                )
            except (ConnectionError, OSError):
                return
        finally:
            task = asyncio.current_task()
            if self.control_tasks.get(incoming.request_id) is task:
                self.control_tasks.pop(incoming.request_id, None)

    def _start_file(self, transport: FrameTransport, incoming: WorkerMessage, epoch: int) -> None:
        if not frame_acceptable(incoming, epoch, self.clock()):
            return
        current = self.file_tasks.get(incoming.request_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._execute_file(transport, incoming, epoch))
        self.file_tasks[incoming.request_id] = task

    async def _execute_file(self, transport: FrameTransport, incoming: WorkerMessage, epoch: int) -> None:
        """File work stays off the read loop and off the OpenCode http slots."""

        try:
            if not frame_acceptable(incoming, epoch, self.clock()):
                return
            recorded = self.ledger.lookup(incoming.request_id, self.clock())
            if recorded is not None and recorded.state == "executing":
                await transport.send(self._duplicate(incoming).to_dict())
                return
            if recorded is not None and recorded.state == "done":
                await transport.send(
                    self._response(
                        incoming, "result", recorded.result, recorded.error_code, error=recorded.error
                    ).to_dict()
                )
                return
            await transport.send(self._response(incoming, "ack", None, None).to_dict())
            self.ledger.begin(incoming.request_id, self.clock())
            async with self.file_slots:
                if self.closed.is_set() or self.epoch != epoch:
                    raise FileCommandError("epoch_conflict", "epoch 已切换")
                name = str(incoming.payload.get("name") or "")
                content = None
                if name == "upload_file":
                    if self.fetch_upload is None:
                        raise FileCommandError("io", "上传正文不存在")
                    content = await self.fetch_upload(incoming.request_id, epoch)
                closed = self.closed
                result, body = await asyncio.to_thread(
                    execute_file_command,
                    incoming.payload,
                    allowed_root=self.config.workspace_root,
                    request_id=incoming.request_id,
                    deadline=float(incoming.expires_at or 0),
                    content=content,
                    gate=connection_gate(closed, lambda: self.epoch == epoch),
                )
            if body:
                if self.upload_body is None:
                    raise FileCommandError("io", "上传通道不可用")
                encoded = base64.b64encode(body).decode("ascii")
                await self.upload_body(
                    incoming.request_id,
                    epoch,
                    {"status": 200, "body": encoded, "headers": {}, "result": result},
                )
            else:
                await transport.send(self._response(incoming, "result", result, None).to_dict())
            self.ledger.finish(incoming.request_id, result, None, self.clock())
        except FileCommandError as exc:
            self.ledger.finish(incoming.request_id, None, exc.code, self.clock(), error=str(exc))
            try:
                await transport.send(
                    self._response(incoming, "result", None, exc.code, error=str(exc)).to_dict()
                )
            except (ConnectionError, OSError):
                return
        except asyncio.CancelledError:
            return
        except (ConnectionError, OSError):
            return
        except Exception as exc:
            self.ledger.finish(incoming.request_id, None, "io", self.clock())
            try:
                await transport.send(
                    self._response(incoming, "result", None, "io", error=str(exc) or "打开路径失败").to_dict()
                )
            except (ConnectionError, OSError):
                return
        finally:
            task = asyncio.current_task()
            if self.file_tasks.get(incoming.request_id) is task:
                self.file_tasks.pop(incoming.request_id, None)

    def _start_events(self, transport: FrameTransport, incoming: WorkerMessage, epoch: int) -> None:
        if not frame_acceptable(incoming, epoch, self.clock()):
            return
        data = incoming.payload.get("data") if isinstance(incoming.payload.get("data"), dict) else {}
        directory = str(data.get("directory") or "")
        current = self.event_tasks.get(directory)
        if current is not None and not current.done():
            asyncio.create_task(self._ack_existing_subscription(transport, incoming))
            return
        task = asyncio.create_task(self._stream_events(transport, incoming, epoch, directory))
        if directory:
            self.event_tasks[directory] = task

    async def _ack_existing_subscription(self, transport: FrameTransport, incoming: WorkerMessage) -> None:
        await transport.send(self._response(incoming, "ack", None, None).to_dict())
        await transport.send(
            self._response(incoming, "result", {"subscribed": True, "reused": True}, None).to_dict()
        )

    async def _stream_events(
        self, transport: FrameTransport, incoming: WorkerMessage, epoch: int, directory: str
    ) -> None:
        try:
            await transport.send(self._response(incoming, "ack", None, None).to_dict())
            if self.epoch != epoch or not directory:
                await transport.send(
                    self._response(incoming, "result", None, "unsupported_command").to_dict()
                )
                return
            await transport.send(
                self._response(
                    incoming, "result", {"subscribed": True, "directory": directory}, None
                ).to_dict()
            )
            url = self.config.opencode_url.rstrip("/") + "/event"
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", url, params={"directory": directory}) as response:
                    response.raise_for_status()
                    lines = response.aiter_lines()
                    while True:
                        while directory in self.paused_directories:
                            if directory in self.stopped_directories or self.epoch != epoch:
                                return
                            waiter = self.resume_waiters.setdefault(directory, asyncio.Event())
                            try:
                                await asyncio.wait_for(waiter.wait(), 1)
                            except TimeoutError:
                                continue
                        if directory in self.stopped_directories or self.epoch != epoch:
                            return
                        try:
                            line = await lines.__anext__()
                        except StopAsyncIteration:
                            return
                        if not line.startswith("data: "):
                            continue
                        seq = self.event_seq.get(directory, 0) + 1
                        self.event_seq[directory] = seq
                        try:
                            event = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue
                        await transport.send(
                            _bounded_event_frame(
                                incoming,
                                self.config,
                                directory=directory,
                                seq=seq,
                                event=event if isinstance(event, dict) else {"raw": event},
                                epoch=epoch,
                                expires_at=int(self.clock()) + 30,
                            )
                        )
        except asyncio.CancelledError:
            return
        finally:
            if self.event_tasks.get(directory) is asyncio.current_task():
                self.event_tasks.pop(directory, None)

    async def _handle_command(self, transport: FrameTransport, incoming: WorkerMessage, epoch: int) -> None:
        if not frame_acceptable(incoming, epoch, self.clock()):
            return
        now = self.clock()
        recorded = self.ledger.lookup(incoming.request_id, now)
        if recorded is not None and recorded.state == "executing":
            await transport.send(self._duplicate(incoming).to_dict())
            return
        if recorded is not None and recorded.state == "done":
            await transport.send(
                self._response(incoming, "result", recorded.result, recorded.error_code).to_dict()
            )
            return
        await transport.send(self._response(incoming, "ack", None, None).to_dict())
        self.ledger.begin(incoming.request_id, now)
        result, error_code = handle_command(
            incoming,
            self.supervisor,
            workspace_root=self.config.workspace_root,
            opencode_url=self.config.opencode_url,
        )
        self._note_event_control(incoming.payload.get("name"), result)
        self.ledger.finish(incoming.request_id, result, error_code, self.clock())
        await transport.send(self._response(incoming, "result", result, error_code).to_dict())

    def _note_event_control(self, name: Any, result: dict[str, Any] | None) -> None:
        if name not in {"pause_events", "resume_events", "stop_events"} or not result:
            return
        directory = str(result.get("directory") or "")
        if name == "pause_events":
            self.paused_directories.add(directory)
            self.stopped_directories.discard(directory)
            waiter = self.resume_waiters.get(directory)
            if waiter is not None:
                waiter.clear()
            return
        if name == "resume_events":
            self.paused_directories.discard(directory)
            self.resume_waiters.setdefault(directory, asyncio.Event()).set()
            return
        self.stopped_directories.add(directory)
        self.paused_directories.discard(directory)
        running = self.event_tasks.pop(directory, None)
        if running is not None:
            running.cancel()

    async def _recv(self, transport: FrameTransport) -> WorkerMessage:
        try:
            return WorkerMessage(**_message_fields(await transport.recv()))
        except WorkerGatewayError as exc:
            raise WorkerTransportError(str(exc)) from exc

    async def _heartbeat(
        self,
        transport: FrameTransport,
        interval: float,
        timeout: float,
        monitor: HeartbeatMonitor,
        stop: asyncio.Event,
    ) -> None:
        count = 0
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), interval)
                return
            except TimeoutError:
                pass
            if stop.is_set():
                return
            count += 1
            request_id = f"hb-{count}"
            event = monitor.track(request_id)
            await transport.send(self._agent_ping(request_id).to_dict())
            if not await self._wait_pong(event, timeout, stop) and monitor.timeout(request_id):
                raise WorkerTransportError("ping_timeout")

    async def _wait_pong(self, event: asyncio.Event, timeout: float, stop: asyncio.Event) -> bool:
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            return False
        return not stop.is_set()

    def _agent_ping(self, request_id: str) -> WorkerMessage:
        message = envelope(
            request_id=request_id,
            worker_instance_id=self.config.worker_instance_id,
            environment_id=self.config.environment_id,
            session_id=None,
            message_type="heartbeat",
            payload={"kind": "ping"},
        )
        message.epoch = self.epoch
        message.expires_at = int(self.clock()) + int(self.config.ping_timeout_seconds) + 1
        return message

    def _heartbeat_reply(self, incoming: WorkerMessage, kind: str) -> WorkerMessage:
        message = envelope(
            request_id=incoming.request_id,
            worker_instance_id=self.config.worker_instance_id,
            environment_id=self.config.environment_id,
            session_id=incoming.session_id,
            message_type="heartbeat",
            payload={"kind": kind},
        )
        message.epoch = self.epoch
        message.expires_at = incoming.expires_at
        return message

    def _duplicate(self, command: WorkerMessage) -> WorkerMessage:
        return envelope(
            request_id=command.request_id,
            worker_instance_id=self.config.worker_instance_id,
            environment_id=self.config.environment_id,
            session_id=command.session_id,
            message_type="response",
            payload={
                "stage": "duplicate",
                "ok": True,
                "result": {"state": "already_executing"},
                "error_code": None,
            },
        )

    def _response(
        self,
        command: WorkerMessage,
        stage: str,
        result: dict[str, Any] | None,
        error_code: str | None,
        error: str | None = None,
    ) -> WorkerMessage:
        payload: dict[str, Any] = {"stage": stage, "ok": error_code is None}
        if stage == "result":
            payload["result"] = result
            payload["error_code"] = error_code
            if error:
                payload["error"] = error
        return envelope(
            request_id=command.request_id,
            worker_instance_id=self.config.worker_instance_id,
            environment_id=self.config.environment_id,
            session_id=command.session_id,
            message_type="response",
            payload=payload,
        )


def _message_fields(data: dict[str, Any]) -> dict[str, Any]:
    message = parse_message(dump_message(data), max_bytes=1024 * 1024)
    return {
        "request_id": message.request_id,
        "worker_instance_id": message.worker_instance_id,
        "environment_id": message.environment_id,
        "session_id": message.session_id,
        "type": message.type,
        "payload": message.payload,
        "epoch": message.epoch,
        "expires_at": message.expires_at,
    }


def load_agent_config(environ: dict[str, str]) -> AgentConfig:
    token = environ.get("NEXUS_WORKER_TOKEN", "").strip()
    if not token:
        raise SystemExit("缺少 NEXUS_WORKER_TOKEN")
    url = environ.get("NEXUS_WORKER_GATEWAY_URL", "").strip()
    worker_id = environ.get("NEXUS_WORKER_INSTANCE_ID", "").strip()
    environment = environ.get("NEXUS_WORKER_ENVIRONMENT_ID", "").strip()
    if not url or not worker_id or not environment:
        raise SystemExit("缺少 Worker Gateway 地址、实例 ID 或环境 ID")
    return AgentConfig(
        gateway_url=url,
        worker_instance_id=worker_id,
        environment_id=int(environment),
        token=token,
        agent_version=__version__,
        opencode_url=environ.get("NEXUS_WORKER_OPENCODE_URL", "http://127.0.0.1:14096"),
        workspace_root=environ.get("NEXUS_WORKER_WORKSPACE_ROOT", "").strip(),
    )


def event_request_id(directory: str, seq: int) -> str:
    """Stable short id. The directory stays in the payload, never in the id."""

    digest = hashlib.sha256(f"{directory}\0{seq}".encode()).hexdigest()[:32]
    return f"evt-{digest}"


def _bounded_event_frame(
    incoming: WorkerMessage,
    config: AgentConfig,
    *,
    directory: str,
    seq: int,
    event: dict[str, Any],
    epoch: int,
    expires_at: int,
) -> dict[str, Any]:
    """Judge the encapsulated frame. An oversized event becomes a gap marker."""

    payload: dict[str, Any] = {"seq": seq, "directory": directory, "event": event}
    message = _event_envelope(incoming, config, directory, seq, payload, epoch, expires_at)
    if len(dump_message(message).encode("utf-8")) <= config.max_frame_bytes:
        return message
    payload = {
        "seq": seq,
        "directory": directory,
        "dropped": True,
        "reason": "event_too_large",
        "event_type": event.get("type"),
    }
    return _event_envelope(incoming, config, directory, seq, payload, epoch, expires_at)


def _event_envelope(
    incoming: WorkerMessage,
    config: AgentConfig,
    directory: str,
    seq: int,
    payload: dict[str, Any],
    epoch: int,
    expires_at: int,
) -> dict[str, Any]:
    message = envelope(
        request_id=event_request_id(directory, seq),
        worker_instance_id=config.worker_instance_id,
        environment_id=config.environment_id,
        session_id=incoming.session_id,
        message_type="event",
        payload=payload,
    )
    message.epoch = epoch
    message.expires_at = expires_at
    return message.to_dict()
