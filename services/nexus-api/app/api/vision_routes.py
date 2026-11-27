"""全平台图像理解配置与内部解析接口，所有识图调用统一经过 Nexus 网关。"""

from __future__ import annotations

import hashlib
from collections import OrderedDict

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.gateway.models import GatewayAuditEvent, GatewayPool, GatewayRoute, GatewayVisionInput
from app.gateway.schemas import VisionInputConfig, VisionInputRead
from app.gateway.vision import build_vision_messages, extract_vision_text, vision_cache_key
from nexus_api.auth import require_internal, require_user
from nexus_api.model_route import ModelRouteError, gateway_api_key, gateway_base_url

router = APIRouter(tags=["vision-input"])

_MAX_IMAGE_BASE64_CHARS = 2 * 1024 * 1024  # ~1.5 MiB 原始图片
_CACHE_LIMIT = 256
_VISION_CACHE: OrderedDict[str, str] = OrderedDict()
_ALLOWED_IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})


def clear_vision_cache() -> None:
    _VISION_CACHE.clear()


def _cache_get(key: str) -> str | None:
    value = _VISION_CACHE.get(key)
    if value is not None:
        _VISION_CACHE.move_to_end(key)
    return value


def _cache_set(key: str, value: str) -> None:
    _VISION_CACHE[key] = value
    _VISION_CACHE.move_to_end(key)
    while len(_VISION_CACHE) > _CACHE_LIMIT:
        _VISION_CACHE.popitem(last=False)


def vision_user(request: Request) -> str:
    """复用与语音输入一致的平台会话身份。"""

    return str(require_user(request)["uid"])


async def vision_route(session: AsyncSession, route_id: str | None) -> GatewayRoute:
    route = await session.get(GatewayRoute, route_id) if route_id else None
    if route is None or route.model_type != "CHAT" or not route.enabled or not route.supports_vision:
        raise HTTPException(422, "请在 API 网关配置已启用且支持图像理解的对话路由")
    pool = await session.get(GatewayPool, route.pool_id)
    if pool is None or not pool.enabled:
        raise HTTPException(422, "图像理解路由的资源池未启用")
    return route


class VisionAnalyzeBody(BaseModel):
    image_base64: str = Field(min_length=1, max_length=_MAX_IMAGE_BASE64_CHARS)
    media_type: str = Field(default="image/jpeg", max_length=64)
    filename: str = Field(default="", max_length=255)
    question: str = Field(default="", max_length=2000)
    sha256: str = Field(default="", max_length=64)


@router.get("/api/gateway/vision-input", response_model=VisionInputRead)
async def get_vision_input(session: AsyncSession = Depends(get_session)):
    config = await session.get(GatewayVisionInput, "default")
    return VisionInputRead(route_id=config.route_id if config else None)


@router.put("/api/gateway/vision-input", response_model=VisionInputRead)
async def set_vision_input(
    body: VisionInputConfig,
    user: str = Depends(vision_user),
    session: AsyncSession = Depends(get_session),
):
    if body.route_id:
        await vision_route(session, body.route_id)
    config = await session.get(GatewayVisionInput, "default")
    before = {"route_id": config.route_id if config else None}
    if config is None:
        config = GatewayVisionInput(id="default")
        session.add(config)
    config.route_id = body.route_id
    session.add(
        GatewayAuditEvent(
            actor=user,
            actor_role="user",
            action="vision-input.update",
            resource_type="vision-input",
            resource_id="default",
            before=before,
            after=body.model_dump(),
        )
    )
    await session.commit()
    return body


@router.post("/api/v1/internal/vision/analyze")
async def analyze_vision(
    body: VisionAnalyzeBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    require_internal(request)
    media_type = body.media_type.strip().lower()
    if media_type not in _ALLOWED_IMAGE_MEDIA_TYPES:
        raise HTTPException(400, "不支持的图片格式")
    config = await session.get(GatewayVisionInput, "default")
    route = await vision_route(session, config.route_id if config else None)
    model = route.model
    target_model = route.target_model
    # 远程识别期间不占用数据库连接。
    await session.rollback()

    digest = body.sha256.strip() or hashlib.sha256(body.image_base64.encode("ascii", "ignore")).hexdigest()
    key = vision_cache_key(model, target_model, digest, body.question)
    cached = _cache_get(key)
    if cached is not None:
        return {"text": cached, "model": model, "cached": True}

    try:
        base = gateway_base_url()
        api_key = gateway_api_key()
    except ModelRouteError as exc:
        raise HTTPException(503, "图像理解的服务端网关地址或调用密钥未配置") from exc

    data_url = f"data:{media_type};base64,{body.image_base64}"
    payload = {
        "model": model,
        "stream": False,
        "messages": build_vision_messages(data_url, filename=body.filename, question=body.question),
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5)) as client:
            response = await client.post(
                f"{base}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "图像理解网关请求超时，请重试") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(503, "无法连接图像理解网关，请重试") from exc

    if not response.is_success:
        if 400 <= response.status_code < 500 and response.status_code not in {401, 403, 429}:
            raise HTTPException(response.status_code, "图像理解模型拒绝了当前图片请求")
        if response.status_code == 429:
            raise HTTPException(503, "图像理解服务当前繁忙，请稍后重试")
        raise HTTPException(502, "图像理解服务暂时不可用，请稍后重试")

    try:
        parsed = response.json()
    except ValueError as exc:
        raise HTTPException(502, "图像理解返回格式异常") from exc
    text = extract_vision_text(parsed)
    if not text:
        raise HTTPException(502, "图像理解未返回有效内容")
    _cache_set(key, text)
    return {"text": text, "model": model, "cached": False}
