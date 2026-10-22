# 数字员工日程 MCP

日期：2026-10-23。状态：**已实现**。私聊中的数字员工可以通过内置 MCP 工具为当前人类创建、列出、查看周期日程；日程仍由 AgentScope 存储和触发，页面仍是管理入口。

## 使用方式

1. 人类在私聊里提出安排，例如“每周一早上 9 点整理本周重点”。
2. 员工确认频率、时区、到点后要做什么，然后调用 `schedule_create`。
3. 创建默认 `enabled=false`；员工应提示人类到“日程安排”页检查内容并启用。
4. 之后员工可以调用 `schedule_list`/`schedule_view` 查看当前人类在本次员工下的日程；删除和编辑仍在页面完成。

MCP 工具只有三个：

| 工具 | 作用 |
| --- | --- |
| `schedule_create` | 创建周期日程；`name`、`description`、五字段 `cron_expression`、`timezone`（默认 `Asia/Shanghai`）、`stateful`、`enabled`（默认 false） |
| `schedule_list` | 列出当前人类在本次员工下的日程 |
| `schedule_view` | 按日程标识查看详情 |

没有 `schedule_update` 或 `schedule_delete`；首版不让模型删除执行历史。

## 组件与数据流

```text
私聊消息
  → Nexus send_private / rebind / IM accept
  → attach_schedule_mcp() 把 MCP 装进该 AgentScope Session
  → 员工调用 mcp__nexus-schedule__schedule_create
  → 日程 MCP 带 session/employee 头调用 Nexus 内部接口
  → Nexus 按 t_nexus_conversations 解析人类，校验员工
  → 以 nexus-schedule-<sha256(uid)> 调官方 /schedule/
  → AgentScope 存储；页面按人类命名空间读取
```

- `services/nexus-api/nexus_api/schedule_mcp.py`：MCP 服务（默认 `127.0.0.1:18203`），校验 Bearer 密钥、员工和会话头，提供三个工具。
- `services/nexus-api/nexus_api/schedule_skill.py`：把 `nexus-schedule` 装进私聊 Session；按 `(agent_id, session_id)` 幂等，失败只记 warning，不阻塞聊天。
- `services/nexus-api/nexus_api/schedules.py`：新增 `/api/v1/internal/schedules` 内部接口，按 `X-Nexus-Session-Id` 查私聊归属，校验 `X-Nexus-Employee-Id` 与员工一致，再以人类身份调用官方接口。
- `services/agent-service/agent_service.py`：对 `mcp__nexus-schedule__` 前缀显式 ALLOW，避免把会话切到 `bypass`。
- 安装点：`send_private`、`rebind_private`、IM 私聊创建和已有私聊消息；团队、邮箱、渠道不安装。

## 归属与安全

- 归属只来自私聊 Session：`t_nexus_conversations.agentscope_session_id + kind='private'` → `owner_id`。MCP 工具和内部接口都不接受 `user_id`/`employee_id`/模型/凭据参数。
- `X-Nexus-Employee-Id` 必须等于会话绑定的员工，否则 409；未知/非私聊会话 404。
- 内部接口要求 `X-Nexus-Internal-Key`，MCP 服务要求自己的 `NEXUS_SCHEDULE_MCP_KEY`；两个密钥都在安装器/环境文件中生成并持久化。
- 员工禁用或没有 Agent 时拒绝。
- 默认不启用，减少“被文档/网页提示注入后自主创建定时任务”的风险；人类在页面确认后启用。
- 只列出当前员工名下的日程，不把同一人类安排给其他员工的日程交给当前员工。

## 官方事实源与已知边界

- 日程、cron 校验、计时器、执行 Session、执行内容全部在 AgentScope；Nexus 没有日程表或计时器。
- MCP 创建走官方 REST，官方 `source` 记为 `USER`、`source_session_id` 为空；页面无法区分“员工经 MCP 创建”和“人类手动创建”。这是选择 MCP 路径的代价，Nexus 日志保留 MCP 调用。
- 日程执行工作区仍不继承私聊的 Skill/MCP/知识库；执行中的 `contact_employee`/`contact_human` 也没有会话映射。需要员工资源的安排仍应先在页面确认能力边界。
- 模型路由变更不会改写已有日程的模型快照；删除员工不会级联日程。

## 配置

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `NEXUS_SCHEDULE_MCP_URL` | Nexus 安装给 Session 的 MCP 地址 | `http://127.0.0.1:18203/mcp` |
| `NEXUS_SCHEDULE_MCP_KEY` | Nexus 与 MCP 服务共享的 Bearer 密钥 | 无，未设置则不安装 |
| `NEXUS_INTERNAL_KEY` | MCP 调 Nexus 内部接口的共享密钥 | 无，未设置则内部接口拒绝 |
| `NEXUS_SCHEDULE_MCP_HOST` / `PORT` | MCP 监听地址/端口 | `127.0.0.1` / `18203` |
| `NEXUS_API_BASE` | MCP 调 Nexus 的地址 | `http://127.0.0.1:18120` |

安装器在 `secrets.env` 生成两个密钥，在 `nexus.env` 写入 MCP 端口和 URL；`astra_service` 启动 `schedule-mcp`，健康检查为 `http://127.0.0.1:18203/health`。

## 验证

- Nexus：`136 passed, 4 skipped`（含内部接口、MCP 工具契约、私聊挂接和权限）。
- Agent Service：`13 passed, 1 skipped`；前端 `30 passed`、`vue-tsc` 与构建通过；Ruff、OpenSpec strict 通过。
- 隔离端到端：真实 AgentScope 2.0.10 + 隔离 Nexus + 日程 MCP + 本地模型桩；私聊创建 Session 后 `nexus-schedule` 健康并发现三个工具；MCP `schedule_create` 返回 `enabled=false` 的日程；页面 API 对当前人类可见、对另一个人不可见；`schedule_list`/`schedule_view` 可用；清理返回 204。证据见 [mcp-e2e.json](../reports/evidence/employee-schedules-20261023/mcp-e2e.json)。
- 未验证：真实供应商模型自行选择工具、墙钟周期触发、生产 MySQL 8.0.36、生产 Nginx、多副本。
