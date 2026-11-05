## ADDED Requirements

### Requirement: 任务操作反映当前执行状态

系统 SHALL 仅在任务等待人工确认时展示完成入口，完成请求还须校验原生执行状态。
系统 SHALL 拒绝已成功完成的任务重新开始。

#### Scenario: 当前会话需要交互
- **WHEN** 任务等待回答、授权或继续输入
- **THEN** 可进入原生工作区，任务不能直接确认完成

#### Scenario: 任务等待验收
- **WHEN** 任务等待人工确认，且原生执行、提问和授权均已结束
- **THEN** 可确认完成并释放 Worker

#### Scenario: 重新开始执行
- **WHEN** 已取消的任务或原生 Session 已不存在的等待任务请求重新开始
- **THEN** 系统在同一个 Task 下创建新的原生 Session

#### Scenario: 任务已经成功完成
- **WHEN** 任务状态为 SUCCEEDED
- **THEN** 页面不展示重新开始入口，接口拒绝重启，当前 Session 引用和 Worker 占用保持原样

### Requirement: Worker 状态与占用可观测

系统 SHALL 分别呈现运行状态、调度开关、占用任务和现有健康诊断。

#### Scenario: 占用中的 Worker 失联
- **WHEN** Worker 连接断开且任务未释放
- **THEN** 显示离线以及每个占用任务的名称、编号和入口

#### Scenario: 刷新失败
- **WHEN** 已载入的详情刷新失败
- **THEN** 保留上次成功的数据并提示失败及更新时间
