from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Any

import httpx

from .config import STATUS_CHOICES, Settings

USER_AGENT = "nr-version-mcp/0.1.0"


class VersionApiError(RuntimeError):
    """版本查询服务返回的可解释错误。"""

    def __init__(self, code: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_text(self) -> str:
        return f"{self.code}: {self.message}"


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _checked_page(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise VersionApiError("invalid_argument", f"{name} 必须是大于 0 的整数")
    return value


def _checked_page_size(value: int, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise VersionApiError("invalid_argument", "page_size 必须是大于 0 的整数")
    if value > maximum:
        raise VersionApiError("invalid_argument", f"page_size 不能超过 {maximum}")
    return value


def _parse_statuses(value: Sequence[str] | str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        parts: Sequence[str] = [value]
    else:
        parts = value
    statuses: list[str] = []
    for part in parts:
        if not isinstance(part, str):
            raise VersionApiError("invalid_argument", "status 只能是字符串或字符串列表")
        for item in part.split(","):
            status = item.strip().upper()
            if not status:
                continue
            if status not in STATUS_CHOICES:
                raise VersionApiError(
                    "invalid_argument",
                    f"未知版本状态 {item!r}，可选值：{', '.join(STATUS_CHOICES)}",
                )
            if status not in statuses:
                statuses.append(status)
    return tuple(statuses) or None


def _parse_date(value: str | None, *, name: str) -> dt.date | None:
    text = _clean_text(value)
    if text is None:
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError as exc:
        raise VersionApiError("invalid_argument", f"{name} 必须是 YYYY-MM-DD 格式的日期") from exc


def _timestamp(value: Any) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _payload_error(response: httpx.Response) -> VersionApiError:
    payload: Any = None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    code = "request_error"
    message = "版本查询服务返回错误"
    if isinstance(payload, dict):
        code = str(payload.get("code") or code)
        message = str(payload.get("message") or message)
    elif response.status_code == 401:
        code, message = "unauthenticated", "会话 Cookie 无效或已过期"
    elif response.status_code == 403:
        code, message = "forbidden", "没有查询产品版本的权限"
    elif response.status_code == 404:
        code, message = "not_found", "请求的资源不存在"
    return VersionApiError(code, message, status_code=response.status_code)


class ProductVersionClient:
    """只读访问 NR 版本 HTTP API 的客户端。"""

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> None:
        self._settings = settings
        self._client = httpx.Client(
            base_url=f"{settings.base_url}/",
            headers={
                "Accept": "application/json",
                "Cookie": settings.session_cookie,
                "User-Agent": USER_AGENT,
            },
            timeout=settings.timeout,
            transport=transport,
            trust_env=False,
        )

    def __enter__(self) -> ProductVersionClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise VersionApiError(
                "connection_error",
                "无法连接版本查询服务，请确认 127.0.0.1:8000 本地演示服务已启动",
            ) from exc
        if response.status_code >= 400:
            raise _payload_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise VersionApiError("invalid_response", "版本查询服务返回了非 JSON 响应") from exc
        if not isinstance(payload, dict):
            raise VersionApiError("invalid_response", "版本查询服务返回的数据结构不正确")
        return payload

    def _normalise_page(self, payload: dict[str, Any], *, requested_page: int) -> dict[str, Any]:
        count = payload.get("count")
        results = payload.get("results")
        if isinstance(count, bool) or not isinstance(count, int) or not isinstance(results, list):
            raise VersionApiError("invalid_response", "版本查询服务分页结构不正确")
        page = payload.get("page")
        page_size = payload.get("page_size")
        return {
            "count": count,
            "page": page if isinstance(page, int) else requested_page,
            "page_size": page_size if isinstance(page_size, int) else len(results),
            "results": results,
        }

    def _base_params(
        self,
        *,
        keyword: str | None,
        product_name: str | None,
        status: str | None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if keyword:
            params["search"] = keyword
        if product_name:
            params["product_name"] = product_name
        if status:
            params["status"] = status
        return params

    def search(
        self,
        *,
        keyword: str | None = None,
        product_name: str | None = None,
        statuses: Sequence[str] | str | None = None,
        release_date_from: str | None = None,
        release_date_to: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        page = _checked_page(page, name="page")
        page_size = _checked_page_size(page_size, maximum=self._settings.max_page_size)
        keyword = _clean_text(keyword)
        product_name = _clean_text(product_name)
        effective_statuses = _parse_statuses(statuses) or self._settings.default_statuses
        start = _parse_date(release_date_from, name="release_date_from")
        end = _parse_date(release_date_to, name="release_date_to")
        if start and end and start > end:
            raise VersionApiError("invalid_argument", "release_date_from 不能晚于 release_date_to")

        if len(effective_statuses) == 1 and start is None and end is None:
            params = self._base_params(
                keyword=keyword, product_name=product_name, status=effective_statuses[0]
            )
            params.update({"page": page, "page_size": page_size})
            payload = self._get("system/versions/", params=params)
            result = self._normalise_page(payload, requested_page=page)
            result.update(
                {
                    "applied_statuses": list(effective_statuses),
                    "client_side_filtering": False,
                }
            )
            return result

        return self._search_aggregated(
            keyword=keyword,
            product_name=product_name,
            statuses=effective_statuses,
            start=start,
            end=end,
            page=page,
            page_size=page_size,
        )

    def _search_aggregated(
        self,
        *,
        keyword: str | None,
        product_name: str | None,
        statuses: tuple[str, ...],
        start: dt.date | None,
        end: dt.date | None,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        fetch_size = self._settings.max_page_size
        collected: list[dict[str, Any]] = []
        for status in statuses:
            params = self._base_params(keyword=keyword, product_name=product_name, status=status)
            params.update({"page": 1, "page_size": fetch_size})
            fetched_pages = 0
            while True:
                payload = self._get("system/versions/", params=params)
                page_data = self._normalise_page(payload, requested_page=params["page"])
                fetched_pages += 1
                collected.extend(item for item in page_data["results"] if isinstance(item, dict))
                if not page_data["results"]:
                    break
                if fetched_pages * fetch_size >= page_data["count"]:
                    break
                if fetched_pages >= self._settings.max_fetch_pages:
                    raise VersionApiError(
                        "result_too_large",
                        "多状态或日期筛选需要读取的结果过多，请缩小产品、关键字或时间范围",
                    )
                params["page"] = fetched_pages + 1

        unique: dict[str, dict[str, Any]] = {}
        for item in collected:
            key = str(item.get("id") or "")
            if not key:
                key = f"{item.get('product_name')}|{item.get('version')}|{item.get('status')}"
            unique[key] = item

        items = list(unique.values())
        if start or end:
            filtered: list[dict[str, Any]] = []
            for item in items:
                raw_date = item.get("release_date")
                if not isinstance(raw_date, str) or not raw_date:
                    continue
                try:
                    released = dt.date.fromisoformat(raw_date)
                except ValueError:
                    continue
                if start and released < start:
                    continue
                if end and released > end:
                    continue
                filtered.append(item)
            items = filtered

        items.sort(
            key=lambda item: (
                _timestamp(item.get("updated_at")),
                str(item.get("id") or ""),
            ),
            reverse=True,
        )
        total = len(items)
        offset = (page - 1) * page_size
        return {
            "count": total,
            "page": page,
            "page_size": page_size,
            "results": items[offset : offset + page_size],
            "applied_statuses": list(statuses),
            "client_side_filtering": True,
            "applied_release_date": {
                "from": start.isoformat() if start else None,
                "to": end.isoformat() if end else None,
            },
        }

    def get(self, version_id: str) -> dict[str, Any]:
        text = _clean_text(version_id)
        if text is None:
            raise VersionApiError("invalid_argument", "version_id 不能为空")
        try:
            canonical = str(uuid.UUID(text))
        except ValueError as exc:
            raise VersionApiError("invalid_argument", "version_id 必须是合法的 UUID") from exc
        return self._get(f"system/versions/{canonical}/")
