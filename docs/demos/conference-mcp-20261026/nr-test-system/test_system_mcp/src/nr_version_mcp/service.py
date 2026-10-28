from __future__ import annotations

import uuid
from typing import Any, Protocol

from .errors import ToolInputError

VALID_STATUSES = frozenset({"DRAFT", "RELEASED", "ARCHIVED"})
MAX_PAGE_SIZE = 100


class VersionApi(Protocol):
    def list_versions(
        self,
        *,
        search: str | None = None,
        product_name: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]: ...

    def get_version(self, version_id: str) -> dict[str, Any]: ...


def _validate_page(page: int) -> int:
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ToolInputError(
            "page 必须是大于等于 1 的整数",
            {"page": "必须为 >= 1 的整数"},
        )
    return page


def _validate_page_size(page_size: int) -> int:
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or page_size < 1
        or page_size > MAX_PAGE_SIZE
    ):
        raise ToolInputError(
            f"page_size 必须是 1 到 {MAX_PAGE_SIZE} 之间的整数",
            {"page_size": f"必须为 1..{MAX_PAGE_SIZE} 的整数"},
        )
    return page_size


def _validate_optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolInputError(
            f"{field} 必须是字符串",
            {field: "必须为字符串"},
        )
    if not value.strip():
        return None
    return value


def _validate_status(status: str | None) -> str | None:
    if status is None:
        return None
    if not isinstance(status, str):
        raise ToolInputError(
            "status 必须是字符串",
            {"status": "必须为字符串"},
        )
    if not status.strip():
        return None
    if status not in VALID_STATUSES:
        raise ToolInputError(
            "status 只支持 DRAFT、RELEASED、ARCHIVED",
            {"status": f"必须是 {sorted(VALID_STATUSES)} 之一"},
        )
    return status


def validate_version_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(
            "version_id 必须是非空 UUID 字符串",
            {"version_id": "必须为 UUID"},
        )
    try:
        parsed = uuid.UUID(value.strip())
    except (ValueError, AttributeError) as exc:
        raise ToolInputError(
            "version_id 必须是合法的 UUID",
            {"version_id": "必须为 UUID"},
        ) from exc
    return str(parsed)


class VersionQueryService:
    """对 MCP 工具输入做本地校验，再委托只读 HTTP 客户端。"""

    def __init__(self, api: VersionApi):
        self._api = api

    def list_versions(
        self,
        *,
        search: str | None = None,
        product_name: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        normalized_product_name = _validate_optional_text(product_name, "product_name")
        return self._api.list_versions(
            search=_validate_optional_text(search, "search"),
            product_name=(
                normalized_product_name.strip() if normalized_product_name is not None else None
            ),
            status=_validate_status(status),
            page=_validate_page(page),
            page_size=_validate_page_size(page_size),
        )

    def get_version(self, version_id: str) -> dict[str, Any]:
        return self._api.get_version(validate_version_id(version_id))
