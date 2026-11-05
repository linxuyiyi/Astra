## Why

任务和 Worker 管理页缺少完整的查询和操作入口。MCP 库登记依赖员工工作区写入，造成无绑定时无法登记和日程身份重复登记。

## What Changes

- 对齐任务、Worker 与维护页面，提供实际分页、操作入口和顺序日志。
- 修复任务绑定到原生工作区的跳转。
- 将 MCP 库登记独立于工作区挂载，给 Nexus 自建 MCP 提供明确的同步和失败反馈。

## Capabilities

### New Capabilities

- `nexus-admin-operations` 管理列表和维护日志。
- `nexus-mcp-registry` 平台 MCP 登记、会话挂载和部署登记。

### Modified Capabilities

无。

## Impact

涉及 Nexus 管理页、Task 和维护查询接口、Agent Service 内部接口及部署验收。
