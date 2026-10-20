"""Worker Gateway HTTP 与 WebSocket。路径与 Agent Worker 别名 /api/workers 分开。"""

from __future__ import annotations

import hmac
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException, Response, WebSocket
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.database import get_session
from app.gateway.security import verify_gateway_key
from app.models import WorkerCommand, WorkerInstance
from app.worker_gateway.admin_key import warn_if_admin_key_missing
from app.worker_gateway.data_channel import DataChannel, RawBody
from app.worker_gateway.protocol import WorkerGatewayError
from app.worker_gateway.service import get_gateway

ADMIN_HEADER = "X-Astra-Worker-Gateway-Key"

router = APIRouter(prefix="/api/worker-gateway")

_STATUS = {
    "unauthorized": 401,
    "unknown_environment": 404,
    "unknown_worker": 404,
    "frame_too_large": 413,
    "invalid_message": 400,
    "duplicate_request": 409,
    "epoch_conflict": 409,
    "body_too_large": 413,
    "response_too_large": 413,
    "unknown_command": 404,
}


class EnrollBody(BaseModel):
    environment_id: int = Field(ge=1)


class CommandBody(BaseModel):
    request_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = Field(default=None, max_length=160)
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)


async def require_admin(
    key: Annotated[str | None, Header(alias=ADMIN_HEADER)] = None,
) -> None:
    expected = get_settings().worker_gateway_admin_key.strip()
    if not expected:
        warn_if_admin_key_missing("")
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "管理密钥未配置，拒绝管理调用"},
        )
    if key is None or not hmac.compare_digest(key, expected):
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "管理接口需要内部服务密钥"},
        )


def _raise(exc: WorkerGatewayError) -> NoReturn:
    raise HTTPException(
        status_code=_STATUS.get(exc.code, 400),
        detail={"code": exc.code, "message": str(exc)},
    ) from exc


def _instance_body(instance: WorkerInstance) -> dict[str, Any]:
    return {
        "worker_instance_id": instance.id,
        "environment_id": instance.environment_id,
        "status": instance.status,
        "epoch": instance.epoch,
        "agent_version": instance.agent_version,
        "last_heartbeat_at": instance.last_heartbeat_at,
        "connected_at": instance.connected_at,
    }


def _command_body(command: WorkerCommand, *, delivered: bool, replayed: bool, error_code: str | None) -> dict:
    return {
        "request_id": command.request_id,
        "worker_instance_id": command.worker_instance_id,
        "environment_id": command.environment_id,
        "session_id": command.session_id,
        "name": command.name,
        "status": command.status,
        "epoch": command.epoch,
        "result": command.result,
        "error_code": command.error_code or error_code,
        "deadline_at": command.deadline_at,
        "delivered": delivered,
        "replayed": replayed,
    }


@router.get("/health")
async def health() -> dict[str, Any]:
    gateway = get_gateway()
    return {
        "status": "ok",
        "bus": gateway.bus_mode,
        "requested_bus": gateway.settings.worker_gateway_bus,
        "owner_id": gateway.owner_id,
        "warning": gateway.bus_warning,
    }


@router.post("/instances", status_code=201)
async def enroll_instance(
    body: EnrollBody,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    try:
        result = await get_gateway().enroll(session, body.environment_id)
    except WorkerGatewayError as exc:
        _raise(exc)
    return {
        "worker_instance_id": result.worker_instance_id,
        "environment_id": result.environment_id,
        "token": result.token,
        "epoch": result.epoch,
        "rotated": result.rotated,
    }


@router.post("/environments/{environment_id}/channel")
async def switch_environment_channel(
    environment_id: int,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    from app.runtime.worker_channel import mark_worker_channel

    environment = await mark_worker_channel(session, environment_id)
    return {"environment_id": environment.id, "worker_channel": environment.worker_channel}


@router.get("/instances/{worker_instance_id}")
async def get_instance(
    worker_instance_id: str,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    instance = await session.get(WorkerInstance, worker_instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail={"code": "unknown_worker", "message": "Worker 不存在"})
    return _instance_body(instance)


@router.post("/instances/{worker_instance_id}/commands")
async def create_command(
    worker_instance_id: str,
    body: CommandBody,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    if body.name in {"switch_channel", "restart_opencode"}:
        raise HTTPException(
            status_code=403,
            detail={"code": "maintenance_command", "message": "维护指令只能通过维护入口下发"},
        )
    try:
        result = await get_gateway().dispatch(
            session,
            worker_instance_id=worker_instance_id,
            request_id=body.request_id,
            name=body.name,
            payload=body.payload,
            session_id=body.session_id,
            timeout_seconds=body.timeout_seconds,
        )
    except WorkerGatewayError as exc:
        _raise(exc)
    return _command_body(
        result.command,
        delivered=result.delivered,
        replayed=result.replayed,
        error_code=result.error_code,
    )


@router.get("/commands/{request_id}")
async def get_command(
    request_id: str,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    command = await session.scalar(select(WorkerCommand).where(WorkerCommand.request_id == request_id))
    if command is None:
        raise HTTPException(status_code=404, detail={"code": "unknown_command", "message": "指令不存在"})
    return _command_body(command, delivered=False, replayed=False, error_code=None)


class UploadBody(BaseModel):
    upload_id: str = Field(min_length=1, max_length=80)
    http_status: int = Field(ge=100, le=599)
    headers: dict[str, str] = Field(default_factory=dict)
    content_type: str | None = None
    chunk: str = ""
    complete: bool = True
    result: dict[str, Any] | None = None


def request_body_response(body: dict[str, Any] | RawBody) -> dict[str, Any] | Response:
    if isinstance(body, RawBody):
        return Response(
            content=body.content,
            media_type="application/octet-stream",
            headers={"X-Astra-Sha256": body.sha256},
        )
    return body


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise WorkerGatewayError("unauthorized", "需要登记 token")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise WorkerGatewayError("unauthorized", "需要登记 token")
    return token


async def _worker_for_token(session: AsyncSession, token: str) -> WorkerInstance:
    rows = (await session.scalars(select(WorkerInstance))).all()
    for row in rows:
        if verify_gateway_key(token, row.token_digest):
            return row
    raise WorkerGatewayError("unauthorized", "登记 token 无效")


@router.get("/data/{request_id}/request")
async def fetch_request_body(
    request_id: str,
    session: AsyncSession = Depends(get_session),
    authorization: Annotated[str | None, Header()] = None,
    epoch: Annotated[int | None, Header(alias="X-Astra-Worker-Epoch")] = None,
) -> Any:
    try:
        if epoch is None:
            raise WorkerGatewayError("unauthorized", "缺少 epoch")
        worker = await _worker_for_token(session, _bearer(authorization))
        fetched = await DataChannel(get_gateway()).fetch_request(
            session,
            request_id=request_id,
            worker_instance_id=worker.id,
            epoch=epoch,
        )
        return request_body_response(fetched)
    except WorkerGatewayError as exc:
        _raise(exc)


@router.put("/data/{request_id}/response")
async def upload_response_body(
    request_id: str,
    body: UploadBody,
    session: AsyncSession = Depends(get_session),
    authorization: Annotated[str | None, Header()] = None,
    epoch: Annotated[int | None, Header(alias="X-Astra-Worker-Epoch")] = None,
) -> dict[str, Any]:
    try:
        if epoch is None:
            raise WorkerGatewayError("unauthorized", "缺少 epoch")
        worker = await _worker_for_token(session, _bearer(authorization))
        return await DataChannel(get_gateway()).commit_upload(
            session,
            request_id=request_id,
            worker_instance_id=worker.id,
            epoch=epoch,
            upload_id=body.upload_id,
            http_status=body.http_status,
            headers=body.headers,
            content_type=body.content_type,
            chunk=body.chunk,
            complete=body.complete,
            result=body.result,
        )
    except WorkerGatewayError as exc:
        _raise(exc)


@router.websocket("/connect")
async def connect(websocket: WebSocket) -> None:
    await get_gateway().serve_socket(websocket)
