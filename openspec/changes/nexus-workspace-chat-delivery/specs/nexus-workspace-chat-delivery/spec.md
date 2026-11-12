## ADDED Requirements

### Requirement: 聊天附件按会话或群组归属管理

聊天附件 SHALL 由 AgentScope 保存原件。Nexus SHALL 对上传、发送和下载校验作用域及身份。

#### Scenario: 群成员上传原件

- **WHEN** 当前群成员上传文件并发布群消息
- **THEN** 当前群成员可以下载，授权的接收员工可在独立 Session 按需读取附件
- **AND** 群外人员和被移除成员无法通过 Nexus 获取原件

#### Scenario: 首次私聊并发操作

- **WHEN** 用户在新私聊同时上传和发送
- **THEN** 操作使用同一个私聊绑定，并阻止跨会话引用

#### Scenario: 旧附件迁移

- **WHEN** 管理员迁移旧消息附件
- **THEN** 校验原件完整性，保留历史下载并报告无法归属或缺失的文件

### Requirement: 保留原生内容和业务投递约束

发送 SHALL 保留原生内容、业务标识和可信执行来源。群消息 SHALL 按接收 Session 的增量边界发送。

#### Scenario: 群游标落后

- **WHEN** 有超过一百条群发言等待投递
- **THEN** 从已送达游标之后按顺序投递，保留发言人与消息标识

#### Scenario: 连续发送

- **WHEN** 会话的模型与资源绑定保持不变
- **THEN** 普通发送复用现有装配，权限撤销后立即停止授权

### Requirement: 私聊提交可重试

私聊 SHALL 以同一会话内的客户端消息标识识别重试，并在调用模型前保护消息引用的附件。

#### Scenario: 复用客户端消息标识

- **WHEN** 已登记的消息标识再次发送，且正文或附件发生变化
- **THEN** Nexus 返回 409
- **AND** AgentScope 不接收新输入

#### Scenario: 上游提交结果未知

- **WHEN** Nexus 无法确认 AgentScope 是否接受已经登记的发送
- **THEN** 附件不会被草稿过期任务回收，客户端可以重试原消息

### Requirement: 控制台会话管理受业务边界约束

通用控制台 SHALL 拒绝读取或改写 Nexus 受管员工的原生会话。

#### Scenario: 浏览器请求受管 Agent 的会话列表

- **WHEN** 浏览器通过控制台请求受管员工的 Session 列表
- **THEN** Nexus 在代理请求前拒绝访问


### Requirement: 群投递和成员附件使用可收敛

群消息 SHALL 按顺序分批投递，并在 AgentScope 确认后更新接收游标。群成员权限撤销后 SHALL 回收该成员 Session 的附件副本，群原件继续归群组管理。

#### Scenario: 接收者积压超过单批预算

- **WHEN** 同一接收者的积压需要多个批次
- **THEN** Nexus 按顺序发送有界批次，未确认的消息保留为积压

#### Scenario: 移除有附件副本的数字员工

- **WHEN** 管理员移除有附件副本的数字员工
- **THEN** 员工的工具访问立即撤销
- **AND** 确认运行停止后清理该成员副本和 Session，群原件仍可供有权限的成员下载
