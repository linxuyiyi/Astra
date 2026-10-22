"""员工模型路由。只读已启用的网关路由，不改 Gateway 和模型映射。"""

from __future__ import annotations

import os
from typing import Any

import httpx

from app.gateway.urls import gateway_base
from nexus_api.agentscope import AgentScopeClient, AgentScopeError

_VENDOR_HOSTS = ("api.openai.com", "api.deepseek.com")


class ModelRouteError(Exception):
    def __init__(self, detail: str, status_code: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def gateway_base_url() -> str:
    raw = os.environ.get("NEXUS_GATEWAY_OPENAI_BASE", "http://127.0.0.1:18110/v1").strip().rstrip("/")
    host = raw.split("://", 1)[-1].split("/", 1)[0].lower()
    if any(item in host for item in _VENDOR_HOSTS):
        raise ModelRouteError("员工模型不能改成直连厂商")
    try:
        return gateway_base(raw)
    except ValueError as exc:
        raise ModelRouteError(str(exc)) from None


def gateway_api_key() -> str:
    key = os.environ.get("NEXUS_GATEWAY_API_KEY", "").strip()
    if not key:
        raise ModelRouteError("网关访问密钥未配置")
    return key


def task_platform_url() -> str:
    return os.environ.get("NEXUS_TASK_PLATFORM_URL", "http://127.0.0.1:18120").rstrip("/")


def redact_secret(text: str, secret: str) -> str:
    if secret and secret in text:
        return text.replace(secret, "[redacted]")
    return text


async def fetch_routes(url: str) -> list[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            from .auth import internal_headers

            response = await client.get(f"{url}/api/gateway/routes", headers=internal_headers())
    except httpx.HTTPError as exc:
        raise ModelRouteError("无法读取已启用的模型路由", 503) from exc
    if response.status_code >= 400:
        raise ModelRouteError("无法读取已启用的模型路由", 503)
    payload = response.json()
    if not isinstance(payload, list):
        raise ModelRouteError("无法读取已启用的模型路由", 503)
    return [item for item in payload if isinstance(item, dict)]


async def require_enabled_route(
    route_id: str, *, loader=None, model_type: str = "CHAT"
) -> dict[str, str]:
    """返回已启用路由的 id 和模型名。响应里不带密钥。"""

    wanted = route_id.strip()
    if not wanted:
        raise ModelRouteError("没有选择模型路由")
    load = loader or fetch_routes
    routes = await load(task_platform_url())
    match = next((item for item in routes if str(item.get("id") or "") == wanted), None)
    if match is None:
        raise ModelRouteError("模型路由不存在")
    if not match.get("enabled"):
        raise ModelRouteError("模型路由未启用")
    if match.get("model_type", "CHAT") != model_type:
        raise ModelRouteError(f"必须选择 {model_type} 类型的模型路由")
    model = str(match.get("model") or "").strip()
    if not model:
        raise ModelRouteError("模型路由没有模型名")
    return {"id": wanted, "model": model}


def chat_config(credential_id: str, model: str) -> dict[str, Any]:
    return {
        "type": "openai_credential",
        "credential_id": credential_id,
        "model": model,
        "parameters": {},
    }


async def chat_config_for_route(
    client: AgentScopeClient, route_id: str, *, loader=None, model_type: str = "CHAT"
) -> dict[str, Any]:
    route = await require_enabled_route(route_id, loader=loader, model_type=model_type)
    key = gateway_api_key()
    base_url = gateway_base_url()
    try:
        credential_id = await client.ensure_gateway_credential(key, base_url)
    except AgentScopeError as exc:
        raise ModelRouteError(redact_secret(exc.detail, key), exc.status_code) from exc
    return chat_config(credential_id, route["model"])
