# NR 产品版本查询 MCP

供 NR 测试系统测试人员使用的只读 MCP 服务。它通过现有 HTTP API 查询产品版本，
不新增或修改后端接口，不执行任何写操作。

## 能力与工具

| 能力 | 工具 | 说明 |
|---|---|---|
| 版本搜索 | `search_product_versions` | 关键字匹配产品名或版本号（API `search`） |
| 筛选 | `search_product_versions` | `product_name`、`status`、`release_date_from/to` |
| 分页 | `search_product_versions` | `page` 从 1 开始，`page_size` 默认 20、最大 100；响应含 `count/page/page_size/results` |
| 版本详情 | `get_product_version` | 按版本 UUID 返回完整版本信息 |

两个工具都声明为只读（`readOnlyHint=true`）。

现有 HTTP API 原生只支持 `search`、`product_name`、单个 `status`：
`test_system_server/apps/versions/api/views.py`。因此：

- 请求多个状态，或使用发布日期筛选时，MCP 会分状态读取匹配分页并在本地聚合，
  响应中标记 `client_side_filtering: true`；
- 聚合读取受 `NR_MAX_FETCH_PAGES` 保护（默认 50 页 × 100 条）；
- `release_date_from/to` 使用 `YYYY-MM-DD`，包含边界；无发布日期的版本在日期筛选下会被排除。

## 默认版本状态范围

业务用户已于 2026-10-28 确认：调用方未显式指定 `status` 时，**默认仅查询
`RELEASED`（已发布）**。草稿和归档版本不会出现在默认结果中。

该默认值可通过环境变量覆盖，调用方显式传 `status` 时始终以调用方为准：

```bash
# 默认：仅已发布
NR_DEFAULT_STATUSES=RELEASED

# 示例：已发布 + 草稿
NR_DEFAULT_STATUSES=RELEASED,DRAFT
```

支持的状态：`DRAFT`、`RELEASED`、`ARCHIVED`。

## 配置

| 环境变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `NR_SESSION_COOKIE` | 是 | 无 | 登录会话 Cookie。可传完整 Cookie 头，或只传 sessionid 值 |
| `NR_SESSION_COOKIE_NAME` | 否 | `sessionid` | 只传 Cookie 值时使用的名称 |
| `NR_API_BASE_URL` | 否 | `http://127.0.0.1:8000/api/v1` | 只允许回环地址 |
| `NR_DEFAULT_STATUSES` | 否 | `RELEASED` | 未显式传 status 时的默认状态范围 |
| `NR_REQUEST_TIMEOUT` | 否 | `15` | HTTP 超时秒数 |
| `NR_MAX_PAGE_SIZE` | 否 | `100` | 单页最大条数 |
| `NR_MAX_FETCH_PAGES` | 否 | `50` | 本地聚合时每个状态的最大读取页数 |

安全边界：

- 鉴权 Cookie **只从环境变量读取**，不写入仓库、不打印到日志或工具错误；
- `NR_API_BASE_URL` 仅允许 `127.0.0.1`、`localhost`、`::1`，其他地址（含生产域名）
  会在配置阶段被拒绝；
- 客户端设置 `trust_env=False`，不经过系统代理，避免 Cookie 被转发到外部；
- 仅使用 GET，不发送任何写请求。

## 安装与启动

```bash
python3 -m venv .venv
.venv/bin/pip install -e "mcp_server[dev]"

NR_SESSION_COOKIE="sessionid=<从本地登录获得>" \
  .venv/bin/python -m nr_version_mcp
```

上面的命令以 stdio 方式启动 MCP。客户端配置示例：

```json
{
  "mcp": {
    "nr-product-version": {
      "type": "local",
      "command": ["/absolute/path/to/.venv/bin/python", "-m", "nr_version_mcp"],
      "environment": {
        "NR_API_BASE_URL": "http://127.0.0.1:8000/api/v1",
        "NR_DEFAULT_STATUSES": "RELEASED"
      },
      "enabled": true
    }
  }
}
```

`NR_SESSION_COOKIE` 应由启动 MCP 的进程环境注入，不要写进仓库中的配置文件。

## 本地演示环境

演示只使用 SQLite 与现有 Django 代码，不修改后端接口。

```bash
# 1. 安装演示依赖
.venv/bin/pip install -e "mcp_server[demo]"

# 2. 初始化本地 SQLite 演示库（.runtime 已被 .gitignore 忽略）
mkdir -p .runtime
DATABASE_ENGINE=sqlite SQLITE_PATH=.runtime/demo.sqlite3 \
  .venv/bin/python test_system_server/manage.py migrate

# 3. 写入演示版本与演示用户（密码只从环境变量读取）
NR_DEMO_USERNAME=demo NR_DEMO_PASSWORD="<本地演示密码>" \
  .venv/bin/python mcp_server/demo/seed_demo.py

# 4. 启动本地演示服务
DATABASE_ENGINE=sqlite SQLITE_PATH=.runtime/demo.sqlite3 \
  .venv/bin/python test_system_server/manage.py runserver 127.0.0.1:8000

# 5. 另一个终端：通过真实 MCP stdio 调用验证四个能力
NR_DEMO_USERNAME=demo NR_DEMO_PASSWORD="<本地演示密码>" \
  .venv/bin/python mcp_server/demo/verify_demo.py
```

`verify_demo.py` 在内存中完成登录，把 sessionid 通过子进程环境变量传给 MCP，
只输出脱敏后的查询结果，不输出 Cookie。

## 测试

```bash
cd mcp_server
../.venv/bin/pytest -q
../.venv/bin/ruff check .
```

测试覆盖：

- 默认状态、配置覆盖与回环地址限制；
- 关键字、产品名、状态、日期筛选和分页参数；
- 多状态聚合与结果过多保护；
- 401/404/连接失败/非法响应映射；
- 通过真实 MCP stdio 会话调用搜索与详情，验证 Cookie 注入、只读工具声明和错误脱敏。
