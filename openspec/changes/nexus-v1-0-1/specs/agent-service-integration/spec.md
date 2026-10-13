## ADDED Requirements

### Requirement: 官方 Agent Service 独立部署

系统 MUST 使用固定版本的官方 AgentScope 2.0.10 Agent Service，并将其作为与 Nexus 业务后端独立启停的进程部署。Nexus SHALL 通过官方公开 HTTP 接口和扩展机制集成，不复制、Fork 或修改 AgentScope 核心源码，不以自建执行服务或模拟接口替代官方服务。

#### Scenario: 独立服务可用
- **WHEN** 按交付的配置和启动说明分别启动 AgentScope 与 Nexus
- **THEN** 两个进程独立运行，Nexus 能通过实际服务接口完成连接检查和 Agent 创建，使用的官方版本与固定版本一致

#### Scenario: 核心实现保持官方版本
- **WHEN** 核查部署依赖及 Nexus 集成实现
- **THEN** AgentScope 核心来自固定版本的正式发布包，定制能力仅通过公开接口或扩展实现，未迁入 Astra 业务执行代码

### Requirement: 服务端模型配置与真实推理

模型凭据及 Session 模型绑定 MUST 由 AgentScope 服务管理，部署配置 SHALL 提供可用的默认模型绑定。Nexus 页面 SHALL 不提供模型配置入口，服务 SHALL 不向浏览器返回模型凭据。推理输出 MUST 来自实际模型调用，缺少配置或模型不可用时 SHALL 明确失败。

#### Scenario: 使用部署模型完成推理
- **WHEN** 已配置有效模型的原生 Session 接收用户输入
- **THEN** 实际模型参与推理，回复来自该 Session 的原生消息，Nexus 不通过预设回复代替推理

#### Scenario: 模型配置缺失或调用失败
- **WHEN** 默认模型绑定或凭据缺失，或者真实模型调用失败
- **THEN** 操作返回可见的失败原因，不生成成功回复，也不在页面提供临时模型配置表单

### Requirement: 原生记录和执行事实唯一

原生 Agent 的姓名、实际执行指令和配置，以及活动 Session 的消息、执行状态和原生记忆 MUST 以 AgentScope 为事实源。Nexus SHALL 仅保存必要业务扩展、所有者和关联，不建立第二套活动 Agent 执行状态、消息或记忆系统。员工关联 MUST 使用原生 Agent 记录的顶层 `id`，不能使用与其不同的 `data.id`。

#### Scenario: 原生标识存在两个不同值
- **WHEN** AgentScope 返回的 Agent 记录顶层 `id` 与 `data.id` 不同
- **THEN** Nexus 保存顶层 `id` 并用其完成后续查询、修改、Session 关联和删除，员工始终对应同一真实 Agent

#### Scenario: 查询执行结果
- **WHEN** Nexus 展示活动会话的历史、运行状态或记忆信息
- **THEN** 所展示事实可与原生服务记录核对，不依据前端状态、流关闭或本地消息数量推断完成，不编造不存在的记忆

### Requirement: 持久化与真实进程重启恢复

服务 SHALL 持久保存原生 Agent、Session、会话消息、原生可持久化状态和 workspace，以及 Nexus 的员工扩展、匿名所有者和稳定 Session 关联。正常停止并重新启动实际进程后，已成功保存的数据 MUST 恢复；瞬时事件丢失 SHALL 通过读取原生历史和状态恢复展示，不要求多副本执行恢复。

#### Scenario: 实际进程重启后恢复
- **WHEN** 创建员工并完成至少两轮真实对话后，停止并重新启动 AgentScope 和 Nexus 进程
- **THEN** 原员工及原生 Agent 标识保持不变，原匿名所有者能访问同一 Conversation 和 Session 的完整已保存历史，后续输入能够使用已恢复的会话上下文

#### Scenario: 重启期间存在未完成运行
- **WHEN** 服务重启前某次推理尚未产生终态
- **THEN** Nexus 重读原生历史和状态，展示实际恢复状态或可核实的中断原因，不把重启或缺失流事件当作成功完成

### Requirement: 部署边界与可观测故障

交付 SHALL 包含环境变量模板、独立启停方式、持久数据位置和健康检查说明，并明确匿名身份仅适用于受控验证环境。AgentScope MUST 仅对服务端开放，由 Nexus 代理浏览器访问；内部服务秘密 MUST 不泄露给前端。服务连接失败 SHALL 显式呈现，不返回虚假的健康或业务成功结果。

#### Scenario: 官方服务停止
- **WHEN** Nexus 运行期间 AgentScope 停止或无法连接
- **THEN** 健康检查反映不可用，需要原生服务的业务操作明确失败，已保存页面内容不被伪造成新的执行结果

#### Scenario: 检查浏览器可见配置
- **WHEN** 浏览器加载 Nexus 并使用员工及会话功能
- **THEN** 浏览器只通过 Nexus 访问业务接口，响应中不含内部服务秘密或模型凭据，部署说明披露匿名 UUID 的身份限制
