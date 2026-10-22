## Why

远程 Worker 在无 systemd 的 Linux 容器中无法完成部署和重启，HTTP(S) 入口、配置继承与健康持久化还存在缺陷。逐项核对公司问题单，修复真实问题并保留维护隔离、运行态核验与任务仓库绑定。

## What Changes

- 支持 systemd 和无 systemd 的显式进程管理，保证安全停止、配置继承与 SSH 返回。
- 统一 Worker WebSocket 与数据通道地址转换，补齐 Nginx Upgrade，保留 TLS 验证。
- 恢复 OpenCode 重启、运行配置就绪等待与网关模型约束。
- 修复 MySQL 健康写入而不移除并发隔离。
- 明确 Python 引导和进程配置重载，提供启用分类查询 MCP。
- 补充任务分配修复：取消任务与 Worker 的仓库 ID 一致约束，按任务类型与数字员工筛选；未配置数字员工时使用公共池，任务分类不限制接单。
- 语音输入改为绑定网关 ASR 路由；网关增加语音识别类型、音频转写代理与探测，独立本地 ASR 提供兼容接口。

## Capabilities

### New Capabilities

- `nexus-container-worker`：容器 Worker 部署、传输、安全重启与健康观测。
- `nexus-task-category-discovery`：认证员工读取已启用任务分类。
- `nexus-task-worker-routing`：任务类型、数字员工与公共 Worker 池的一致分配规则。
- `nexus-gateway-speech-input`：统一网关语音识别模型与聊天语音输入配置。

### Modified Capabilities

无。不修改 Astra 历史契约。

## Impact

涉及 remote installer/provisioner、Worker Agent、健康持久化、任务调度/启动/API/MCP/表单、Nginx 模板和部署文档；保留任务与代码仓关系，但仓库不再作为分配条件，不接管 AgentScope 内部状态。
