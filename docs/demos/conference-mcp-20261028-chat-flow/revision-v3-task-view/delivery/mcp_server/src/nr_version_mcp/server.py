from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from .client import ProductVersionClient, VersionApiError
from .config import ConfigError, load_settings

INSTRUCTIONS = """
只读查询 NR 测试系统的产品版本。所有能力复用 127.0.0.1:8000/api/v1 的现有 HTTP API，
不会创建、修改或删除任何数据。

- 搜索与筛选：search_product_versions 支持关键字、产品名、版本状态和发布日期范围；
- 分页：page 从 1 开始，page_size 最大 100，响应包含 count/page/page_size；
- 详情：get_product_version 使用列表返回的版本 UUID 获取完整信息。

调用方未指定 status 时使用部署配置的默认状态范围（默认仅 RELEASED，可用
NR_DEFAULT_STATUSES 覆盖）。现有 HTTP API 只支持单个 status 与关键字/产品名筛选，
因此多状态和发布日期筛选由 MCP 在读取后本地聚合，并会在响应中标记
client_side_filtering=true。
""".strip()

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

ClientFactory = Callable[[], ProductVersionClient]


def _coerce_status(status: list[str] | str | None) -> list[str] | None:
    if status is None:
        return None
    if isinstance(status, str):
        return [status]
    return status


def create_server(client_factory: ClientFactory | None = None) -> FastMCP:
    factory: ClientFactory = client_factory or (lambda: ProductVersionClient(load_settings()))
    server = FastMCP(
        name="nr-product-version-query",
        instructions=INSTRUCTIONS,
        log_level="WARNING",
    )

    @server.tool(
        name="search_product_versions",
        description=(
            "按关键字、产品名、版本状态和发布日期范围搜索产品版本，返回分页结果。"
            "status 省略时使用部署默认状态范围；page 从 1 开始，page_size 最大 100。"
        ),
        annotations=READ_ONLY,
    )
    def search_product_versions(
        keyword: str | None = None,
        product_name: str | None = None,
        status: list[str] | str | None = None,
        release_date_from: str | None = None,
        release_date_to: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """搜索产品版本。

        Args:
            keyword: 版本号或名称关键字，对应 API 的 search 参数。
            product_name: 精确的产品名称，对应 API 的 product_name 参数。
            status: 版本状态，可选 DRAFT、RELEASED、ARCHIVED；可传单个值或列表。
            release_date_from: 发布日期下限，格式 YYYY-MM-DD，包含当天。
            release_date_to: 发布日期上限，格式 YYYY-MM-DD，包含当天。
            page: 页码，从 1 开始。
            page_size: 每页条数，最大 100。
        """
        try:
            with factory() as client:
                return client.search(
                    keyword=keyword,
                    product_name=product_name,
                    statuses=_coerce_status(status),
                    release_date_from=release_date_from,
                    release_date_to=release_date_to,
                    page=page,
                    page_size=page_size,
                )
        except (ConfigError, VersionApiError) as exc:
            raise ToolError(_error_text(exc)) from exc

    @server.tool(
        name="get_product_version",
        description="按版本 UUID 查询单个产品版本的完整信息。",
        annotations=READ_ONLY,
    )
    def get_product_version(version_id: str) -> dict[str, Any]:
        """获取产品版本详情。

        Args:
            version_id: 版本 UUID，可从 search_product_versions 的结果中获取。
        """
        try:
            with factory() as client:
                return client.get(version_id)
        except (ConfigError, VersionApiError) as exc:
            raise ToolError(_error_text(exc)) from exc

    return server


def _error_text(exc: ConfigError | VersionApiError) -> str:
    if isinstance(exc, VersionApiError):
        return exc.as_text()
    return f"config_error: {exc}"


mcp = create_server()
