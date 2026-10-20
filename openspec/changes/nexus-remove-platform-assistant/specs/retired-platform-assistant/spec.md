# Retired Platform Assistant

## Requirement: 平台助手不再存在

Nexus MUST NOT 运行平台助手 OpenCode Runtime、assistant MCP 或 `/api/assistant/*` 接口。

### Scenario: 旧入口不可用

- WHEN 客户端请求 `/api/assistant/health`
- THEN Nexus 返回 404

### Scenario: 统一服务不启动助手

- WHEN 运维启动 Nexus
- THEN 只启动数字员工 Task Runtime、网关、MCP 与统一 API
- AND 不占用 14097 端口

## Requirement: 任务复盘与总结随助手移除

Nexus MUST NOT 提供任务复盘/总结 API、页面入口或后台调度。

### Scenario: 复盘接口移除

- WHEN 客户端请求 `/api/tasks/{id}/review`
- THEN Nexus 返回 404

### Scenario: 任务详情无复盘入口

- WHEN 用户打开任务详情
- THEN 不显示“总结与经验”菜单或复盘抽屉

## Requirement: 助手数据被清除

数据库 MUST NOT 保留 assistant 表、task_reviews、task_summaries 或 tasks.review_history。

### Scenario: 升级到 0053

- WHEN 执行 `alembic upgrade head`
- THEN `t_nexus_assistant_*`、`t_nexus_task_reviews`、`t_nexus_task_summaries` 被删除
- AND `t_nexus_tasks.review_history` 被删除
- AND 升级前备份可恢复旧数据

## Requirement: 数字员工能力不受影响

移除助手 MUST NOT 改变数字员工、Task、Runtime、Worker、知识库与网关的现有行为。

### Scenario: 数字员工任务正常执行

- WHEN 数字员工领取并执行任务
- THEN Task/Session 状态与事件流保持原语义
- AND 只有复盘/总结入口消失
