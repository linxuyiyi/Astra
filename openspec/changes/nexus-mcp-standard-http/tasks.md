## 1. MCP 服务与鉴权

- [x] 1.1 Task MCP 中间件只写入来源上下文；`initialize`/`tools/list` 无业务上下文仍成功，工具调用执行原 `authorize_task_source`。
- [x] 1.2 Schedule MCP 移除共享 key 和会话前置拒绝；工具调用在缺少来源或内部 key 时返回明确错误。
- [x] 1.3 Library MCP 移除静态 Bearer verifier；保留 DNS rebinding 保护、Worker 网段入口和 `library_*` 业务校验。
- [x] 1.4 删除 `NEXUS_MCP_API_KEY`、`NEXUS_TASK_MCP_KEY`、`NEXUS_SCHEDULE_MCP_KEY` 的校验、注入、安装器生成和 Worker 配置写入。

## 2. 注册与同步

- [x] 2.1 Task/Schedule 继续通过 AgentScope 官方 `/workspace/mcp` 挂接；地址和来源头变化时替换，重复同步复用。
- [x] 2.2 任务技能上传与 MCP 注册分开；注册后经 `/workspace/mcp` 实际核对 `is_healthy` 和 `tools`。
- [x] 2.3 同步返回技能上传、注册、连接/发现、收回阶段；错误写入员工 capability 的 `error` 和 `task` 字段。
- [x] 2.4 取消任务能力时收回所有会话的技能和 MCP；再次启用时重新挂接，邮箱不保留 `nexus-task`。

## 3. 部署

- [x] 3.1 统一配置由 MCP 监听地址和端口生成 URL，默认回环；旧 MCP key 在升级时静默清理。
- [x] 3.2 生命周期验收对 Library/Task/Schedule 实际执行 MCP `initialize` 和 `tools/list`。
- [x] 3.3 部署后通过真实 AgentScope 工作区注册 Task/Schedule，核对健康与工具，删除临时 Agent/Session。
- [x] 3.4 任一 MCP 或 AgentScope 验收失败时返回非零并标明失败阶段。

## 4. 验证

- [x] 4.1 针对性测试：Task/Schedule/Library MCP 无 key 初始化和发现、无活跃讨论同步、缺少业务上下文工具错误、URL 变化、服务不可达、取消再启用、员工 capability 展示。
- [x] 4.2 回归：`tests/platform` 与 `tests/nexus` 全部通过；Agent Service 测试；Ruff。
- [x] 4.3 真实进程验收：启动三个 MCP，执行标准 initialize/tools/list；启动临时 AgentScope 2.0.10，真实注册 Task/Schedule 并核对工具，验收后删除 Agent/Session。
- [ ] 4.4 公司 Linux root 首次安装、MySQL 8.0.36、Nginx/TLS 与真实 Worker 回连未在本机执行，保留为未验证。
