"""HTTP bodies for Worker commands. WebSocket frames only say a request is ready."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentWorker, Task, WorkerCommand, utcnow
from app.worker_gateway.http_limit import release_http_slot
from app.worker_gateway.protocol import MAX_CHUNK_BYTES, WorkerGatewayError

MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILE_TEXT_BYTES = ((MAX_FILE_BYTES + 2) // 3) * 4
FILE_RESULT_COMMANDS = frozenset({"read_file", "git_readonly"})
_READY = "ready"


@dataclass(slots=True)
class RawBody:
    """upload_file request bytes. The http JSON shape stays on the other command."""

    content: bytes
    sha256: str


def request_key(request_id: str) -> str:
    return f"body-request:{request_id}"


def response_key(request_id: str, upload_id: str) -> str:
    return f"body-response:{request_id}:{upload_id}"


def http_frame_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = {key: value for key, value in payload.items() if key != "body"}
    data[_READY] = True
    return data


def encoded_body(payload: dict[str, Any]) -> bytes:
    raw = payload.get("body")
    if isinstance(raw, str):
        encoded = raw.encode("utf-8")
    else:
        encoded = json.dumps(raw if raw is not None else {}, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_BODY_BYTES:
        raise WorkerGatewayError("body_too_large", "请求体超过 8 MiB")
    return encoded


def side_effect_http(name: str, payload: dict[str, Any] | None) -> bool:
    if name != "http":
        return False
    method = str((payload or {}).get("method") or "GET").upper()
    return method not in {"GET", "HEAD"}


class DataChannel:
    def __init__(self, gateway) -> None:
        self.gateway = gateway

    async def stage_request(self, command: WorkerCommand) -> None:
        if command.name != "http":
            return
        body = encoded_body(command.payload or {})
        deadline = command.deadline_at or utcnow()
        margin = self.gateway.settings.worker_gateway_dispatch_lease_seconds
        ttl = max(1.0, (deadline - utcnow()).total_seconds() + margin)
        await self.gateway.bus.put_text(request_key(command.request_id), body.decode("utf-8"), ttl)

    async def stage_upload(self, request_id: str, content: bytes, ttl: float) -> None:
        """Store upload bytes before the command is handed to the Worker."""

        if len(content) > MAX_FILE_BYTES:
            raise WorkerGatewayError("body_too_large", "文件不能超过 20 MB")
        text = base64.b64encode(content).decode("ascii")
        await self.gateway.bus.put_text(request_key(request_id), text, ttl)

    async def fetch_request(
        self,
        session: AsyncSession,
        *,
        request_id: str,
        agent_worker_id: str,
        epoch: int,
    ) -> dict[str, Any] | RawBody:
        command, instance = await self._current_command(
            session, request_id=request_id, agent_worker_id=agent_worker_id, epoch=epoch
        )
        if command.status not in {"pending", "accepted"}:
            raise WorkerGatewayError("epoch_conflict", "指令已结束")
        if command.epoch != instance.epoch or command.epoch != epoch:
            raise WorkerGatewayError("epoch_conflict", "epoch 已切换")
        now = utcnow()
        if command.deadline_at is not None and command.deadline_at <= now:
            raise WorkerGatewayError("epoch_conflict", "指令已过期")
        upload = command.name == "upload_file"
        stored = await self.gateway.bus.get_text(request_key(request_id))
        raw_upload: bytes | None = None
        if upload:
            if stored is None:
                raise WorkerGatewayError("unknown_command", "上传正文不存在")
            try:
                raw_upload = base64.b64decode(stored, validate=True)
            except ValueError as exc:
                raise WorkerGatewayError("invalid_message", "上传正文无效") from exc
        elif stored is None:
            stored = encoded_body(command.payload or {}).decode("utf-8")
        now = utcnow()
        await self._require_current_attempt(session, command)
        current_instance = (
            select(AgentWorker.id)
            .where(AgentWorker.id == agent_worker_id, AgentWorker.epoch == epoch)
            .scalar_subquery()
        )
        result = await session.execute(
            update(WorkerCommand)
            .where(
                WorkerCommand.id == command.id,
                WorkerCommand.epoch == epoch,
                WorkerCommand.status.in_(("pending", "accepted")),
                WorkerCommand.agent_worker_id == current_instance,
                or_(WorkerCommand.deadline_at.is_(None), WorkerCommand.deadline_at > now),
            )
            .values(body_fetched_at=func.coalesce(WorkerCommand.body_fetched_at, now), updated_at=now)
        )
        if result.rowcount != 1:
            await session.rollback()
            raise WorkerGatewayError("epoch_conflict", "指令已结束")
        await session.commit()
        await session.refresh(command)
        if upload:
            if raw_upload is None:
                raise WorkerGatewayError("unknown_command", "上传正文不存在")
            return RawBody(content=raw_upload, sha256=hashlib.sha256(raw_upload).hexdigest())
        payload = command.payload or {}
        return {
            "request_id": request_id,
            "method": payload.get("method") or "GET",
            "path": payload.get("path") or "/",
            "query": payload.get("query") or {},
            "body": stored,
            "epoch": command.epoch,
        }

    async def commit_upload(
        self,
        session: AsyncSession,
        *,
        request_id: str,
        agent_worker_id: str,
        epoch: int,
        upload_id: str,
        http_status: int,
        headers: dict[str, str],
        content_type: str | None,
        chunk: str,
        complete: bool,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if len(chunk.encode("utf-8")) > MAX_CHUNK_BYTES:
            raise WorkerGatewayError("response_too_large", "分块超过 256 KiB")
        try:
            command, instance = await self._current_command(
                session, request_id=request_id, agent_worker_id=agent_worker_id, epoch=epoch
            )
        except WorkerGatewayError:
            await self.gateway.bus.delete_text(response_key(request_id, upload_id))
            raise
        if command.epoch != instance.epoch or epoch != instance.epoch:
            await self.gateway.bus.delete_text(response_key(request_id, upload_id))
            raise WorkerGatewayError("epoch_conflict", "epoch 已切换")
        staged = await self._read_stage(request_id, upload_id)
        staged["chunks"].append(chunk)
        staged["http_status"] = http_status
        staged["headers"] = headers
        staged["content_type"] = content_type
        raw = "".join(staged["chunks"])
        file_result = command.name in FILE_RESULT_COMMANDS
        limit = MAX_FILE_TEXT_BYTES if file_result else MAX_BODY_BYTES
        if len(raw.encode("utf-8")) > limit:
            await self.gateway.bus.delete_text(response_key(request_id, upload_id))
            message = "响应体超过 20 MiB" if file_result else "响应体超过 8 MiB"
            raise WorkerGatewayError("response_too_large", message)
        ttl = self.gateway.settings.worker_gateway_command_timeout_seconds + (
            self.gateway.settings.worker_gateway_dispatch_lease_seconds
        )
        await self.gateway.bus.put_text(response_key(request_id, upload_id), json.dumps(staged), ttl)
        if not complete:
            await session.rollback()
            return {"status": "partial", "upload_id": upload_id}
        if file_result:
            try:
                decoded = base64.b64decode(raw, validate=True)
            except ValueError as exc:
                await self.gateway.bus.delete_text(response_key(request_id, upload_id))
                raise WorkerGatewayError("response_too_large", "响应体无法按原始字节还原") from exc
            if len(decoded) > MAX_FILE_BYTES:
                await self.gateway.bus.delete_text(response_key(request_id, upload_id))
                raise WorkerGatewayError("response_too_large", "响应体超过 20 MiB")
            digest = hashlib.sha256(decoded).hexdigest()
            size = len(decoded)
        else:
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            size = len(raw.encode("utf-8"))
        meta = {
            "http_status": http_status,
            "size": size,
            "sha256": digest,
            "headers": headers,
            "upload_id": upload_id,
            "content_type": content_type,
        }
        if file_result and isinstance(result, dict):
            for key, value in result.items():
                if key not in {"size", "sha256"}:
                    meta[key] = value
        if command.status == "succeeded" and (command.result or {}).get("sha256") == digest:
            stored = dict(command.result or {})
            await self.gateway.bus.delete_text(response_key(request_id, upload_id))
            await session.rollback()
            return {"status": "stored", "result": stored}
        current_instance = (
            select(AgentWorker.id)
            .where(AgentWorker.id == agent_worker_id, AgentWorker.epoch == epoch)
            .scalar_subquery()
        )
        values = {
            "status": "succeeded",
            "result": meta,
            "error_code": None,
            "finished_at": utcnow(),
            "updated_at": utcnow(),
            "slot_held": False,
        }
        owned = (
            WorkerCommand.id == command.id,
            WorkerCommand.epoch == epoch,
            WorkerCommand.status.in_(("pending", "accepted")),
            WorkerCommand.agent_worker_id == current_instance,
        )
        released = await session.execute(
            update(WorkerCommand).where(*owned, WorkerCommand.slot_held.is_(True)).values(**values)
        )
        if released.rowcount == 1:
            await release_http_slot(session, agent_worker_id, epoch)
        else:
            result = await session.execute(update(WorkerCommand).where(*owned).values(**values))
            if result.rowcount != 1:
                await session.rollback()
                await self.gateway.bus.delete_text(response_key(request_id, upload_id))
                raise WorkerGatewayError("epoch_conflict", "上传没有提交")
        await session.commit()
        await self.gateway.bus.publish_response(request_id, {"result": meta, "body": raw})
        return {"status": "stored", "result": meta}

    async def _read_stage(self, request_id: str, upload_id: str) -> dict[str, Any]:
        raw = await self.gateway.bus.get_text(response_key(request_id, upload_id))
        if not raw:
            return {"chunks": [], "headers": {}}
        parsed = json.loads(raw)
        parsed.setdefault("chunks", [])
        return parsed

    async def _current_command(
        self,
        session: AsyncSession,
        *,
        request_id: str,
        agent_worker_id: str,
        epoch: int,
    ) -> tuple[WorkerCommand, AgentWorker]:
        instance = await session.get(AgentWorker, agent_worker_id)
        command = await session.scalar(select(WorkerCommand).where(WorkerCommand.request_id == request_id))
        if instance is None or command is None or command.agent_worker_id != agent_worker_id:
            raise WorkerGatewayError("unknown_command", "指令不存在")
        if instance.epoch != epoch or command.epoch != epoch:
            raise WorkerGatewayError("epoch_conflict", "epoch 已切换")
        return command, instance

    async def _require_current_attempt(self, session, command):
        body = (command.payload or {}).get("body") or {}
        outbound = body.get("messageID") if isinstance(body, dict) else None
        task_id = (command.payload or {}).get("task_id")
        if not outbound or not task_id:
            return
        task = await session.get(Task, task_id)
        if (
            task is None
            or task.pending_message_id != outbound
            or task.current_session_id != command.session_id
            or (task.pending_prompt or {}).get("request_id") != command.request_id
        ):
            raise WorkerGatewayError("epoch_conflict", "指令已不是当前提交")


async def retire_submission(request_id):
    """退役旧命令并释放其 HTTP 槽；重发由 Task 重新提交，绝不跨 epoch 补投旧帧。"""
    from app.database import SessionLocal

    async with SessionLocal() as session:
        command = await session.scalar(select(WorkerCommand).where(WorkerCommand.request_id == request_id))
        if command is None or command.status not in {"pending", "accepted"}:
            return
        released = await session.execute(
            update(WorkerCommand)
            .where(
                WorkerCommand.id == command.id,
                WorkerCommand.status.in_(("pending", "accepted")),
                WorkerCommand.slot_held.is_(True),
            )
            .values(status="expired", error_code="superseded", finished_at=utcnow(), slot_held=False)
        )
        if released.rowcount:
            await release_http_slot(session, command.agent_worker_id, command.epoch)
        else:
            await session.execute(
                update(WorkerCommand)
                .where(WorkerCommand.id == command.id, WorkerCommand.status.in_(("pending", "accepted")))
                .values(status="expired", error_code="superseded", finished_at=utcnow())
            )
        await session.commit()
