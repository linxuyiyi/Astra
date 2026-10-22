## Why

员工目前只能在页面由人类代为创建日程，私聊里无法把“每周一提醒我”这类安排落成官方日程。AgentScope 自带的 `Schedule*` 工具被 Nexus 主动移除；直接放开会让记录落到平台身份 `nexus`，人类页面看不到且可能跨人可见。需要一条绑定当前私聊会话归属的 MCP 工具链。

## What Changes

- 新增日程 MCP 服务，暴露创建、列表、查看三个工具；只安装到私聊会话，按会话头识别员工与会话。
- Nexus 新增内部日程接口：从 AgentScope `session_id` 解析当前私聊人类，校验员工启用与归属，再以人类日程命名空间调用官方接口。
- MCP 创建默认不启用；名称、内容、五字段 cron、时区由 Nexus/官方校验；日程仍由 AgentScope 存储、计时器触发。
- 团队、邮箱、未知会话、无内部密钥的调用一律拒绝。

## Capabilities

### New Capabilities
- `nexus-employee-schedule-mcp`: 数字员工在私聊中通过 MCP 工具创建、查看当前人类的日程。

### Modified Capabilities
无。不修改 Astra 历史契约。

## Impact

涉及 Nexus 内部日程接口、日程 MCP 服务、私聊 MCP 安装与权限允许、开发/部署配置和测试。不修改 AgentScope schema，不新增日程表或计时器。MCP 走官方 REST 创建，官方 `source` 记为 `USER`，不保留 `source=AGENT`；页面仍按人类命名空间隔离。
