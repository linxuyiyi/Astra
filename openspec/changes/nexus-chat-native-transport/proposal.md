## Why

阶段 A 的聊天岛只能读 Nexus 扁平化后的历史（`text + tools`），thinking、图片、交互状态已经在
`session_transcript` 里丢掉；实时流仍由旧 Vue 适配器独占，且 `proxy_stream` 在 `REPLY_END`
后注入收尾事件并关闭连接，原生 `useMessages` 只建一条长连接，第二条回复收不到事件。
聊天岛因此只能看历史、不能发送、不能停止。

## What Changes

- Nexus 暴露会话的**原生消息**读取：完整 content 块、事件游标、当前是否在回复，按会话归属授权。
- Nexus 暴露**不中途关闭、不掺杂私有事件**的实时事件流，供原生客户端长期订阅；旧页面的流行为不变。
- 聊天岛的**发送与停止**改经 Nexus；首次建 Session、任务能力装配、身份、模型路由、附件校验留在 Nexus。
- 明确**单一事件消费者**：聊天岛接管后旧渲染不再订阅同一条会话。
- 人工确认不在本期：现有会话策略是 `dont_ask`，确认卡不会出现，改 UI 不得改变该策略。

## Capabilities

### New Capabilities

- `chat-native-transport`: 嵌入式聊天客户端读取原生消息、订阅原生事件流、经 Nexus 发送与停止的契约。

### Modified Capabilities

- 无。`openspec/specs/` 中的 Astra 历史契约不修改；旧聊天页接口保持不变。

## Impact

- `services/nexus-api`：新增原生历史与停止入口，`proxy_stream` 增加透传模式，`read_session` 多返回运行态。
- `web/nexus-chat`：shim 从扁平历史切到原生历史，接入发送与停止，移除只读限制。
- `web/nexus`：`?island=1` 时旧适配器停止订阅，避免双消费者。
- 不涉及 `services/task-platform`、AgentScope Agent Service 运行机制、模型配置与群组路由。
