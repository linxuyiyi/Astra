"""现有 Worker Gateway 的引擎无关命令传输。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.config.settings import get_settings
from app.database import SessionLocal
from app.executors.errors import OpenCodeServerUnavailable
from app.models import AgentWorker
from app.worker_gateway.protocol import WorkerGatewayError
from app.worker_gateway.service import get_gateway


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


class WorkerCommandTransport:
    def __init__(self, *, agent_worker_id: str, gateway=None, timeout_seconds: float = 60):
        self.agent_worker_id = agent_worker_id
        self.gateway = gateway or get_gateway()
        self._timeout_seconds = timeout_seconds
        self.settings = get_settings()

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
            raise WorkerCommandError(exc.code, str(exc)) from exc
        if failed:
            raise WorkerCommandError(failed)
        if not delivered:
            raise WorkerCommandError("not_delivered", "Worker 没有接收这条指令")
        frame = await self.gateway.bus.wait_response(request_id, self._timeout_seconds)
        if frame is None:
            raise TimeoutError("Worker 未在时限内返回指令结果")
        return command_result(frame)

    async def _dispatch(
        self, request_id: str, name: str, payload: dict[str, Any], session_id: str | None
    ) -> tuple[str, int, bool, str | None]:
        async with SessionLocal() as session:
            instance = await self._instance(session)
            result = await self.gateway.dispatch(
                session,
                agent_worker_id=instance.id,
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

    async def _instance(self, session) -> AgentWorker:
        instance = await session.scalar(select(AgentWorker).where(AgentWorker.id == self.agent_worker_id))
        if instance is None or instance.status not in {"IDLE", "WORKING", "PAUSED"}:
            raise OpenCodeServerUnavailable("Worker 不在线")
        return instance
