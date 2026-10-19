## ADDED Requirements

### Requirement: Native group timeline

聊天客户端 SHALL 能读取群组按来源划分的原生消息：人类消息一条流，每位成员一条 Session 流，
每条消息保留完整 content 块。同一条人类消息 MUST 只出现一次。

#### Scenario: Human message appears once

- **WHEN** 一条人类消息被写进多个成员 Session
- **THEN** 时间线里只保留 Nexus 记录的那一条

#### Scenario: Member blocks are preserved

- **WHEN** 成员的回复包含 thinking、工具调用与文本
- **THEN** 客户端收到的消息保留这些块，不需要从纯文本还原

#### Scenario: Member without session

- **WHEN** 某位成员尚未建立 Session
- **THEN** 时间线不包含该成员的空流，也不会因此报错

### Requirement: Per-member event streams

聊天客户端 SHALL 能按成员订阅实时事件流，连接在一次回复结束后保持打开；某位成员的
订阅失败或中断 MUST NOT 清空其他成员的消息。

#### Scenario: Only the mentioned member replies

- **WHEN** 用户发送 `@某成员` 的消息
- **THEN** 只有该成员产生新的回复事件

#### Scenario: Stop one member

- **WHEN** 用户停止正在回复的成员
- **THEN** 该成员的会话收到停止请求，其他成员不受影响

### Requirement: Speaker attribution outside the bubble

群组视图 SHALL 在消息气泡之外标注发言人；MUST NOT 把员工姓名拼进消息正文。

#### Scenario: Two members reply

- **WHEN** 两位成员先后回复
- **THEN** 各自的消息上方显示对应员工名，正文保持原样
