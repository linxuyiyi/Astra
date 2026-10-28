# 实施简报：NR 测试系统产品版本查询 MCP

- 任务编号：T-20261026-149C47
- 日期：2026-10-26
- 类型：测试平台需求（新功能）

## 人工确认结论（开发前）

开发开始前已通过 OpenCode 原生提问与用户确认访问范围，结论如下：

| 确认项 | 用户结论 |
|---|---|
| 环境 / API 基地址 | 本地开发环境 `http://127.0.0.1:8000`（API 前缀 `/api/v1`） |
| 可访问产品与版本范围 | 全部产品 / 版本，只读 |
| 认证数据来源与使用方式 | 通过环境变量注入既有会话 Cookie，只读；MCP 不登录、不写入、不落盘凭据 |

由此确定：

- MCP 只暴露查询类工具，不提供创建、修改、归档或删除能力。
- 复用现有 `GET /api/v1/system/versions/` 与 `GET /api/v1/system/versions/{id}/`，不新增后端接口。
- 复用 Session + CSRF 认证与 `system.version.view` 权限；MCP 只转发会话，不提升权限。
- 基地址与会话均来自环境变量，仓库内不出现真实凭据。

## 目标与价值

- 用户：日常测试与排查人员。
- 当前问题：产品版本需要登录网页手工检索，缺少可供 MCP 客户端直接调用的查询入口。
- 可观察结果：MCP 客户端可搜索、筛选、分页获取版本列表，并按版本标识获取完整详情。

## 验收场景

### Scenario: 关键字搜索版本列表

- **GIVEN** 后端已存在多个产品版本且会话具备 `system.version.view`
- **WHEN** MCP 客户端调用 `list_versions(search="NR")`
- **THEN** 返回与直接调用 `GET /api/v1/system/versions/?search=NR` 一致的 `count/page/page_size/results`

### Scenario: 筛选与分页

- **GIVEN** 不同产品、不同状态的版本数据
- **WHEN** 客户端传入 `product_name`、`status`、`page`、`page_size`
- **THEN** 结果只包含匹配项，并返回分页元信息；非法页码/每页条数在本地被拒绝

### Scenario: 版本详情

- **GIVEN** 已知一个版本 UUID
- **WHEN** 客户端调用 `get_version(version_id=...)`
- **THEN** 返回与直接调用详情接口一致的完整字段

### Scenario: 错误分支

- **GIVEN** 会话缺失、无权限、版本不存在、上游不可用或参数非法
- **WHEN** 客户端调用工具
- **THEN** 返回稳定错误码与中文说明，不泄露栈信息与凭据

## 范围

- 范围内：新增独立 MCP 工程、只读 HTTP 客户端、两个 MCP 工具、自动化测试、真实 MCP 协议验证、使用说明。
- 范围外：任何后端接口新增/修改、写操作、凭据管理服务、生产部署、远端推送。

## 关键设计

- 目录：根目录新增 `test_system_mcp/`，Python 3.12，官方 MCP Python SDK（stdio 传输）+ `httpx`。
- 工具：
  - `list_versions(search?, product_name?, status?, page?, page_size?)`
  - `get_version(version_id)`，`version_id` 为版本 UUID
- HTTP：`NR_MCP_API_BASE_URL` 默认 `http://127.0.0.1:8000/api/v1`；
  `NR_MCP_SESSION_COOKIE` 注入 `Cookie` 请求头；超时 `NR_MCP_TIMEOUT_SECONDS` 默认 15。
- 参数校验：`page >= 1`、`1 <= page_size <= 100`、`status ∈ {DRAFT, RELEASED, ARCHIVED}`、`version_id` 为 UUID。
- 错误映射：配置缺失、参数错误、401、403、404、400、超时/网络、上游异常，均转为结构化工具错误。
- 测试：`pytest` + `httpx.MockTransport` 覆盖四类能力与错误分支；另提供真实 MCP 客户端脚本做端到端验证。

## 可验证步骤

- [x] 先写失败测试（RED）
- [x] 实现最小行为并通过目标测试（GREEN）
- [x] 运行完整 pytest 与 Ruff
- [x] 使用本地 Django + 真实会话完成 MCP 握手与工具调用
- [x] 记录真实输出到 `docs/mcp/live-verification.md`

## 未知项与决策点

- 仓库当前没有既有 MCP 工程，因此按仓库 Python 工程约定新建 `test_system_mcp/`；若后续出现统一 MCP 规范，需要迁移。
- 真实联调使用本地 SQLite 开发库，仅用于验证，不改动 MySQL 或远端 schema。

## 验证结果

- 自动化测试：53 个用例通过（搜索、筛选、分页、详情、配置、参数边界、401/403/404/400/429/5xx、超时、网络、非法响应）。
- Ruff：`check` 与 `format --check` 通过。
- 真实 MCP：stdio 握手成功（协议 `2025-11-25`），`list_versions` 搜索/筛选分页与 `get_version` 详情均与直接 HTTP API 返回一致，非法 `page` 返回结构化 `validation_error`。
- 证据：[`live-verification.md`](live-verification.md) 与 [`live-verification.json`](live-verification.json)。
