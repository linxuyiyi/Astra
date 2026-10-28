import json
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import VersionApiClient
from .config import ConfigurationError, Settings
from .errors import ApiError, ToolInputError
from .service import VersionQueryService

INSTRUCTIONS = (
    "NR 测试系统产品版本查询 MCP，只读。"
    "复用既有 /api/v1/system/versions/ 接口与 Session 认证；"
    "不提供任何创建、修改、归档或删除能力。"
)

READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

mcp = MCPServer(
    name="nr-product-version",
    title="NR 产品版本查询",
    description="按关键字、产品与状态检索产品版本，并查看版本详情。",
    instructions=INSTRUCTIONS,
    version="0.1.0",
)


def _run(operation):
    settings = Settings.from_env()
    client = VersionApiClient(
        base_url=settings.api_base_url,
        session_cookie=settings.session_cookie,
        timeout_seconds=settings.timeout_seconds,
    )
    try:
        return operation(VersionQueryService(client))
    finally:
        client.close()


def _to_error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, (ApiError, ToolInputError)):
        return exc.to_dict()
    return {
        "code": getattr(exc, "code", "internal_error"),
        "message": str(exc),
        "details": {},
    }


def _raise_tool_error(exc: Exception) -> None:
    raise ToolError(json.dumps(_to_error_payload(exc), ensure_ascii=False)) from exc


@mcp.tool(
    name="list_versions",
    description=(
        "查询产品版本列表。支持关键字搜索、按产品名/状态筛选与分页；"
        "返回 count、page、page_size、results，字段与既有 HTTP API 一致。"
    ),
    annotations=READ_ONLY_ANNOTATIONS,
)
def list_versions(
    search: Annotated[
        str | None,
        Field(description="关键字，匹配产品名或版本号（对应 HTTP search 参数）"),
    ] = None,
    product_name: Annotated[
        str | None,
        Field(description="产品名精确匹配（忽略大小写，对应 HTTP product_name 参数）"),
    ] = None,
    status: Annotated[
        str | None,
        Field(description="版本状态，只能是 DRAFT、RELEASED、ARCHIVED"),
    ] = None,
    page: Annotated[int, Field(description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Field(description="每页条数，1..100")] = 20,
) -> dict[str, Any]:
    try:
        return _run(
            lambda service: service.list_versions(
                search=search,
                product_name=product_name,
                status=status,
                page=page,
                page_size=page_size,
            )
        )
    except (ToolInputError, ApiError, ConfigurationError) as exc:
        _raise_tool_error(exc)


@mcp.tool(
    name="get_version",
    description=("按版本 UUID 查询单个产品版本的完整详情，字段与既有 HTTP 详情接口一致。"),
    annotations=READ_ONLY_ANNOTATIONS,
)
def get_version(
    version_id: Annotated[
        str,
        Field(description="产品版本 UUID，例如 11111111-1111-4111-8111-111111111111"),
    ],
) -> dict[str, Any]:
    try:
        return _run(lambda service: service.get_version(version_id))
    except (ToolInputError, ApiError, ConfigurationError) as exc:
        _raise_tool_error(exc)
