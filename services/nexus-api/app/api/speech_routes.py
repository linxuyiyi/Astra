"""聊天录音只经过服务端网关，浏览器无需持有网关或模型凭据。"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.gateway.audio import AUDIO_PATH, MAX_AUDIO_BODY_BYTES, parse_audio_upload
from app.gateway.errors import GatewayDataPlaneError, payload_too_large
from app.gateway.models import GatewayAuditEvent, GatewayPool, GatewayRoute, GatewaySpeechInput
from app.gateway.schemas import SpeechInputConfig
from nexus_api.auth import require_user
from nexus_api.model_route import ModelRouteError, gateway_api_key, gateway_base_url

router = APIRouter(tags=["speech-input"])


def speech_user(request: Request) -> str:
    """Use the same server-side session as every other Nexus route."""

    return str(require_user(request)["uid"])


async def speech_route(session: AsyncSession, route_id: str | None) -> GatewayRoute:
    route = await session.get(GatewayRoute, route_id) if route_id else None
    if route is None or route.model_type != "ASR" or not route.enabled:
        raise HTTPException(422, "请在 API 网关配置已启用的语音识别路由")
    pool = await session.get(GatewayPool, route.pool_id)
    if pool is None or not pool.enabled:
        raise HTTPException(422, "语音识别路由的资源池未启用")
    return route


@router.get("/api/gateway/speech-input", response_model=SpeechInputConfig)
async def get_speech_input(session: AsyncSession = Depends(get_session)):
    config = await session.get(GatewaySpeechInput, "default")
    return SpeechInputConfig(route_id=config.route_id if config else None)


@router.put("/api/gateway/speech-input", response_model=SpeechInputConfig)
async def set_speech_input(
    body: SpeechInputConfig,
    user: str = Depends(speech_user),
    session: AsyncSession = Depends(get_session),
):
    if body.route_id:
        await speech_route(session, body.route_id)
    config = await session.get(GatewaySpeechInput, "default")
    before = {"route_id": config.route_id if config else None}
    if config is None:
        config = GatewaySpeechInput(id="default")
        session.add(config)
    config.route_id = body.route_id
    session.add(GatewayAuditEvent(
        actor=user, actor_role="user", action="speech-input.update",
        resource_type="speech-input", resource_id="default",
        before=before, after=body.model_dump(),
    ))
    await session.commit()
    return body


@router.post("/api/asr/transcribe")
async def transcribe(
    request: Request,
    _user: str = Depends(speech_user),
    session: AsyncSession = Depends(get_session),
):
    config = await session.get(GatewaySpeechInput, "default")
    route = await speech_route(session, config.route_id if config else None)
    model = route.model
    # 远程识别期间不占用数据库连接。
    await session.rollback()
    try:
        base = gateway_base_url()
        key = gateway_api_key()
    except ModelRouteError as exc:
        raise HTTPException(503, "语音输入的服务端网关地址或调用密钥未配置") from exc
    try:
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_AUDIO_BODY_BYTES:
            raise payload_too_large("音频超过 12 MiB 上限")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_AUDIO_BODY_BYTES:
                raise payload_too_large("音频超过 12 MiB 上限")
        audio = parse_audio_upload(bytes(data), request.headers.get("content-type", ""))
        # 浏览器不能覆盖服务端配置的模型；音频表单只允许上传文件。
        if audio.fields:
            raise HTTPException(400, "语音输入的模型由网关配置选择")
        body, content_type = audio.encode(model)
    except GatewayDataPlaneError as exc:
        return exc.to_response()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(55, connect=5)) as client:
            response = await client.post(
                base + AUDIO_PATH.removeprefix("/v1"),
                content=body,
                headers={"Authorization": f"Bearer {key}", "Content-Type": content_type},
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "语音识别网关请求超时，请重试") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(503, "无法连接语音识别网关，请重试") from exc
    if not response.is_success:
        # 4xx 是用户音频或请求问题，转发标准化 code/message 让前端给出可操作提示；
        # 鉴权、限流和 5xx 不回显上游细节，避免泄露节点或凭据信息。
        status = response.status_code
        code = ""
        message = ""
        try:
            payload = response.json()
            detail = payload.get("detail")
            if isinstance(detail, dict):
                code = str(detail.get("code") or "")
                message = str(detail.get("message") or "")
            elif isinstance(detail, str):
                message = detail
            error = payload.get("error")
            if isinstance(error, dict):
                code = code or str(error.get("code") or "")
                message = message or str(error.get("message") or "")
        except (ValueError, AttributeError):
            pass
        if status in {400, 404, 413, 415, 422, 429} and code and message:
            return JSONResponse(
                {"detail": {"code": code[:64], "message": message[:200]}},
                status_code=status,
            )
        return JSONResponse(
            {"detail": {"code": "gateway_asr_failed", "message": "语音识别网关调用失败，请检查路由与节点"}},
            status_code=status,
        )
    try:
        text = response.json()["text"]
        if not isinstance(text, str):
            raise ValueError("invalid text")
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(502, "语音识别网关未返回有效文字") from exc
    return {"text": text}
