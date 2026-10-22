## Context

现有日程页由人类在 `/schedules` 创建，Nexus 把可信 UID 映射为 `nexus-schedule-<sha256(uid)>`。员工聊天 Session 统一使用平台身份 `nexus`，`agent_service.py` 又主动移除了官方 `ScheduleCreate/List/View/Delete`。因此 MCP 工具不能直接以聊天 Session 身份写日程，必须由 Nexus 把会话映射回人类命名空间。

## Goals / Non-Goals

目标：私聊中由员工通过 MCP 创建、查看当前人类的周期日程；归属只由会话决定；默认不启用；官方存储和触发不变。

非目标：团队/邮箱/渠道创建；MCP 编辑或删除；一次性提醒；Skill/MCP/知识库继承；`source=AGENT` 审计；外部无会话客户端直接调用。

## Decisions

- **MCP 服务独立进程**，不复用官方 `Schedule*` Agent 工具，也不把 MCP 挂进 Nexus API 进程。私聊创建/首次消息时由 Nexus 用 `add_workspace_mcp()` 安装，headers 带 employee/session/conversation 和共享密钥。
- **归属解析在 Nexus**：新增 `GET/POST /api/v1/internal/schedules` 系列接口，按 `t_nexus_conversations.agentscope_session_id` + `kind='private'` 查 `owner_id`，并要求 `X-Nexus-Employee-Id` 与该会话员工一致。MCP 工具不接受 `user_id`、`employee_id`、模型或凭据参数。
- **写路径复用官方 REST**：Nexus 以 `schedule_user_id(owner)` 调官方 `/schedule/`；官方仍然是日程和执行的唯一事实源。代价是来源为 `USER`、没有 `source_session_id`，由 Nexus 日志记录 MCP 调用。
- **默认不启用**：MCP `schedule_create` 的 `enabled` 默认 false；员工应提示人类到日程页确认后启用。人类明确要求立即启用时才传 true。
- **权限**：MCP 工具是 PASSTHROUGH；在 `agent_service.py` 的 MCP 权限检查里对 `mcp__nexus-schedule__` 前缀显式 ALLOW，避免把整个会话切到 `bypass`。
- **不做删除工具**：MCP 首版只创建、列表、查看；删除和编辑仍在页面，避免误删执行历史。

## Risks / Trade-offs

- 官方 `source` 为 `USER`，页面无法区分“员工经 MCP 创建”和“人类手动创建”；这是选择 MCP 路径的代价。
- MCP 服务不可用时私聊仍可继续，但员工没有日程工具；Nexus 记录 warning。
- 日程执行 Session 不在私聊映射里，不会再次安装 MCP，因此不会递归创建日程；但执行中也无法使用 `contact_employee/contact_human`，这是既有边界。
- 多副本部署时每个 Nexus/Agent Service 都需要能访问 MCP 服务；MCP 服务本身无状态。
