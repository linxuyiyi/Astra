# Nexus v1.5.0 SSH / OpenCode Runtime 验收

日期：2026-11-09。工作分支 `v1.5.0`，开始检视时 HEAD 为 `6a9b536cd`。本次直接修改当前 checkout，未发布到远端或公司环境。

## 原因与实际修改

旧 Worker Gateway 将原生 OpenCode HTTP/SSE 包装成命令投递、正文搬运、事件日志、游标、epoch 及跨进程路由。该中间状态层已经删除。

- `runtime/worker_connection.py` 按 Worker 管理 SSH 与一个 loopback 本地转发；`ssh_client.py` 在同一 Transport 上复用 TCP 通道。SSH exec 同时发送 stdin、读取 stdout/stderr，避免并发耗尽阻塞线程。
- `session_connection.py` 统一创建 `OpenCodeServerClient` 访问转发后的原生 HTTP/SSE。SSH 失效重连与资源关闭不启动或重启 OpenCode。
- Task 周期对账与 SSE 通知调用同一入口。未知提交先查询原生消息；保持原 Task、Worker、Session，空闲后串行重发；确认 Session 不存在时 WAITING，显式重新开始才新建。
- 新增 FAILED：当前轮最新原生 assistant 确认 error，且原生父子 Session 已不忙、没有交互请求时结束。旧消息和单条 SSE 错误不作为失败事实。
- Worker 连接事实取 SSH + 原生健康，保持 PAUSED 和调度开关独立；Worker、Task 和星系页面统一展示最近检查。
- 文件、Git、工作区准备和只读诊断通过 SSH 调用保留的远端本地辅助。OpenCode 进程监管独立运行，不建立网络 Agent。部署期间单独持有原有短生命周期 SSH/flock，避免运行时重连释放部署互斥锁。
- Task MCP 不再直接读写业务或调用 Runtime，通过既有内部密钥认证的 HTTP 入口复用 API 的 TaskTools；接口只允许显式列举的方法。Celery 调度只领取任务，由 API 巡检启动。复盘进程通过原有 Task 原生代理读取，避免这些独立进程各自建立运行时 SSH。
- 原生工作区继续使用 Task 作用域代理，绑定 Worker、目录、根/子 Session，保留权限与脱敏边界。

## 删除的组件

- `app/worker_gateway/` 全部模块，包括 WebSocket 路由、Redis/Memory Route Bus、HTTP DataChannel、EventLog、Consumer Cursor、通信 Lease、HTTP slot、背压控制和管理密钥。
- `WorkerOpenCodeClient`、`WorkerCommandTransport` 和 Worker Agent 的 connection/transport/local_opencode/trace_read 网络实现。
- `t_nexus_worker_commands`、`t_nexus_worker_http_slots`；Worker 的 token_digest、epoch、last_heartbeat_at、agent_version。
- 旧通信配置、Nginx 暴露路径、安装器密钥生成与网络包依赖，以及对应通信协议测试。

保留 `worker_tools` 的文件安全、工作区、诊断和进程监管辅助。其 wheel 只依赖 Python 标准库。AgentScope Redis、模型网关 Redis、维护日志/锁、经验复盘租约和业务 Outbox 继续保留。

空库 `0001_init` 对应最终模型：50 张业务表，Task 27 列，Worker 25 列；新增 Worker opencode_port、last_checked_at、opencode_version。按照本次不做历史迁移的要求，没有旧库升级和新旧双通道。

## 验证证据

| 检查 | 实际结果 |
| --- | --- |
| platform + nexus + MySQL 可选模块全量 | **806 passed / 24 skipped**（89.32 秒） |
| 最后补充的监管、wheel、部署、SSH/native、客户端及装备定向回归 | **44 passed**（包含新增加的进程退出恢复用例） |
| 隔离 MySQL 8.0.36 | **5 passed**：空库表字段与 ORM 精确一致、并发领取独占、并发维护互斥、SSH 断线保留任务/占用与 PAUSED、工作区准备持锁时取消立即返回可重试冲突 |
| 最终跨进程、Task 状态、复盘、内部认证与问答授权定向回归 | **81 passed / 1 skipped** |
| 桌面 Worker Playwright 场景 | **通过**：四态/调度组合、离线占用、诊断、刷新、分页、部署日志和离开清理；无页面脚本错误 |
| web 单元测试 | **72 passed** |
| chat-island 单元测试 | **17 passed** |
| web / chat-island 类型检查及生产构建 | **通过**；现有大 chunk 提示保留 |
| 最终真实 SSH / OpenCode 恢复补验 | **5 passed** |
| Ruff：app / nexus_api / tests | **通过** |
| git diff --check | **通过** |

全量里的 4 个 MySQL 模块用例及 1 个 MySQL 取消并发用例，另行在新建隔离实例运行通过。测试实例使用临时数据目录、独立端口及随机测试数据库；验证后已停止并清理，不读写既有 MySQL 业务库。

真实传输/原生集成覆盖：

1. 真实 Paramiko SSH TCP 转发，12 个并发原生 HTTP 请求复用一个 Transport；SSE 结束后重新订阅，关闭本地监听后拒绝新连接。
2. 10 个并发 SSH exec，同时发送 stdin 和读取输出，无阻塞线程饥饿。
3. 两个 Worker 使用不同原生服务，HTTP 请求隔离；切断实际 SSH Transport 后重连，另一 Worker 不受影响。
4. 当前配置指向的真实 OpenCode **1.18.33** 通过 SSH 完成健康检查、Session 创建、SSE session.created、idle 查询、children/question/permission 和 abort；释放隧道后进程仍存活；重新建立 SSH 后同一 Session 仍存在且 idle 状态一致。
5. 原生 busy/retry、提问/权限、人工确认、取消、当前轮 FAILED、旧轮错误隔离、Session 丢失等待人工、无 SSE 通知的周期对账和失败重试由任务生命周期回归覆盖。问答/权限回复通过真实 OpenCodeServerClient + HTTP MockTransport 核验请求参数和回复后的原生状态对账。
6. Task MCP 经认证内部 HTTP 调用统一 API；私有方法及无效密钥被拒绝。Celery 调度与复盘读取的测试禁止直接创建 SSH，复盘读取验证 Task 代理 URL。

关键命令：

```bash
cd services/nexus-api
.venv/bin/python -m pytest tests/platform tests/nexus tests/mysql/test_runtime_convergence.py -q
.venv/bin/ruff check app nexus_api tests
cd web && npm test && npm run type-check && npm run build
cd ../chat-island && npm test && npm run type-check && npm run build
```

## 未验证事项与约束

- 未重新部署公司 Linux Worker、Nginx 和公司控制机，未运行公司网络下的 SSH 故障注入。
- 真实 OpenCode 验证没有调用外部模型；真实模型的任务工程执行、问答/授权和业务验收尚未做端到端验证。
- 当前宿主为 macOS，Linux openat2/O_PATH 文件边界用例仍按平台条件跳过；真实 AgentScope、模型网关 Redis Lua、群聊 MySQL 与实际 Nginx 部署等外部环境用例也有跳过，不计入通过。
- 运行时 SSH 连接池按单 API 进程管理；标准部署维持单 API 进程，多 Worker 并发已验证。没有新增跨 API 进程路由或连接所有权中间件。
- 本次按空库最终结构替换 init，不提供既有数据库的升级路径。
