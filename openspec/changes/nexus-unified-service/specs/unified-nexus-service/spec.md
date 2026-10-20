# Unified Nexus Service

## Requirement: 单一 HTTP 服务

Nexus 必须由一个 HTTP 服务同时提供员工对话、任务平台、Worker Gateway WebSocket 与前端静态资源。

### Scenario: 同一进程提供两类 API

- WHEN 客户端请求 `/api/v1/employees`
- THEN 请求由 Nexus 统一服务处理并返回员工目录
- WHEN 客户端请求 `/api/tasks`
- THEN 请求由同一个 Nexus 统一服务处理并返回任务列表

### Scenario: 前端与 API 同源

- WHEN 浏览器打开服务根路径
- THEN 返回构建后的前端 `index.html`
- WHEN 前端请求 `/api/health`
- THEN 返回 Nexus 健康信息，不需要第二个端口或反向代理

### Scenario: Worker WebSocket

- WHEN Worker 连接 `/api/worker-gateway/connect`
- THEN Nexus 统一服务接受该 WebSocket 连接

## Requirement: 统一数据表前缀

所有 Nexus 业务表必须以 `t_nexus_` 开头，并位于同一个 `nexus.db`。

### Scenario: 新库初始化

- WHEN 统一服务在空数据库上启动
- THEN 创建的全部业务表名以 `t_nexus_` 开头

### Scenario: 旧库升级

- WHEN 运维执行 `scripts/migrate_legacy_databases.py`
- THEN 旧任务库与聊天库的数据进入统一 `nexus.db`
- AND Alembic 版本到达 `0052_t_nexus_table_prefix`
- AND 库内不再保留裸表名的业务表

### Scenario: 前端构建产物缺失

- WHEN 前端尚未构建
- THEN API 仍可访问
- AND 根路径返回明确的 404，而不是空的 `index.html`

## Requirement: 辅助进程边界

网关数据面、Task MCP、Library MCP 与 AgentScope 可以继续作为独立进程运行。

### Scenario: 部署统一服务

- WHEN 运维启动 Nexus
- THEN `nexus_api.service:app` 是唯一对外 HTTP 服务
- AND 18100 与 15174 不再作为生产端口
