## 1. 契约与归属

- [x] 1.1 新建 OpenSpec change，写明 MCP 创建/列表/查看、私聊限定、默认不启用和 `source=USER` 边界。
- [x] 1.2 实现 Nexus 内部日程接口与 session→人类解析；验证私聊成功、团队/邮箱 404、员工不匹配 409、无内部密钥 401。验证：`test_schedules.py::test_internal_schedule_is_scoped_to_private_session_owner`。

## 2. MCP 服务

- [x] 2.1 实现 `nexus-schedule` MCP 服务：鉴权中间件、`schedule_create`、`schedule_list`、`schedule_view`；验证工具参数、默认 `enabled=false` 和错误映射。验证：`test_schedule_mcp.py` 6 passed；真实服务 `/health`、401、initialize、tools/list 通过。
- [x] 2.2 私聊 Session 创建/消息时安装 MCP，headers 带 employee/session/conversation；验证已存在会话可补装、MCP 不可用不阻塞聊天。验证：`test_live_client_posts_chat_without_writing_a_reply`、隔离 E2E。

## 3. 权限与部署

- [x] 3.1 在 Agent Service 对 `mcp__nexus-schedule__` 前缀显式 ALLOW，任务 MCP 行为不变；验证权限决策。验证：`test_dont_ask_allows_platform_mcp_tools`。
- [x] 3.2 补齐 dev/deploy 端口、密钥、环境、启动与健康检查；验证模板测试和 `/health`。验证：`test_service_gateway.py`、部署测试 33 passed；MCP `/health` 200。

## 4. 验证闭环

- [x] 4.1 跑 Nexus 针对性/回归、Agent Service、前端测试、类型检查/构建、Ruff、OpenSpec strict。验证：Nexus 136 passed/4 skipped、Agent 13 passed/1 skipped、前端 30 passed、`vue-tsc`、Ruff、OpenSpec strict。
- [x] 4.2 用隔离 Agent Service + 模型桩验证“私聊 → MCP 创建 → 页面出现”；模型桩未自行选择工具，tools/call 由测试脚本发起；墙钟触发未验证。证据：`mcp-e2e.json`。
- [ ] 4.3 更新架构/验收/交接文档，提交并合入 v1.4.8。
