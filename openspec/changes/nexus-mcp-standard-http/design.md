## 设计决策

- **注册面**：AgentScope 2.0.10 没有面向 Nexus 的全局 MCP 注册接口；`POST /workspace/mcp` 是官方按会话登记入口。继续复用它，不新增注册表或代理。Library MCP 面向 Agent Worker，不经 AgentScope 注册。
- **鉴权分层**：MCP 中间件只把来源头写入上下文；`/health`、`initialize`、`tools/list` 不查业务状态。Task 工具调用走现有 `authorize_task_source`，Schedule 工具调用走现有 Nexus 内部私聊接口。这样既保持原有员工/会话/任务/日程权限，又不让同步依赖运行中的讨论。
- **无共享 key**：内网 MCP 由监听地址和 Worker 网段 ACL 限制。Task/Schedule 使用来源头；Library MCP 的写入能力仍由 `library_*` 工具自身的 `runtime_context` 校验。删除 `NEXUS_MCP_API_KEY` 等生成和注入，Worker 旧配置在新一次 provisioning 时由 `merge_config` 替换。
- **同步状态**：`sync_task_capability` 返回带 `stage` 的结构化失败；员工 capability 增加 `task` 字段并复用现有 `runtime_status`/`error` 展示。技能上传和 MCP 注册分别捕获，互不阻断。
- **部署验收**：生命周期层用标准 MCP HTTP 请求做 `initialize`/`tools/list`；应用部署层用 `AgentScopeClient` 创建临时 Agent/Session，经官方 `/workspace/mcp` 添加 Task/Schedule，读取健康状态和工具后删除。临时 Agent 没有员工目录、任务或业务数据。

## 舍弃方案

- 不为 Task MCP 增加“发现模式”或伪造员工/会话/讨论 ID。
- 不把 MCP 改成静态全局配置；员工和会话来源仍由 AgentScope 官方 workspace 机制按会话携带。
- 不新增鉴权框架、代理层或签名机制。
- 不修改员工、任务或 Runtime 状态机，也不新增同步状态表。

## 影响

- 部署配置会在升级时删除旧 MCP key；其他密钥和数据库配置按既有原子备份逻辑保留。
- Task MCP 在群讨论结束后仍可被发现，但工具调用会按原规则拒绝；这是连接与业务执行分离的预期结果。
- AgentScope 删除临时 Session 后工作区目录可能按官方设计保留；验收不创建员工目录或任务数据。
