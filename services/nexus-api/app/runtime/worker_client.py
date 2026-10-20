"""OpenCode calls for an environment marked worker. Bodies move over HTTP."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.config.settings import Settings, get_settings
from app.database import SessionLocal
from app.executors.errors import (
    OpenCodeExecutionError,
    OpenCodeExecutionTimeout,
    OpenCodeServerUnavailable,
    OpenCodeSessionNotFound,
)
from app.executors.opencode_server import (
    OpenCodeServerCapabilityError,
    OpenCodeServerClient,
    WorkspacePath,
)
from app.models import WorkerInstance
from app.worker_gateway.events import EventLog
from app.worker_gateway.protocol import WorkerGatewayError
from app.worker_gateway.service import get_gateway

_CURSOR_HEADERS = ("x-next-cursor", "link", "x-has-more", "content-type")


class WorkerCommandError(RuntimeError):
    """A Worker command failed. ``str(exc)`` is the message callers already match."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


def command_result(frame: dict[str, Any]) -> dict[str, Any]:
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else frame
    error_code = payload.get("error_code")
    if error_code:
        message = payload.get("error") or str(error_code)
        raise WorkerCommandError(str(error_code), str(message))
    result = payload.get("result")
    if not isinstance(result, dict):
        result = {"value": result}
    if "payload" not in frame and isinstance(frame.get("body"), str):
        result = {**result, "body": frame["body"]}
    return result


class WorkerOpenCodeClient(OpenCodeServerClient):
    """Same method names as the direct client. The socket only carries the ready notice."""

    records_fetch = True

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        environment_id: int,
        gateway=None,
        timeout_seconds: int | None = None,
    ):
        super().__init__(
            settings or get_settings(),
            base_url="http://127.0.0.1",
            timeout_seconds=timeout_seconds,
        )
        self.environment_id = environment_id
        self.gateway = gateway or get_gateway()
        self.offline_grace_seconds = float(self.settings.worker_event_offline_grace_seconds)

    async def exchange(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        command_request_id: str | None = None,
        attempt_ref: dict[str, Any] | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        payload: dict[str, Any] = {
            "method": method,
            "path": path,
            "query": params or {},
            "body": body if body is not None else {},
        }
        if task_id:
            payload["task_id"] = task_id
        if attempt_ref:
            payload["attempt_ref"] = attempt_ref
        request_id = command_request_id or f"http_{uuid4().hex}"
        try:
            worker_id, epoch, delivered, failed = await self._dispatch(
                request_id, "http", payload, session_id
            )
        except WorkerGatewayError as exc:
            raise OpenCodeServerUnavailable(str(exc)) from exc
        if failed:
            raise OpenCodeServerUnavailable(failed)
        if not delivered:
            raise OpenCodeServerUnavailable("Worker 没有接收这次调用")
        frame = await self.gateway.bus.wait_response(request_id, self._timeout_seconds)
        if frame is None:
            raise OpenCodeExecutionTimeout("Worker 未在时限内上传响应")
        meta = frame.get("result") if isinstance(frame.get("result"), dict) else {}
        raw = frame.get("body")
        encoded = raw.encode("utf-8") if isinstance(raw, str) else b""
        headers = {
            key.lower(): value
            for key, value in (meta.get("headers") or {}).items()
            if key.lower() in _CURSOR_HEADERS
        }
        if meta.get("content_type") and "content-type" not in headers:
            headers["content-type"] = str(meta["content_type"])
        return int(meta.get("http_status") or 502), encoded, headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        directory: WorkspacePath | None = None,
        body: Any = None,
        query: dict[str, Any] | None = None,
        include_headers: bool = False,
        command_request_id: str | None = None,
        attempt_ref: dict[str, Any] | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> Any:
        params = dict(query or {})
        if directory:
            params["directory"] = str(directory)
        status, content, headers = await self.exchange(
            method,
            path,
            params=params,
            body=body,
            command_request_id=command_request_id,
            attempt_ref=attempt_ref,
            session_id=session_id,
            task_id=task_id,
        )
        if status == 404 and path.startswith("/session/") and path != "/session/status":
            raise OpenCodeSessionNotFound(path.split("/")[2])
        if status >= 500:
            raise OpenCodeServerUnavailable(f"OpenCode HTTP {status}")
        if status >= 400:
            raise OpenCodeExecutionError(f"OpenCode HTTP {status}: {content[:1000]!r}")
        payload = json.loads(content) if content else None
        return (payload, headers) if include_headers else payload

    async def run_command(
        self,
        name: str,
        data: dict[str, Any],
        *,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = request_id or f"cmd_{uuid4().hex}"
        try:
            _worker, _epoch, delivered, failed = await self._dispatch(request_id, name, data, session_id)
        except WorkerGatewayError as exc:
            raise RuntimeError(str(exc)) from exc
        if failed:
            raise RuntimeError(failed)
        if not delivered:
            raise RuntimeError("Worker 没有接收这条指令")
        frame = await self.gateway.bus.wait_response(request_id, self._timeout_seconds)
        if frame is None:
            raise TimeoutError("Worker 未在时限内返回指令结果")
        return command_result(frame)

    async def health(self) -> dict[str, Any]:
        try:
            payload = await self.request("GET", "/global/health")
        except OpenCodeExecutionError as exc:
            raise OpenCodeServerUnavailable(f"OpenCode Server 不可用: {exc}") from exc
        if not isinstance(payload, dict):
            raise OpenCodeServerUnavailable("OpenCode Server 不可用: 健康检查没有返回对象")
        version = str(payload.get("version") or "") or None
        version_error = self.manager.version_error(version)
        if version_error:
            raise OpenCodeServerCapabilityError(version_error)
        if payload.get("healthy") is not True:
            raise OpenCodeServerCapabilityError(
                f"OpenCode Server 健康检查失败: {json.dumps(payload, ensure_ascii=False)}"
            )
        return {"healthy": True, "version": version}

    async def events(
        self, directory: WorkspacePath, *, consumer: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        folder = str(directory)
        worker_id, epoch = await self._online()
        log = self._event_log()
        holder = consumer or f"browser:{uuid4().hex}"
        # A live cursor must exist before the first frame, or control() treats
        # the directory as unused and deletes the log.
        await log.touch_cursor(
            worker_id=worker_id, epoch=epoch, directory=folder, consumer=holder, seq=0
        )
        await self.run_command("subscribe_events", {"directory": folder})
        seen = 0
        offline_deadline: float | None = None
        while True:
            try:
                current_id, current_epoch = await self._online()
            except OpenCodeServerUnavailable:
                now = time.monotonic()
                if offline_deadline is None:
                    offline_deadline = now + self.offline_grace_seconds
                if now >= offline_deadline:
                    yield {
                        "type": "astra.event_gap",
                        "directory": folder,
                        "reason": "worker_offline",
                        "from": seen,
                        "to": None,
                    }
                    return
                yield {"type": "astra.keepalive", "directory": folder}
                await asyncio.sleep(min(0.5, max(0.0, offline_deadline - now)))
                continue
            offline_deadline = None
            if current_id != worker_id or current_epoch != epoch:
                previous = seen
                worker_id, epoch = current_id, current_epoch
                seen = 0
                yield {
                    "type": "astra.event_gap",
                    "directory": folder,
                    "reason": "worker_reconnected",
                    "from": previous,
                    "to": None,
                }
                await log.touch_cursor(
                    worker_id=worker_id, epoch=epoch, directory=folder, consumer=holder, seq=0
                )
                await self.run_command("subscribe_events", {"directory": folder})
                continue
            state = await log._load(log._key(worker_id, epoch, folder))
            delivered = False
            for item in state.get("events") or []:
                seq = int(item.get("seq") or 0)
                if seq <= seen:
                    continue
                seen = seq
                delivered = True
                await log.touch_cursor(
                    worker_id=worker_id, epoch=epoch, directory=folder, consumer=holder, seq=seq
                )
                if item.get("gap"):
                    gap = {
                        "type": "astra.event_gap",
                        "directory": folder,
                        "from": item.get("from"),
                        "to": item.get("to"),
                    }
                    if item.get("reason"):
                        gap["reason"] = item["reason"]
                    yield gap
                elif isinstance(item.get("event"), dict):
                    yield item["event"]
            if not delivered:
                await log.touch_cursor(
                    worker_id=worker_id, epoch=epoch, directory=folder, consumer=holder, seq=seen
                )
            action = await log.control(worker_id=worker_id, epoch=epoch, directory=folder)
            if action == "resume_events":
                await self.run_command("resume_events", {"directory": folder})
            if not delivered:
                await log.wait_append(worker_id=worker_id, epoch=epoch, directory=folder)

    def _event_log(self) -> EventLog:
        return EventLog(self.gateway.bus, limit=self.gateway.settings.worker_gateway_event_queue_size)

    async def claim_gap_refresh(self, directory: WorkspacePath) -> bool:
        worker_id, epoch = await self._online()
        return await self._event_log().begin_refresh(
            worker_id=worker_id, epoch=epoch, directory=str(directory)
        )

    async def finish_gap_refresh(self, directory: WorkspacePath) -> bool:
        worker_id, epoch = await self._online()
        return await self._event_log().finish_refresh(
            worker_id=worker_id, epoch=epoch, directory=str(directory)
        )

    async def _dispatch(
        self, request_id: str, name: str, payload: dict[str, Any], session_id: str | None
    ) -> tuple[str, int, bool, str | None]:
        async with SessionLocal() as session:
            instance = await self._instance(session)
            result = await self.gateway.dispatch(
                session,
                worker_instance_id=instance.id,
                request_id=request_id,
                name=name,
                payload=payload,
                session_id=session_id,
                timeout_seconds=self._timeout_seconds,
            )
            if result.command.status == "failed":
                return instance.id, instance.epoch, False, result.error_code or "offline"
            waiting = result.delivered or result.command.status in {"pending", "accepted"}
            return instance.id, result.command.epoch, waiting, None

    async def _online(self) -> tuple[str, int]:
        async with SessionLocal() as session:
            instance = await self._instance(session)
            return instance.id, instance.epoch

    async def _instance(self, session) -> WorkerInstance:
        instance = await session.scalar(
            select(WorkerInstance).where(WorkerInstance.environment_id == self.environment_id)
        )
        if instance is None or instance.status != "online":
            raise OpenCodeServerUnavailable("Worker 不在线")
        return instance
