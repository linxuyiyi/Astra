# Task Experience Review v2

## Requirement: 人工确认后独立复盘

系统 SHALL 在人工确认任务完成后创建一次持久化复盘作业；复盘 MUST NOT 改变任务状态或占用执行 Worker。

### Scenario: 确认创建作业

- WHEN 用户确认任务完成
- THEN 同一事务写入确认 Event 与 PENDING 复盘 Job
- AND 重复确认不产生新 Job

### Scenario: 复盘失败

- WHEN 复盘超时、模型失败或证据不可用
- THEN Task 保持 SUCCEEDED
- AND Job 记录稳定错误码并按策略重试或终止

## Requirement: 大轨迹分层分析

系统 SHALL 分页读取原生 Session，按因果边界切片，并使用 Map/Reduce 在有限上下文内完成复盘。

### Scenario: 超过上下文

- WHEN 单段超过预算
- THEN 系统拆段或缩窗，不一次性合并全部历史

### Scenario: 断点恢复

- WHEN Worker 在部分片段完成后退出
- THEN 重启只继续未完成片段

## Requirement: 证据可信

系统 SHALL 只接受真实属于来源 Session、位于确认水位内、可定位到原生 message/part 的证据。

### Scenario: 伪造引用

- WHEN 模型输出不存在的 trace/message/part
- THEN 候选被拒绝且不写入知识库

## Requirement: 候选受控

系统 SHALL 只写入 candidate，MUST NOT 自动发布或修改员工 Harness 配置。

### Scenario: 双轨输出

- WHEN 复盘发现领域经验或工作方法改进
- THEN 分别以 domain / harness_improvement 写入候选
- AND 都需要人工审核后才能发布
