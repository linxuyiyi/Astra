## Why

日程由 AgentScope 独立创建 Session，绕过员工资源同步入口，导致日程无法使用已绑定的 Skill 和 MCP。需要保证日程首次推理前具备员工已授权能力，复用同一资源收敛规则。

## What Changes

- 为日程 Session 在每次运行前同步员工资源，并在工具装配时应用员工权限和激活允许的工具组。
- 校验官方 Session 的日程来源、安排者身份及员工绑定；准备失败通过官方运行错误报告，不继续空能力推理。
- 支持平台资源库向隔离日程工作区安装资源，以及日程 Task MCP 的可信来源鉴权。
- 不复制调度器、会话或记忆，不修改 Library MCP 凭据，不主动重放生产日程。

## Capabilities

### New Capabilities

- `nexus-schedule-session-resources`: 日程执行前的员工资源收敛、权限和隔离。

### Modified Capabilities

无。现有日程 capability 仍在独立 change 中，本变更补充资源准备契约。

## Impact

涉及 Nexus 员工资源同步、内部日程接口、Task MCP 身份校验及 Agent Service 启动适配。继续使用 AgentScope 2.0.10 官方存储、工作区 API、scheduler、inbox 和运行状态，无 schema 变更。
