## Why

Worker 求助目前只能在工作区回答，责任员工无法通过 Task MCP 查询和回传答案。人不得被要求进入 AgentWork；数字员工应先利用已有上下文处理，确实缺少业务信息或授权时才在原对话求助。

## What Changes

- 新增责任员工查询当前 Worker 提问、权限请求和 DSH 回合输出、回传回答的能力。
- 复用任务事件唤醒员工；私聊回到原 Session，群聊在有效成员权限下发起只包含责任员工的新讨论。
- 更新任务技能，要求先判断是否能回答；必须问人时携带明确问题和选项，并把人的答复送回同一任务。
- 拒绝跨任务、跨来源会话、过期请求、历史 Session、结束任务和停止中的写入。

## Capabilities

### New Capabilities

- `nexus-worker-assistance`: 通过数字员工闭环处理 Worker 求助。

### Modified Capabilities

无。不修改 Astra 历史契约。

## Impact

影响 Task MCP、现有 Runtime 适配、任务唤醒、群讨论投递及 Agent Service 任务技能。保留原生 Runtime 为请求事实源，不新增 Agent 或任务状态机，不改变人工任务验收，不自动部署或发布。
