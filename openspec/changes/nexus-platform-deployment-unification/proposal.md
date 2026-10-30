## Why

平台安装器同时维护 systemd 与后台进程分支，配置被多处默认值和生成文件覆盖，升级与健康检查不构成完整闭环。需要把首次部署、升级和配置纠错统一为一个可验证入口。

## What Changes

- **BREAKING** 平台部署取消 systemd 和开发模式选择，唯一入口协调 Python 受管进程、前端构建、Nginx 与验收。
- 配置加载后诊断重复、冲突、失效地址及真实连接，正确值复用，敏感值保留，修改前备份。
- 安装版本与来源使用独立可持久化清单，本地与公司仅切换来源；无隐式公网回退。
- HTTPS Web 与限定网段的 Worker HTTP 入口统一生成，内部服务保持回环绑定。

## Capabilities

### New Capabilities

- `nexus-platform-deployment`: 统一安装升级、配置诊断、下载源、网络发布、进程身份和完整验收。

### Modified Capabilities

无。不修改 Astra 历史契约。

## Impact

涉及 ops/deploy、配置加载、部署测试和部署文档。复用既有进程原语，不修改 Agent Worker 远程部署实现，不改变业务状态机或 AgentScope 数据所有权。
