## 为什么

Task Platform 0.4.0 与 Nexus API 目前是两个 FastAPI 服务、两个部署单元和两套 SQLite 库。前端通过不同端口分别调用 `/api` 与 `/api/v1`，跨模块状态和部署配置容易分叉。产品需要一个 Nexus 服务，而不是两套服务。

## 变更范围

- `services/nexus-api` 成为唯一 Nexus 服务：同一进程提供 `/api/v1`、`/api`、Worker Gateway WebSocket 与前端静态资源。
- Task Platform 的代码、Alembic、配置、测试和前端代码整体迁入 `services/nexus-api`。
- 任务库与聊天库合并为一个 `nexus.db`，所有业务表统一 `t_nexus_` 前缀。
- 更新部署脚本、环境模板、CI 与当前架构文档。

## 非目标

- 不改变 Task、Run、Employee、Runtime 的业务状态机与 API 语义。
- 不把 OpenCode 网关数据面、Task MCP、Library MCP、AgentScope 并入同一进程。
- 不修改 `openspec/specs/` 中的 Astra 历史契约。

## 影响

- 端口：对外 HTTP 收敛到 18120；18100 与 15174 不再作为生产端口。
- 数据：升级必须执行一次旧库合并与 Alembic 迁移。
- 目录：`services/task-platform` 与 `web/nexus` 不再存在。
