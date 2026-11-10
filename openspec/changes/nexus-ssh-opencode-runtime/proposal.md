## Why

Worker Gateway 将原生 HTTP/SSE 包装成持久化命令、事件日志及跨进程路由，扩大恢复与部署复杂度。本次统一为 SSH 隧道访问 OpenCode 原生接口。

## What Changes

- **BREAKING** 删除 Worker Gateway、WebSocket、HTTP DataChannel、Redis Route Bus 及通信表/配置/API。
- Worker 复用 SSH 连接及 loopback 端口转发，保留部署、维护、文件操作和工作区代理。
- 原生 SSE 只通知对账；周期查询原生状态，断线保留任务与占用，Session 丢失等待人工。
- 增加确认执行失败后的 FAILED 终态。

## Capabilities

### New Capabilities
- `nexus-ssh-task-runtime`: SSH 原生任务执行、恢复与隔离契约。

### Modified Capabilities
无；不修改历史 Astra 契约。

## Impact

services/nexus-api 的 Runtime、Worker、部署、模型、API、前端及测试；ops 入口配置和部署说明。无历史数据迁移、兼容通道、Agent 内部编排。
