# MCP Python SDK 接口迁移：先核对版本，再验证真实调用

## 适用场景

已有示例与实际安装的 MCP Python SDK 接口不一致，需要开发或维护 Python MCP 服务。本次实际安装版本为 **mcp 2.3.0**；结论限定于本次验证版本，不外推到所有 SDK 或未来版本。

## 本次问题与改动

按旧示例使用 `FastMCP` 或按 camelCase 访问 Python 对象属性时，出现了导入或属性访问错误。本次实现使用 `mcp.server.mcpserver.MCPServer`，并通过实际安装包核对接口签名。

Python 对象的工具 schema、结果与注解使用对应属性：`input_schema`、`structured_content`、`is_error`、`read_only_hint`、`destructive_hint`。这些是 Python 对象属性名，不能据此外推其他语言 SDK 或 JSON 协议字段的命名规则。

## 可复用验证方法

1. 先记录实际 SDK 版本，再核对导入路径、函数签名和对象字段，不在新旧接口之间堆叠猜测性兼容分支。
2. 单测检查工具注册、只读注解、参数校验与错误映射。
3. 使用真实 `stdio_client + ClientSession` 完成握手、发现工具并调用工具。
4. 对每个正向调用，断言其结构化结果与直接 HTTP 查询结果一致；对负向调用，按预期断言 `is_error` 和具体错误码。负向用例被正确拒绝是验证成功，不能仅因出现错误标志就判定联调失败，也不能仅看进程退出码。
5. 测试夹具可以在隔离环境完成登录和数据准备；MCP 服务本身仍只从环境变量读取会话 Cookie、只发 GET。测试脚本登录与 MCP 服务认证边界应分别核实。

## 本轮证据

- 来源任务：`T-20261026-149C47`。
- 原生 Session：`ses_e8826d641ffen4XzCS49w167og`。
- 交付提交：`350207a78603035c060f038d5456835cb4f228ff`，未推送。
- 独立复跑：MCP 测试 **53 passed**；已有版本 API 回归 **9 passed**；Ruff 与 Django check 通过。
- 真实协议协商：`2025-11-25`，工具 `list_versions` 与 `get_version`。
- 搜索、筛选、详情三次正向调用均与直接 HTTP 响应一致；`page=0` 按预期返回 `validation_error`。
- 本轮独立验证记录：`independent-verification.json`、`independent-live-mcp.json`；Worker 的可复现脚本与原始调用记录见该提交中的 `test_system_mcp/scripts/live_mcp_check.py` 和 `docs/mcp/live-verification.json`。

## 边界

本轮使用本地 Django、SQLite 和自建演示数据。没有验证公司 MySQL、生产网络、生产账号权限或发布部署。MCP 的查询只读；测试脚本的夹具初始化不是 MCP 写能力。临时联调服务结束后会关闭，独立复跑需要按同一配置临时启动，测完再次关闭。

## 审核说明

本正文根据原生工具证据、最终代码和独立复跑修订。原始候选中的中间测试数量不代表最终数量；协议负向用例、测试登录与 MCP Cookie 注入分别归因，避免将片段推测发布为事实。
