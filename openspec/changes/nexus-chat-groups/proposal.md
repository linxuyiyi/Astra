## Why

群组不是一条 AgentScope Session：人类消息在 Nexus，每位员工各有自己的 Session，
回复、工具调用和停止都以成员为单位。现有 `/timeline` 把一切压成纯文本，聊天岛无法
按发言人还原原生消息，也无法在成员维度订阅事件与停止。

## What Changes

- 新增群组原生时间线：按来源返回每位成员 Session 的完整 `Msg`，人类消息只出现一次。
- 成员级 raw 事件流：一条会话一轮结束后不关闭，按成员独立订阅。
- 成员级停止：校验群组与成员绑定后转发到对应 Session。
- 聊天岛新增群组视图：合并多 Session、在气泡外标注发言人、复用原生气泡与输入区；
  发送仍走 Nexus 的 channel 入口，`@` 路由与去重规则不变。
- 不使用 AgentScope Agent Team 替代群组。

## Capabilities

### New Capabilities

- `chat-group-timeline`: 群组原生时间线、成员级事件流与停止、发言人归属的契约。

### Modified Capabilities

- 无。旧 `/timeline` 的扁平响应保留给旧页面。

## Impact

- `services/nexus-api`：新增群组原生时间线与成员停止入口，成员流支持 raw 透传。
- `web/nexus-chat`：新增群组视图与数据通道。
- `web/nexus`：`?island=1` 时群组也交给聊天岛，旧适配器不再订阅。
