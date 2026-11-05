## Why

Task 状态由定时原生 REST 查询推进，原生 SSE 仅供浏览器使用。Worker 上的原生流中断时也可能没有通知订阅者。

## What Changes

- 后台按活动 Task 订阅原生 SSE，相关事件触发统一对账。
- 初次订阅、断流恢复、事件缺口和未知提交继续使用既有恢复边界。

## Capabilities

### New Capabilities

- `nexus-task-native-reconciliation` 原生事件触发 Task 对账。

### Modified Capabilities

无。

## Impact

涉及 Task Runtime、Worker 事件链路和 E2E 验收。
