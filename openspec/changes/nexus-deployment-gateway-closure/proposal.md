## Why

远程 Worker 部署仍丢失执行模式与网关凭据，外部 llm_wiki 接入会生成无效凭据，模型代理缺少 embedding 入口。用户要求修复审查发现并更新真实部署约定。

## What Changes

- 持久化实际服务环境、幂等登记平台网关 key，移除部署阶段厂商直连依赖。
- 统一 Worker 的 /v1 地址，既有 llm_wiki 使用明确的凭据和项目并验证访问。
- 增加共享网关的 embeddings 转发，知识库只使用网关路由登记 credential。
- 更新 README、AGENTS.md 和部署说明；保留 MySQL 8.0.36 空库、独立 AgentScope 与 Redis 边界。
- **BREAKING**：知识库嵌入配置使用 model_route_id 与 dimensions，不再接受厂商 credential_id。

## Capabilities

### New Capabilities
- `nexus-deployment-gateway-contract`：可重启的远程部署配置与全部模型请求代理。

### Modified Capabilities
无。不修改 Astra 历史契约。

## Impact

影响安装器、启动验证、Worker 端点、网关数据面、知识库模型绑定和文档。复用现有鉴权、配额、租约和日志；不改变任务状态机，不修改数据库 schema 或 AgentScope 安装包。
