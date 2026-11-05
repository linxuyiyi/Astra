## ADDED Requirements

### Requirement: 活跃经验复盘保护来源 Task

Task 存在活跃经验复盘作业时，删除接口 SHALL 返回 HTTP 409。所有复盘作业均进入终态后，Task SHALL 可按现有规则删除。

#### Scenario: 删除时复盘仍活跃

- **WHEN** 用户删除仍有排队、处理或等待重试作业的 Task
- **THEN** 返回 HTTP 409，保留 Task、复盘状态、租约和知识来源关联

#### Scenario: 删除时复盘已结束

- **WHEN** 用户删除满足现有删除条件且复盘作业已全部进入终态的 Task
- **THEN** 删除 Task，保留复盘结果、知识条目和原生证据
