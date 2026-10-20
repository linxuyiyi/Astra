## 决策

### 1. 整条链路一起删除

任务复盘与总结由平台助手产出，不是独立于助手的业务能力。保留 `/api/tasks/{id}/review`、`TaskReview`、`TaskSummary` 会留下一个永远无法产生新结果的空接口。因此一起删除：

```text
app/assistant/ → assistant MCP → TaskReview/TaskSummary → /review API → 任务详情抽屉
```

### 2. 数据表直接删除

新增 Alembic `0053_remove_platform_assistant`：

- drop `t_nexus_assistant_*` 6 张表；
- drop `t_nexus_task_reviews`、`t_nexus_task_summaries`；
- drop `t_nexus_tasks.review_history`。

这是破坏性迁移。`downgrade` 不重建数据，升级前必须备份。历史迁移 0013/0022/0030 保留不动，新库仍按时间线创建后再删除。

### 3. 运行时收敛

- `astra_service.py` 不再创建 `assistant-opencode` 进程，`load_astra()` 不再加载 `AssistantOpenCodeServerManager`。
- `preflight.py` 删除平台助手 Runtime 检查。
- 14097 从部署端口、环境模板与文档中移除。
- `.opencode/agents/platform-assistant.md`、`.opencode/skills/nexus-task-experience-review/` 删除。

### 4. 保留知识库通用能力

`KnowledgeService.mcp_submit_experience` 与经验审核/发布能力保留；它们是知识库能力，可由人工或其他集成调用。本次只移除平台助手这个生产者，不删除知识库数据与接口。

## 被舍弃的方案

- 只删前端入口、保留后端：留下无人调用的 assistant Runtime 与表。
- 保留复盘并改由数字员工生产：需要重新设计复盘协议，不属于本次移除。
