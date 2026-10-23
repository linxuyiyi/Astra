## Why

真实 Worker 与经验沉淀尚未整体验收。实测发现统一服务鉴权、任务仓库目标、正式执行上下文、复盘读取和经验使用归因存在集成断点，导致任务无法执行或经验不能形成可追溯的复用闭环。

## What Changes

- Worker 协议路由继续由原有 token、epoch 与管理密钥校验，不再要求浏览器 Cookie。
- Nexus 统一服务的维护操作人使用服务端登录会话，忽略客户端身份头和旧 UID Cookie。
- Task MCP 提供仓库发现，创建和改派任务校验确定的目标仓库。
- 正式模型收到受控工具上下文；复盘轨迹脱敏，合法任务 worktree 可读，成功恢复后清除旧错误并关联证据。
- Wiki 编译来源页的读取关联确定的已发布经验；知识审核与执行停止确认记录真实登录账号。
- 取消任务详情显示占用事实，并提供既有人工停止确认入口。
- 实测数字员工建任务、Linux Worker 执行、人工确认、后台复盘、审核发布和后续任务检索复用。

## Capabilities

### New Capabilities
- `nexus-unified-worker-auth`: 统一服务中人类维护身份与 Worker 协议鉴权的明确边界。
- `nexus-task-repository-selection`: 可发现、确定且与 Worker 一致的工程任务仓库。
- `nexus-experience-reuse`: 执行工具上下文、复盘证据与正式经验读取的可追溯闭环。

### Modified Capabilities

无。复用 `nexus-experience-review` 与现有生命周期契约；不修改历史 Astra Spec。

## Impact

统一服务、Task MCP、Worker 轨迹读取、经验复盘、图书馆与任务详情。复用既有任务生命周期、占用谓词和知识存储；无 schema 变更。已完成旧复盘的错误与证据关联通过显式目标、默认预览的一次性脚本修复。验证使用隔离 Nexus/AgentScope/Redis、Linux QA 工作区和真实网关；经验发布连接已配置的真实 Wiki 项目。
