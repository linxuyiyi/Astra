# NR 产品版本查询 MCP

面向日常测试与排查的**只读** MCP 服务。它复用 NR 测试系统既有的 HTTP API、Session 认证与
权限模型，不新增后端接口，不提供任何写操作。

- 传输：stdio
- 实现：Python 3.12 + 官方 MCP Python SDK 2.x + httpx
- 上游接口：
  - `GET /api/v1/system/versions/`（列表、搜索、筛选、分页）
  - `GET /api/v1/system/versions/{uuid}/`（详情）
- 权限：沿用登录会话的 `system.version.view`

## 工具清单

### `list_versions`

查询产品版本列表，返回与 HTTP API 一致的分页结构。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `search` | string | 否 | - | 关键字，匹配产品名或版本号 |
| `product_name` | string | 否 | - | 产品名精确匹配（忽略大小写） |
| `status` | string | 否 | - | `DRAFT` / `RELEASED` / `ARCHIVED` |
| `page` | integer | 否 | `1` | 页码，必须 `>= 1` |
| `page_size` | integer | 否 | `20` | 每页条数，必须 `1..100` |

返回：

```json
{
  "count": 4,
  "page": 1,
  "page_size": 2,
  "results": []
}
```

### `get_version`

按版本 UUID 查询单个版本的完整详情。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `version_id` | string | 是 | 产品版本 UUID |

返回字段：`id`、`product_name`、`version`、`status`、`release_date`、`description`、
`is_default`、`created_by_name`、`updated_by_name`、`created_at`、`updated_at`。

## 认证配置

MCP 侧不登录、不硬编码凭据、不扩大权限，只把既有会话 Cookie 原样转发给上游 API。

| 环境变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `NR_MCP_API_BASE_URL` | 否 | `http://127.0.0.1:8000/api/v1` | 既有 API 基地址，需包含 `/api/v1` |
| `NR_MCP_SESSION_COOKIE` | 是 | - | 既有会话 Cookie，只读使用，例如 `sessionid=...` |
| `NR_MCP_TIMEOUT_SECONDS` | 否 | `15` | 上游请求超时秒数 |

获取会话的方式与现有平台一致（例如通过 `/api/v1/auth/login/` 登录，或从已登录浏览器会话
复制 `sessionid`）。**不要**把真实 Cookie 写入仓库、文档或提交记录。只读工具均为 `GET`，
无需携带 CSRF Token；写操作本服务不提供。

## 运行

使用 `uv`（与仓库 Python 工程约定一致）：

```bash
cd test_system_mcp
uv sync --extra dev
NR_MCP_API_BASE_URL=http://127.0.0.1:8000/api/v1 \
NR_MCP_SESSION_COOKIE='sessionid=...' \
uv run nr-version-mcp
```

没有 `uv` 时：

```bash
cd test_system_mcp
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
NR_MCP_API_BASE_URL=http://127.0.0.1:8000/api/v1 \
NR_MCP_SESSION_COOKIE='sessionid=...' \
.venv/bin/python -m nr_version_mcp
```

### MCP 客户端配置示例

```json
{
  "mcpServers": {
    "nr-product-version": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "/absolute/path/to/repo/test_system_mcp",
        "nr-version-mcp"
      ],
      "env": {
        "NR_MCP_API_BASE_URL": "http://127.0.0.1:8000/api/v1",
        "NR_MCP_SESSION_COOKIE": "sessionid=<从既有登录会话注入>"
      }
    }
  }
}
```

## 示例调用

```json
{"name": "list_versions", "arguments": {"search": "NR", "page": 1, "page_size": 20}}
```

```json
{"name": "list_versions", "arguments": {"product_name": "NR 测试套件", "status": "RELEASED"}}
```

```json
{"name": "get_version", "arguments": {"version_id": "11111111-1111-4111-8111-111111111111"}}
```

## 错误处理

工具错误以 MCP `isError` 返回，文本为 JSON，包含稳定 `code`、中文 `message` 和 `details`。

| `code` | 触发条件 |
|---|---|
| `configuration_error` | 缺少会话 Cookie、基地址或超时配置非法 |
| `validation_error` | 页码/每页条数/状态/UUID 非法，或上游返回 400 |
| `unauthenticated` | 上游 401，会话无效或过期 |
| `forbidden` | 上游 403，缺少 `system.version.view` |
| `not_found` | 上游 404，版本不存在 |
| `upstream_timeout` | 请求上游超时 |
| `upstream_unavailable` | 无法连接上游 |
| `upstream_rate_limited` | 上游 429 |
| `upstream_invalid_response` | 上游响应无法解析或分页结构不完整 |
| `upstream_error` | 上游 5xx 或其他失败 |

## 测试

```bash
cd test_system_mcp
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

真实 MCP 协议联调（本地 Django + 真实会话）见
[`../docs/mcp/live-verification.md`](../docs/mcp/live-verification.md)。联调脚本：

```bash
NR_MCP_API_BASE_URL=http://127.0.0.1:8000/api/v1 \
NR_MCP_TEST_USERNAME=... \
NR_MCP_TEST_PASSWORD=... \
uv run python scripts/live_mcp_check.py --output ../docs/mcp/live-verification.json
```

脚本在内存中登录获取会话，只通过子进程环境变量传递 Cookie，不打印、不落盘。

## 边界

- 只读：仅 `GET`，无创建、修改、归档、删除工具。
- 不新增/修改后端接口、数据库 schema 或权限。
- 详情按 UUID 查询；按版本号检索请使用 `list_versions` 的 `search`。
- API 基地址必须包含 `/api/v1`。
