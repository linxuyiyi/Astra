## Why

公司以 MySQL 8.0.36 从空库部署，不需要旧 SQLite 数据或历史版本升级。当前 Nexus 两套数据接入和历史迁移链阻碍全新部署，AgentScope 尚使用 Redis 持久化。

## What Changes

- **BREAKING**：Nexus 删除历史迁移和旧库合并工具，以单个 `0001_init` 创建当前完整 schema；已有数据库不支持就地升级，不自动清库或 stamp。
- Nexus 自有业务表统一使用配置指定的数据库，生产使用 MySQL 8.0.36，保留 SQLite 用于开发和回归。
- AgentScope 2.0.10 使用官方 SQL 存储保存 Agent、Session、消息和凭据，独立管理其 schema；Redis 保留消息总线与运行态职责。
- 更新全新安装、数据库预检、测试和运维文档，不开发历史数据导入与双写。

## Capabilities

### New Capabilities

- `nexus-fresh-sql-storage`：全新数据库初始化、统一业务持久化与服务重启恢复。
- `nexus-agentscope-sql-storage`：AgentScope 官方 SQL 持久化与独立消息总线。

### Modified Capabilities

无。不修改 Astra 历史 specs；本变更替代 Nexus 早期方案中的历史库合并流程。

## Impact

影响 `services/nexus-api` 数据库接入、SQL 查询和 Alembic，`services/agent-service` 存储装配及安装脚本。API 与运行状态机保持既有语义。需要 MySQL 驱动和真实数据库验证；不变更模型推理、任务调度规则或前端展示。
