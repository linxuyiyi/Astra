## Context

阶段 A 已在 `?island=1` 下把中间区换成 React 聊天岛，渲染复用 AgentScope v2.0.10 组件，
数据取 `GET /api/v1/conversations/{id}` 的扁平历史（`text + tools`）。该响应由
`session_transcript` 生成，thinking、data 块和工具状态在转换时丢失；实时流由
`proxy_stream` 提供，它在 `REPLY_END` 后注入 `NEXUS_RUN_FINISHED` 并主动关闭连接。

Agent、Session、消息与运行态只存在于 AgentScope。Nexus 只保存员工目录、会话绑定和
未被接受的用户消息，不复制消息内容。

## Decisions

**D1 原生历史单独开一个读取入口。**
新增 `GET /api/v1/conversations/{id}/messages`，返回上游 `Msg[]`、`event_cursor`、`is_running`。
现有 `GET /api/v1/conversations/{id}` 的扁平响应保持不动，旧页面不回归。
理由：扁平响应是旧渲染的现有契约，就地改成原生结构会同时破坏旧页和阶段 A 的回退路径。

**D2 事件流加显式透传模式，而不是复制一条新流。**
`GET /api/v1/conversations/{id}/stream` 增加 `raw=1`：原样转发上游帧，不注入
`NEXUS_RUN_FINISHED`，不因 `REPLY_END` 关闭连接。默认（无 `raw`）行为不变。
理由：旧页依赖“收尾即关闭”来结束一次读取循环；两种消费者对连接生命周期的要求相反，
只能显式区分。

**D3 运行态取上游字段，不从消息或时间推断。**
`read_session` 额外返回上游 `GET /sessions/{sid}/messages` 响应里的 `is_running`。
理由：AGENTS.md 明确禁止用时间戳、事件数量或前端状态反推运行状态。

**D4 发送仍然只有一条路。**
客户端把 `ContentBlock[]` 压成 `{text, attachment_ids}`，走现有
`POST /api/v1/conversations/{id}/messages`。附件先经 `/api/v1/attachments` 上传并保留归属。
不新增平行发送通道，也不让浏览器直连 `/agentscope-api/chat/`。

**D5 停止先校验归属，再转发。**
新增 `POST /api/v1/conversations/{id}/interrupt`；Nexus 校验 owner、确认会话已绑定
Session 后调用 AgentScope `POST /sessions/{sid}/interrupt`。浏览器不能自带 agent/session。

**D6 单一消费者。**
`?island=1` 时旧适配器不再调用 `follow()`，事件流只由聊天岛消费。
理由：两条 SSE 会各自维护游标，重连时互相抢占。

**D7 人工确认不在本期。**
私聊发送路径每次都会 `set_permission_mode(..., "dont_ask")`，确认卡不可达。
本变更不改变该策略，也不把 `ConfirmCard` 的确认通道接到 Nexus。

## Rejected Alternatives

- 让浏览器直接调 `/agentscope-api/`：绕过首次建 Session、任务能力装配与身份。
- 在聊天岛里再写一套事件解析：与 `useMessages` 重复，且会分叉上游行为。
- 把扁平历史就地改成原生结构：破坏旧页与回退路径。
- 用文本+时间去重对账：会吞掉连续相同消息。

## Risks

- `raw=1` 的连接不自动结束，客户端必须自己 abort，否则切换会话会留下连接。
- 历史读取多了一次上游分页遍历（最多 10 页），长会话首屏会变慢，必要时再加缓存。
- 真实环境验收需要一条可用的 AgentScope Session；本机旧 Session 在重启后已不存在，
  验收前要重新建立。
