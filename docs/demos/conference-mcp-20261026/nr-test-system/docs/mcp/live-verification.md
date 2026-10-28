# 产品版本查询 MCP 真实协议验证记录

- 任务：T-20261026-149C47
- 执行时间：2026-10-26T11:20:53Z
- 环境：本地开发环境 `http://127.0.0.1:8000/api/v1`
- 后端：本仓库 Django 5.2 + DRF，SQLite 隔离库（`.runtime/mcp-live/db.sqlite3`）
- MCP：`nr-product-version` 0.1.0，stdio 传输
- 协议版本：`2025-11-25`
- 原始记录：[`live-verification.json`](live-verification.json)

## 验证方式

使用真实 MCP 客户端 `mcp.client.stdio.stdio_client` + `ClientSession`：

1. 通过既有 `/api/v1/auth/login/` 获取真实 Session，Cookie 仅保存在内存并通过子进程环境变量
   注入 MCP Server；未打印、未写入文件。
2. 通过既有版本 API 准备 4 条本地联调数据（2 个产品、3 种状态）。
3. 完成 MCP `initialize` 握手并 `tools/list`。
4. 依次调用 `list_versions` 搜索、筛选分页、`get_version` 详情与一个非法参数错误分支。
5. 对每次成功调用，用同一会话直接请求对应 HTTP API，并比较返回 JSON 是否完全一致。

复现命令（使用本地测试账号，不要把真实凭据写入文件）：

```bash
cd test_system_mcp
NR_MCP_API_BASE_URL=http://127.0.0.1:8000/api/v1 \
NR_MCP_TEST_USERNAME=... \
NR_MCP_TEST_PASSWORD=... \
uv run python scripts/live_mcp_check.py \
  --output ../docs/mcp/live-verification.json
```

## 握手与工具发现

```json
{
  "protocol_version": "2025-11-25",
  "server_info": {
    "name": "nr-product-version",
    "title": "NR 产品版本查询",
    "version": "0.1.0"
  }
}
```

发现工具：`list_versions`、`get_version`。两者均标注
`read_only_hint=true`、`destructive_hint=false`、`idempotent_hint=true`。

## 工具调用结果

### 1. 搜索 + 分页

调用参数：`{"search": "NR", "page": 1, "page_size": 2}`

真实输出摘要：

```json
{
  "count": 4,
  "page": 1,
  "page_size": 2,
  "results": [
    {
      "product_name": "NR 其他产品",
      "version": "v2.0.0",
      "status": "ARCHIVED"
    },
    {
      "product_name": "NR 测试套件",
      "version": "v1.3.0",
      "status": "DRAFT"
    }
  ]
}
```

与直接 HTTP 调用 `GET /api/v1/system/versions/?search=NR&page=1&page_size=2` 的完整 JSON
比较：**一致**。

### 2. 筛选 + 分页元信息

调用参数：

```json
{"product_name": "NR 测试套件", "status": "RELEASED", "page": 1, "page_size": 20}
```

真实输出摘要：

```json
{
  "count": 2,
  "page": 1,
  "page_size": 20,
  "results": [
    {"version": "v1.2.1", "status": "RELEASED"},
    {"version": "v1.2.0", "status": "RELEASED"}
  ]
}
```

与直接 HTTP 调用比较：**一致**。

### 3. 版本详情

调用参数：`{"version_id": "d3d82e3d-2910-4942-b8dd-d35997082e26"}`

真实输出字段：

```text
id, product_name, version, status, release_date, description,
is_default, created_by_name, updated_by_name, created_at, updated_at
```

与直接 HTTP 调用 `GET /api/v1/system/versions/{uuid}/` 比较：**一致**。

### 4. 错误分支

调用参数：`{"page": 0}`

真实返回 `isError=true`，文本为：

```json
{
  "code": "validation_error",
  "message": "page 必须是大于等于 1 的整数",
  "details": {"page": "必须为 >= 1 的整数"}
}
```

## 结论

- 真实 MCP 客户端握手成功，协议版本 `2025-11-25`。
- 两个只读工具可被真实调用。
- 搜索、筛选、分页与详情输出均与既有 HTTP API 完全一致。
- 非法参数在本地被拒绝，未发起上游请求。
- 验证过程未写入、未打印任何会话 Cookie。
