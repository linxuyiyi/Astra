## 目标形态

```text
services/nexus-api/
  nexus_api/      对话、员工目录、统一入口 nexus_api.service:app
  app/            Task、Runtime、Worker、网关控制面、图书馆
  alembic/        任务/Runtime 迁移
  web/            Vue 3 前端，构建产物由统一服务托管
  chat-island/    聊天岛 React 子应用
  tests/nexus/    对话测试
  tests/platform/ 任务与 Runtime 测试
  data/nexus.db   唯一数据库
```

## 决策

### 1. 保留 `app` 包名

Task Platform 有约 4 万行代码、上百个 `from app.*` 引用。把包改名为 `nexus_api.platform.*` 只会制造大面积机械 diff，不增加产品能力。采用 `nexus_api`（对话）+ `app`（任务）两个包，由 `nexus_api.service` 组合为一个 FastAPI 应用。

### 2. 单进程组合方式

`nexus_api.service:create_service()` 复用现有 `create_app()` 注册对话路由，再追加任务路由、异常处理与静态资源兜底；用组合 lifespan 同时运行两套启动/关闭逻辑。`/api/health` 保留对话健康响应，任务健康路由不再进入 OpenAPI（避免重复 operation id），任务数据库健康由任务 API 自身覆盖。

### 3. 数据库

- 任务表由 Alembic 管理，0052 把 40 张表重命名为 `t_nexus_*`。
- 聊天表由 `nexus_api.db` 的 `CREATE TABLE IF NOT EXISTS` 管理，初始化前做一次旧表名重命名；该兼容代码在新部署全部升级后删除。
- 旧 `services/task-platform/data/nexus.db` 与 `services/nexus-api/data/nexus.sqlite3` 通过 `scripts/migrate_legacy_databases.py` 合并。
- 索引名保持不变；表前缀是外部命名约定，索引名不是。

### 4. 前端托管

前端构建到 `services/nexus-api/web/dist`，由统一服务在 `/` 提供。SPA 路径回退 `index.html`；`/assets/` 与 `/opencode-workspace/` 缺失时返回 404，避免把 HTML 当静态资源。API 前缀不参与 SPA 回退。

### 5. 保持独立的进程

OpenCode 网关数据面（18110）、Task MCP（18202）、Library MCP（18201）与 AgentScope（18000）有独立生命周期和连接模型，继续独立运行。`astra_service.py` 只管理这些辅助进程与 Celery/OpenCode。

## 被舍弃的方案

- 只做运行时挂载、保留两套目录：不满足代码与前端归属收敛。
- 全量改包名：机械改动大、回归面大，收益低。
- 保留两个数据库文件：不符合“一个服务、一套数据”的目标。
