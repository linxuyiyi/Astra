## 1. 后端

- [x] 1.1 删除 `app/assistant/`、assistant MCP 入口与 `app/runtime/summary.py`
- [x] 1.2 删除 `TaskReview`、`TaskSummary`、`Task.review_history`、`/tasks/{id}/review` 与 Celery 复盘任务
- [x] 1.3 删除 `nexus_task_summary` MCP Tool 与 `task_tools.summary`
- [x] 1.4 删除 assistant 配置、OpenCode Runtime、预检与端口冲突判断

## 2. 数据

- [x] 2.1 新增 `0053_remove_platform_assistant`，drop 8 张表与 `review_history`
  - 验证：空库 `alembic upgrade head` 后无 assistant/task_reviews/task_summaries 表
- [x] 2.2 本机 `nexus.db` 已升级到 0053

## 3. 前端

- [x] 3.1 删除任务详情“总结与经验”入口、复盘抽屉、API 类型与 client 方法
  - 验证：`npm run type-check`、`npm run build`

## 4. 运维与文档

- [x] 4.1 `astra_service.py`、`preflight.py`、安装脚本、环境模板移除助手
- [x] 4.2 删除 `.opencode` assistant Agent 与经验复盘 Skill
- [x] 4.3 更新 README、部署指南、端口对照与 OpenSpec 说明

## 5. 验证

- [x] 5.1 `tests/nexus` 与 `tests/platform`：1106 passed；仅 3 个 macOS 无法执行的 Linux-only 用例失败，需 Linux CI 复跑
- [x] 5.2 `ruff check app nexus_api tests`
- [x] 5.3 统一服务启动后 `/api/health`、`/api/tasks`、`/` 可用，`/api/assistant/*` 404
