## Why

删除已完成的 Task 会主动终止尚未结束的经验复盘，导致人工确认后应沉淀的经验丢失。待复盘结束再删除，可以保留候选经验和原生证据。

## What Changes

活跃复盘期间暂时拒绝删除 Task，待所有复盘作业进入终态后允许删除。

## Capabilities

### New Capabilities

- `nexus-task-review-deletion` 规定 Task 删除与经验复盘的互斥边界。

### Modified Capabilities

无。

## Impact

影响 Task 删除接口和并发手动重试的事务顺序。沿用现有知识来源清理规则和复盘快照。
