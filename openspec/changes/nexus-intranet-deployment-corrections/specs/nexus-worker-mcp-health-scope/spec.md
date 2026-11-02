## ADDED Requirements

### Requirement: 受管 MCP 决定调度门禁

Worker MCP 就绪 MUST 根据平台要求与管理员显式绑定的受管集合计算，不能由 OpenCode 全局额外配置的聚合结果决定。节点 SHALL 报告逐服务连接事实；控制面 SHALL 统一判定周期报告、手动检查和部署验收。额外 MCP 异常 MUST 继续显示为诊断告警。

#### Scenario: 额外项目 MCP 失败
- **WHEN** 所有受管 MCP 均已正确部署并连接，而未绑定的项目 MCP 连接失败或缺失，且其他调度条件满足
- **THEN** Worker 保持 READY、可调度，并显示额外 MCP 的名称与安全错误摘要

#### Scenario: 显式绑定 MCP 失败
- **WHEN** 任一受管 MCP 配置已生效但连接失败
- **THEN** Worker 为 DEGRADED、不可调度，并显示具体受管服务的故障，不把故障标记为配置待同步

#### Scenario: 逐服务观测不可用
- **WHEN** 节点无法读取 MCP 状态或不能证明受管 MCP 的连接状态
- **THEN** Worker 为 UNKNOWN、不可调度，不能从端点存在或其他 MCP 健康猜测已连接

### Requirement: MCP 配置漂移保持手动同步语义

MCP 绑定或端点的期望配置与实际配置不一致时 MUST 保持 DRIFTED 并阻止新派单，不能因为额外 MCP 告警策略而放宽。保存 SHALL 不自动同步，也不能据此改写正在执行任务的终态。

#### Scenario: 修改或删除显式绑定
- **WHEN** 管理员保存受管 MCP 的新增、地址修改或删除
- **THEN** Worker 保持待同步、不可接受新任务，直到管理员手动部署并核验；当前任务继续由其原有生命周期管理
