## ADDED Requirements

### Requirement: 稳定的业务关联

系统 SHALL 保存验收项标识，并保护未完成复盘的源 Worker。

#### Scenario: 编辑验收项或 Worker
- **WHEN** 用户修改验收项或尝试删除或改变未完成复盘的源 Worker
- **THEN** 原验收项保留标识，新项分配标识，破坏复盘来源的 Worker 操作被拒绝

### Requirement: 有界运行监控

系统 SHALL 按当前连接事实呈现运行监控，并限制每次事件和日志读取量。

#### Scenario: 打开的监控页面
- **WHEN** 可见列表上的 Task 或 Worker 状态变化，或其他用户创建运维操作
- **THEN** 页面自动刷新当前查询，连接态独立于业务状态

#### Scenario: 长时间任务和运维
- **WHEN** 事件或日志持续产生
- **THEN** 最新视图增量读取，历史可分页查询

#### Scenario: 原生订阅无首事件
- **WHEN** 新原生订阅建立但尚未收到第一条事件
- **THEN** Task 执行一次原生状态对账
