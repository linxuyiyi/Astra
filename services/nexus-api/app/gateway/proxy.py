"""下游透明转发。

请求体按原始字节转发到同协议下游，只替换认证 Header：客户端带来的
``Authorization`` 与 hop-by-hop Header 一律不转发，节点认证 Header 由
``auth_type`` / ``auth_header`` 与解密的 ``api_key_encrypted`` 构造。

只有明确证明请求未发送到下游的传输错误（连接被拒、连接超时）才被标记为
:class:`NotSentDownstream`，允许切换节点重试；一旦收到响应头或流式分块，绝不
重试。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from app.gateway.errors import (
    UPSTREAM_CONNECT_ERROR,
    UPSTREAM_CONNECT_TIMEOUT,
    UPSTREAM_ERROR,
    UPSTREAM_PROTOCOL_ERROR,
    UPSTREAM_TIMEOUT,
    GatewayDataPlaneError,
)
from app.gateway.probe import build_node_headers

# 不转发的请求头：路由覆盖 + hop-by-hop + 客户端认证。
_EXCLUDED_HEADERS = {
    "host",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "accept-encoding",
    "authorization",
}


class NotSentDownstream(Exception):
    """传输错误，且可证明请求未发送到下游。"""

    def __init__(self, message: str, *, timeout: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout


def build_upstream_headers(
    client_headers: Mapping[str, str], node: Any, api_key: str | None
) -> dict[str, str]:
    headers = {
        key: value
        for key, value in client_headers.items()
        if key.lower() not in _EXCLUDED_HEADERS
    }
    headers.setdefault("content-type", "application/json")
    headers.update(build_node_headers(node, api_key))
    return headers


def upstream_url(node: Any, path: str) -> str:
    base = str(getattr(node, "base_url", "") or "").rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return f"{base}{path}"


class GatewayProxy:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        default_connect_timeout: float = 10.0,
        default_request_timeout: float = 600.0,
    ) -> None:
        # 每个请求通过 build_request 单独设置 connect / read 超时，客户端不设默认值。
        self._client = httpx.AsyncClient(transport=transport, timeout=None)
        self._default_connect_timeout = default_connect_timeout
        self._default_request_timeout = default_request_timeout

    async def close(self) -> None:
        await self._client.aclose()

    def timeout_for(self, node: Any) -> httpx.Timeout:
        request = getattr(node, "request_timeout_seconds", None) or self._default_request_timeout
        connect = getattr(node, "connect_timeout_seconds", None) or self._default_connect_timeout
        return httpx.Timeout(request, connect=connect)

    def build_request(
        self,
        node: Any,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        content: bytes,
    ) -> httpx.Request:
        return self._client.build_request(
            method,
            upstream_url(node, path),
            headers=dict(headers),
            content=content,
            timeout=self.timeout_for(node),
        )

    async def send(self, request: httpx.Request, *, stream: bool) -> httpx.Response:
        try:
            return await self._client.send(request, stream=stream)
        except httpx.ConnectTimeout as exc:
            raise NotSentDownstream(str(exc), timeout=True) from exc
        except httpx.ConnectError as exc:
            raise NotSentDownstream(str(exc)) from exc

    @staticmethod
    def classify(exc: Exception) -> GatewayDataPlaneError:
        if isinstance(exc, NotSentDownstream):
            if exc.timeout:
                return GatewayDataPlaneError(
                    UPSTREAM_CONNECT_TIMEOUT, "连接下游超时", 504, "api_error", not_sent=True
                )
            return GatewayDataPlaneError(
                UPSTREAM_CONNECT_ERROR, "无法连接下游节点", 502, "api_error", not_sent=True
            )
        if isinstance(exc, httpx.TimeoutException):
            return GatewayDataPlaneError(UPSTREAM_TIMEOUT, "下游请求超时", 504)
        if isinstance(exc, httpx.RemoteProtocolError):
            return GatewayDataPlaneError(UPSTREAM_PROTOCOL_ERROR, "下游协议错误", 502)
        if isinstance(exc, httpx.HTTPError):
            return GatewayDataPlaneError(UPSTREAM_ERROR, "下游请求失败", 502)
        raise exc