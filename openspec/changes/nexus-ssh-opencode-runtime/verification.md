# nexus-ssh-opencode-runtime 核验

日期：2026-11-09。使用仓库 openspec-verify-change 的完整性、正确性和一致性维度核实；CLI strict validate 通过。

| 维度 | 结果 |
| --- | --- |
| 完整性 | tasks.md 的 6/6 项完成；4 个 Requirement / 5 个 Scenario 均有实现与针对性证据 |
| 正确性 | 原生 HTTP/SSE、状态投影、恢复和隔离对应同一 Runtime 与 Task 权限边界 |
| 一致性 | 只有统一 API 持有运行时 SSH；无旧通信双通道或新 Session 表 |

## 场景与证据

- 并发访问/关闭：`ssh_client.LocalForward`、`worker_connection.WorkerConnections`；test_ssh_runtime 的真实 SSH 并发 HTTP、stdin exec、SSE 订阅、释放与真实 OpenCode 用例。
- 独立进程连接所有权：Task MCP 经 task_tool_routes 的内部认证 HTTP 入口；Celery dispatch 只领取，API recovery 启动；trace_reader 经 Task 原生代理。test_task_mcp_auth、test_runtime、test_experience_review 覆盖认证、私有方法拒绝及禁止独立进程创建 SSH。
- 通知触发核实：lifecycle 的原生状态/问题/权限/当前轮错误投影；recovery 周期巡检。test_v150_architecture 覆盖完整生命周期、当前轮 FAILED、旧轮错误隔离、无事件对账及失败重试；真实 OpenCodeServerClient 的问答/授权 HTTP 请求与后续对账另有回归。
- 连接恢复：真实 SSH Transport 切断后重建，多 Worker 不串用；真实 OpenCode 连接释放后同一 Session 可恢复查询；MySQL 检查断线不解除 Task 占用、PAUSED 不被恢复。Session 丢失等待人工、未知提交保持原会话由生命周期回归覆盖。
- 多 Worker 并行：真实两个原生 HTTP 服务的 SSH 连接隔离；MySQL 并发领取/维护互斥；Task 代理绑定、父子 Session 交互和越界拒绝回归。

完整测试数量与边界见 docs/reviews/nexus-ssh-runtime-20261109.md。最近全量 806 passed / 24 skipped；最后新增代理读取与问答授权用例另外定向通过。跳过的 5 个 Runtime MySQL 场景另行运行通过，不把其他外部环境跳过算成通过。

## 风险与未验证

没有实现层面的 CRITICAL 遗漏。公司 Linux 重新部署、真实模型工程执行/交互及完整原生工作区业务端到端未验证；Linux 文件 syscall 用例当前宿主不能运行。连接池按标准单 API 进程管理。OpenSpec 新契约已同步，保留本次 change 供审阅，没有修改历史 Astra capability。
