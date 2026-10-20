## 为什么

平台助手是早期测试平台的功能，现已被数字员工取代。它仍占用独立 OpenCode Runtime、assistant MCP、前端复盘入口和 8 张数据表，容易让使用者误以为平台还有第二个对话式操作入口。

## 变更范围

- 删除 `app/assistant/`、assistant MCP、14097 OpenCode Server 与全部 assistant 配置。
- 删除依赖助手的任务复盘/总结：`TaskReview`、`TaskSummary`、`review_history`、`/api/tasks/{id}/review`、任务详情“总结与经验”。
- 删除 6 张 assistant 表、`task_reviews`、`task_summaries`，并移除 `tasks.review_history` 列。
- 删除前端复盘抽屉、API client 方法与类型，以及 `.opencode` 的 assistant Agent/Skill。
- 更新测试、部署脚本、环境模板、当前架构文档与 OpenSpec 说明。

## 非目标

- 不改变数字员工、Task、Runtime、Worker、知识库与网关的现有行为。
- 不删除历史报告、归档设计文档或 `openspec/changes/` 中的历史记录。

## 影响

- 升级到 0053 会永久删除 assistant 与复盘数据；升级前需备份 `nexus.db`。
- `nexus_task_summary` MCP 工具与任务复盘 API 不再存在。
- 14097 端口不再被占用。
