## ADDED Requirements

### Requirement: Native conversation history

聊天客户端 SHALL 能读取一条私聊会话在 AgentScope 中的原生消息，包含每条消息的 role、name、created_at 与完整 content 块（文本、thinking、工具调用、工具结果、data、hint），以及事件游标和当前是否正在回复。Nexus MUST NOT 把它压缩成纯文本，也 MUST NOT 只保留工具名而丢弃参数、结果或状态。

#### Scenario: History keeps content blocks

- **WHEN** 会话里有一条包含 thinking、工具调用和工具结果的历史消息
- **THEN** 客户端收到的消息保留这些块及其字段，不需要再从扁平字段还原

#### Scenario: Only the owner can read

- **WHEN** 一个用户请求不属于自己的会话历史
- **THEN** 请求被拒绝，响应不包含任何消息内容

#### Scenario: Conversation without a session

- **WHEN** 会话尚未绑定 AgentScope Session
- **THEN** 返回空消息、空游标和未运行状态，而不是报错或新建 Session

#### Scenario: Reply in flight

- **WHEN** 该会话的回复仍在进行
- **THEN** 历史响应标记为运行中，客户端可以立即提供停止操作

### Requirement: Long-lived native event stream

聊天客户端 SHALL 能订阅一条私聊会话的实时事件流。连接 MUST 在一次回复结束后保持打开，且 MUST NOT 混入 Nexus 私有的收尾事件。旧聊天页使用的流 MUST 保持现有行为。

#### Scenario: Two replies on one subscription

- **WHEN** 同一会话连续产生两次回复
- **THEN** 客户端在同一条连接上收到两轮的开始与结束事件

#### Scenario: Resume after disconnect

- **WHEN** 客户端带游标重新订阅
- **THEN** 只收到游标之后产生的事件

#### Scenario: Legacy stream unchanged

- **WHEN** 旧聊天页订阅同一条会话
- **THEN** 它仍按现有约定收到收尾事件并结束连接

### Requirement: Sending goes through Nexus

从聊天客户端发送消息 SHALL 经 Nexus 的会话发送入口完成，保留首次建 Session、任务能力装配、发送者身份、模型路由与附件校验。浏览器 MUST NOT 直接调用 AgentScope 的聊天入口发送消息。

#### Scenario: First message creates the binding

- **WHEN** 一条尚未绑定 Session 的会话收到第一条消息
- **THEN** Nexus 建立 Session、装配任务能力，并在响应中返回可读的绑定

#### Scenario: Attachment rejected

- **WHEN** 消息引用了不属于该用户的附件或超过数量/大小限制
- **THEN** 请求被拒绝，消息不会进入会话

#### Scenario: Retry does not duplicate

- **WHEN** 客户端因网络重试同一条发送请求
- **THEN** 会话中不出现两条相同的用户消息，客户端能按请求标识对账本地乐观消息

### Requirement: Stop the right reply

停止 SHALL 经 Nexus 校验会话归属与绑定后转发给对应 Session；会话的权限策略 MUST NOT 因客户端改造而改变。

#### Scenario: Stop reaches the session

- **WHEN** 用户对正在回复的会话点击停止
- **THEN** 事件流随后收到该轮的结束事件，界面回到空闲态

#### Scenario: Stop foreign session rejected

- **WHEN** 用户对不属于自己的会话请求停止
- **THEN** 请求被拒绝，且不向 AgentScope 发出任何调用

#### Scenario: Confirmation policy unchanged

- **WHEN** 会话策略为 `dont_ask`
- **THEN** 界面不出现确认卡，客户端改造也不会把未经确认的工具调用放行

### Requirement: One event consumer per conversation

同一会话在同一时刻 MUST 只有一个实时事件消费者。

#### Scenario: Island takes over

- **WHEN** 聊天岛在 `?island=1` 下接管某会话
- **THEN** 旧渲染不再订阅该会话的事件流

#### Scenario: Switching conversations

- **WHEN** 用户从一个会话切到另一个
- **THEN** 前一个会话的订阅被释放，新会话不会收到旧会话的事件
