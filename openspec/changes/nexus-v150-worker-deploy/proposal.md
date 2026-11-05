## Why

公司部署暴露了 Worker 入口诊断、资源下发和就绪验收的问题。当前部署可能在安装后才发现网络不可达、遗漏 MCP 认证头，或从与平台无关的目录取 Skill。

## What Changes

- 在 Worker 主机检查受管入口与代理路由。
- 使用平台当前启用 Skill 版本，完整下发 MCP 连接参数。
- 使用部署生命周期的剩余预算验收本次认证连接和装备，反馈实际阻塞原因。

## Capabilities

### New Capabilities

- `nexus-worker-deployment`：Worker 安装入口、资源和就绪验收契约。

### Modified Capabilities

无。

## Impact

影响受管 Worker 安装、就绪诊断及资源配置，无 schema 变更。
