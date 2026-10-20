## 1. 目录合并

- [x] 1.1 `app/`、`alembic/`、`configs/`、测试迁入 `services/nexus-api`
- [x] 1.2 `web/nexus` 迁入 `services/nexus-api/web`，`web/nexus-chat` 迁入 `services/nexus-api/chat-island`
- [x] 1.3 删除 `services/task-platform` 与旧 `web/nexus`
- [x] 1.4 合并 pyproject / requirements / pytest 配置

## 2. 统一入口

- [x] 2.1 `nexus_api.service:app` 提供 `/api/v1`、`/api`、`/api/worker-gateway` 与 `/`
  - 验证：OpenAPI 同时包含 `/api/health`、`/api/tasks`、`/api/v1/employees`
- [x] 2.2 组合 lifespan 运行任务 bootstrap、后台任务与对话 AgentScope client
- [x] 2.3 前端静态资源与 SPA 回退
  - 验证：`GET /` 返回 `index.html`；`GET /assets/missing.js` 404；`GET /api/unknown` 404 JSON

## 3. 数据

- [x] 3.1 所有 ORM 表、外键、聊天 SQL 改为 `t_nexus_` 前缀
- [x] 3.2 Alembic `0052_t_nexus_table_prefix` 重命名任务表
- [x] 3.3 聊天表旧名一次性重命名；统一使用 `data/nexus.db`
- [x] 3.4 `scripts/migrate_legacy_databases.py` 合并旧库
  - 验证：迁移后 `alembic_version=0052_t_nexus_table_prefix`，库内无裸业务表

## 4. 部署与文档

- [x] 4.1 `ops/dev`、`ops/deploy`、`ops/env/.env.example` 切到 `nexus-api` 与 18120
- [x] 4.2 `astra_service.py` 不再启动 backend/frontend，只保留辅助进程与 Celery/OpenCode
- [x] 4.3 CI、AGENTS、README 与当前架构文档更新

## 5. 验证

- [x] 5.1 `tests/nexus` 全部通过
- [x] 5.2 `tests/platform`：本机除 Linux-only `openat2`/OS 相关用例外通过；Linux CI 需全量
- [x] 5.3 `ruff check app nexus_api tests`
- [x] 5.4 `web` 与 `chat-island` 的 type-check/build
- [x] 5.5 启动统一服务，确认 `/`、`/api/health`、`/api/tasks`、`/api/v1/employees` 可用
