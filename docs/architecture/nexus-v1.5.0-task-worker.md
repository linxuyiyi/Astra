# Nexus v1.5.0 Worker 通信与 Task Runtime

更新日期：2026-11-09。

## 调用关系与事实源

```text
数字员工 / 人工 → Nexus Task → AgentWorker → SSH 本地转发 → OpenCode HTTP / SSE
原生工作区浏览器 → Nexus Task 作用域代理 ────────────────────┘
部署 / 文件 / Git / 诊断 → SSH exec / SFTP → 远端 worker_tools
人工确认 → ExperienceReviewJob 来源快照 → 同一原生 HTTP 消息分页 → 知识候选
```

公司执行模式为 `ASTRA_EXECUTOR_TYPE=worker`，控制机不启动、安装或校验本机 OpenCode。`opencode_server` 仅供本机开发。AgentScope 的 Agent、聊天 Session 与 Memory 仍由独立 Agent Service 管理。

Task 保存业务状态、独占 Worker、Workspace、当前原生 Session 指针及尚未被原生服务接受的输入。OpenCode 管理原生 Session；Nexus 不复制 Session 状态机、事件历史或执行步骤。

## SSH 与原生服务

`WorkerConnections` 按 Worker 复用 Paramiko 连接和一个仅监听 `127.0.0.1` 的随机本地转发端口。所有 OpenCode HTTP/SSE 由 `OpenCodeServerClient` 访问转发地址，远端目标固定为 `127.0.0.1:<opencode_port>`。多个 TCP 通道共享 SSH Transport，SSE 不占用阻塞读取线程。

Worker 保存 SSH `port` 和独立的 `opencode_port`。后者可以登记已有服务端口，留空时由一键部署分配并持久化。SSH 与 Git 凭据继续加密保存，读取 API 仅返回是否配置。

健康巡检直接验证 SSH 与 OpenCode 健康及固定版本；最近检查时间只用于展示。连接失效会释放本地转发并重建，关闭 Nexus 只释放连接，不停止远端 OpenCode。Worker 配置变更及删除关闭对应连接。

Task MCP 经内部认证 HTTP 调用统一 API 的 TaskTools；Celery 调度只领取任务，API 巡检启动执行；复盘进程经已有 Task 原生工作区代理读取。它们都不直接建立运行时 SSH。连接池是 API 进程内资源，标准部署使用单个 API 进程；多个 Worker 并发运行各自任务。同一 Worker 仍只独占一个 RUNNING / WAITING Task。部署阶段单独持有短生命周期 SSH 连接及远端 flock，运行时连接继续由连接池管理。

远端 `worker_tools` 只保留独立 OpenCode 进程监管与 SSH 调用的本地辅助，不再建立主动网络连接。文件和 Git 边界复用原有受限路径、openat2、原子上传与只读 Git 操作；运行时 HTTP/SSE 不通过辅助入口转发。

## 状态、占用与恢复

Task：PENDING / RUNNING / WAITING / SUCCEEDED / FAILED / CANCELED。
Worker：OFFLINE / IDLE / WORKING / PAUSED。连接在线事实独立于 PAUSED 和调度开关。

- 原生根或子 Session busy / retry → RUNNING。
- 原生 question / permission → WAITING，保留占用并支持原生回答与授权。
- 原生 idle → WAITING / confirmation；当前完成规则为人工核验验收标准后确认 SUCCEEDED。
- 仅当前输入对应的最新原生 assistant 结果确认 error，且父子 Session 均不忙、没有待答交互时 → FAILED。单条 SSE error、传输失败或旧轮消息均不能确认失败。
- 取消先停止父子 Session 并查询确认不再 busy / retry，然后 → CANCELED。未确认停止继续占用；强制释放仍要求操作者原因和审计。
- SUCCEEDED 不可重启；FAILED、CANCELED 和 session_missing 的等待任务可显式重新开始。

SSE 仅唤醒对账。后台订阅自动重建，周期巡检也查询原生状态；没有通知或对账失败都不会停止后续核验。没有 EventLog、Consumer Cursor、事件回放、通信 Lease 或 SSE 背压控制协议。

SSH 断线保留 Task 原状态、Session 指针和 Worker 占用。重连后重新查询原生状态。确认 Session 不存在则进入 WAITING / session_missing，等待人工处理，不自动创建替代 Session。

发送前持久化 pending 消息与正文。未知提交先查原生消息是否已接受；原生仍忙则等待，空闲后串行重发同一输入，接受重复执行。不在重发时停止已有执行，不承诺 exactly-once。原生明确 4xx 拒绝输入进入 WAITING / input_rejected，并清除 pending。

## 页面、权限与维护

`/api/workers` 保留注册、配置、调度和删除；`/maintenance` 保留部署、检查、更新、重启、取消、重试和维护日志。原生工作区仍通过 Task 作用域代理，固定 Worker、目录和 Session 树，并对其他 Task 的 Session、路径覆盖及子 Session 写入进行拒绝。配置密钥继续脱敏。

部署运维日志的分页/SSE、维护互斥锁、维护结果未知的 `stop_unconfirmed`、经验复盘租约以及唤醒 Outbox 都是独立业务能力，继续保留。它们不参与运行时通信或 Session 状态判定。

## 数据与删除组件

空库使用最终 `0001_init`，不提供旧库迁移和兼容运行。全部业务表见 [schema 清单](nexus-v1.5.0-schema.md)：50 张业务表；Task 27 列、AgentWorker 25 列。

已删除 Worker Gateway 路由及 WebSocket、HTTP DataChannel、Redis Route Bus、命令投递/epoch/Token 体系、事件日志/游标/背压，以及 `WorkerOpenCodeClient`、Worker Agent 网络守护和对应测试。

删除 `t_nexus_worker_commands`、`t_nexus_worker_http_slots`；Worker 删除 `token_digest`、`epoch`、`last_heartbeat_at`、`agent_version`，改为 `opencode_port`、`last_checked_at`、`opencode_version`。部署 Nginx Worker 入口仅保留模型 `/v1/` 和图书馆 `/mcp`；旧通信管理密钥和连接地址不再生成。

本次自动化验证及环境限制记录于 [验收记录](../reviews/nexus-ssh-runtime-20261109.md)。历史 reviews 不作为本次验证结论。
