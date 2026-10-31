## Why

Nexus 内置的 Task/Schedule MCP 已经是独立 HTTP 进程，但 Task MCP 在初始化和工具发现阶段就校验当前会话、群讨论和租约，导致没有进行中讨论时 AgentScope 工作区同步失败。Library/Task/Schedule 还共享部署生成的 Bearer key，挂接失败又只记 warning，员工配置页可能仍显示同步成功。

## What Changes

- 内置 MCP 继续使用 AgentScope 官方 `/workspace/mcp` 按会话挂接；连接和工具发现只带入会话头，员工、会话、任务归属和群讨论/日程来源在工具调用时校验。
- 删除 Task/Schedule/Library MCP 的共享 Bearer key 校验、自动注入、安装器生成和 Worker 配置注入；保留业务权限。
- Task 技能上传与 MCP 连接分开执行；同步后经 AgentScope 工作区接口核对连接与工具发现，并把注册、连接/发现失败写入员工 capability。
- 部署器按统一地址/端口生成 MCP URL，旧 key 配置在升级时静默清理；启动验收实际执行 `initialize`/`tools/list`，并通过真实 AgentScope 注册 Task/Schedule MCP 后删除临时校验 Agent/Session。

## Capabilities

### New Capabilities

- `nexus-builtin-mcp`: 内置 MCP 的标准 HTTP 注册、发现、业务鉴权、同步状态和部署验收。

### Modified Capabilities

无。现有员工、任务、日程和 Runtime 状态机不变。

## Impact

涉及 `services/nexus-api` 的 MCP 服务、AgentScope 客户端、员工能力核对和部署验收，以及 `ops/deploy` 的配置与生命周期。不新增数据库表、鉴权框架、代理层或第二套 MCP 注册系统；不改动 Worker SSH 传输逻辑。
