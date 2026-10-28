from __future__ import annotations

from typing import Any

import httpx

from .errors import ApiError

LIST_PATH = "system/versions/"
DETAIL_PATH = "system/versions/{version_id}/"

_ERROR_MESSAGES = {
    400: "请求参数校验失败",
    401: "会话无效或已过期，请更新 NR_MCP_SESSION_COOKIE",
    403: "当前会话无权查看产品版本（需要 system.version.view）",
    429: "版本接口限流，请稍后重试",
}


def _error_code(status_code: int) -> str:
    if status_code == 400:
        return "validation_error"
    if status_code == 401:
        return "unauthenticated"
    if status_code == 403:
        return "forbidden"
    if status_code == 404:
        return "not_found"
    if status_code == 429:
        return "upstream_rate_limited"
    return "upstream_error"


class VersionApiClient:
    """复用既有版本 HTTP API 的只读客户端。"""

    def __init__(
        self,
        base_url: str,
        session_cookie: str,
        timeout_seconds: float = 15.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._client = httpx.Client(
            base_url=f"{self._base_url}/",
            timeout=timeout_seconds,
            headers={
                "Accept": "application/json",
                "Cookie": session_cookie,
            },
            transport=transport,
        )

    def __repr__(self) -> str:
        return (
            "VersionApiClient("
            f"base_url={self._base_url!r}, timeout_seconds={self._timeout_seconds!r}"
            ")"
        )

    def close(self) -> None:
        self._client.close()

    def list_versions(
        self,
        *,
        search: str | None = None,
        product_name: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if search:
            params["search"] = search
        if product_name:
            params["product_name"] = product_name
        if status:
            params["status"] = status

        payload = self._get(
            LIST_PATH,
            params=params,
            not_found_message="页码超出范围或没有匹配的版本",
        )
        self._ensure_page_payload(payload)
        return payload

    def get_version(self, version_id: str) -> dict[str, Any]:
        payload = self._get(DETAIL_PATH.format(version_id=version_id))
        if not payload.get("id"):
            raise ApiError(
                "upstream_invalid_response",
                "版本接口返回的详情结构不完整",
                {},
            )
        return payload

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        not_found_message: str = "请求的产品版本不存在",
    ) -> dict[str, Any]:
        try:
            response = self._client.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise ApiError(
                "upstream_timeout",
                "访问版本接口超时，请稍后重试",
                {},
            ) from exc
        except httpx.RequestError as exc:
            raise ApiError(
                "upstream_unavailable",
                "无法连接版本接口，请检查 API 基地址与网络",
                {},
            ) from exc

        if response.status_code >= 400:
            self._raise_for_response(response, not_found_message=not_found_message)

        try:
            payload = response.json()
        except ValueError as exc:
            raise ApiError(
                "upstream_invalid_response",
                "版本接口返回了无法解析的响应",
                {},
            ) from exc

        if not isinstance(payload, dict):
            raise ApiError(
                "upstream_invalid_response",
                "版本接口返回的响应结构不合法",
                {},
            )
        return payload

    def _raise_for_response(
        self,
        response: httpx.Response,
        *,
        not_found_message: str,
    ) -> None:
        status_code = response.status_code
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        details: dict[str, Any] = {}
        if status_code == 400:
            upstream_details = payload.get("details")
            if isinstance(upstream_details, dict):
                details = upstream_details

        if status_code >= 500:
            message = "版本接口暂时不可用，请稍后重试"
        elif status_code == 404:
            message = not_found_message
        else:
            message = _ERROR_MESSAGES.get(status_code, "版本接口请求失败")

        raise ApiError(
            _error_code(status_code),
            message,
            details,
            status=status_code,
        )

    @staticmethod
    def _ensure_page_payload(payload: dict[str, Any]) -> None:
        valid_ints = []
        for key in ("count", "page", "page_size"):
            value = payload.get(key)
            valid_ints.append(isinstance(value, int) and not isinstance(value, bool))
        if not all(valid_ints) or not isinstance(payload.get("results"), list):
            raise ApiError(
                "upstream_invalid_response",
                "版本接口返回的分页结构不完整",
                {},
            )
