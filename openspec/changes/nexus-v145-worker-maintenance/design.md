## Context

基线 `ae6bc2e3e` 的维护实现把执行放在 `environment_routes.py` 的 `StreamingResponse` 内，流关闭的 `finally` 会取消 `RemoteProvisioner.run()`；锁与操作状态写在 `opencode_execution_environments.status_detail.operation`，`worker_channel.py` 也共享这段 JSON。任务占用已有 canonical 事实源 `app/runtime/occupancy.py`，不能另造一套占用判断。

## Goals / Non-Goals

**Goals:**

- 维护操作与事件独立持久化，HTTP 只提交/查看/订阅，断线不取消。
- 数据库驱动的租约 executor，支持多进程 claim、续租、fencing 与崩溃恢复。
- 维护锁成为单一事实源，覆盖保存/检测/删除、Worker 通道、Task 启动与调度。
- 危险步骤的占用与原生 idle 保护，维护失败与运行时健康分离。
- 保留并复用 `RemoteProvisioner` 的 SSH/安装脚本/Worker 命令。

**Non-Goals（本阶段不做，不是 v1.4.5 整体范围外）:**

- Web、Git 完整管理、装备详细核验、`ASTRA_*` 大规模改名与真实 VM 验收属于同一 v1.4.5 的其它阶段。Web 已接入新维护 API 并删除旧调用；真实 Linux 验收仍在进行。
- 不承诺自动回滚 Git 工作区、数据库或凭据。
- 不新建服务、消息框架或第二份实施计划。
- 不把维护任务放进 solo `nexus-control` Celery 队列。

## Decisions

### 数据模型与单一事实源

新增 `worker_maintenance_operations`：

| 列 | 说明 |
| --- | --- |
| `id` | 操作 ID（uuid hex） |
| `environment_id` | Worker 环境 ID，索引 |
| `action` | `check` / `deploy` / `update` / `restart` / `disable` / `restore` |
| `state` | `QUEUED/PREFLIGHT/RUNNING/VERIFYING/SUCCEEDED/FAILED/CANCELED/INTERRUPTED` |
| `stage` | 当前阶段名 |
| `requested_by` | 请求人（沿用内网调用方，不扩展 RBAC） |
| `attempt` | 从 1 开始，每次显式重试递增 |
| `lease_owner` / `lease_until` | 执行者 ID 与租约截止 |
| `cancel_requested` | 显式取消标记 |
| `danger_dispatched` | 已发出可能改变远端状态的危险命令 |
| `target_revision` | 目标版本/装备摘要（JSON） |
| `before_snapshot` / `after_snapshot` | 维护前后快照（JSON） |
| `error_code` / `error_message` | 失败/中断原因 |
| `retry_of_attempt` | 重试来源 attempt（可空） |
| `active_environment_id` | 活动期间等于 `environment_id`，终态置 NULL |
| 时间列 | `requested_at/started_at/finished_at/created_at/updated_at` |

`active_environment_id` 上的唯一索引在 SQLite 与 MySQL 上都能用（多 NULL 允许），从而跨进程保证一个环境同时只有一个非终态维护操作。`INTERRUPTED` 仍保留该值，表示环境尚未核实，禁止新维护与调度；显式 `CANCELED` 或成功核实后的 `FAILED/SUCCEEDED` 才释放。

新增 `worker_maintenance_events`：自增 `id` 作为 SSE event id，`operation_id`、`attempt`、`type`、`stage`、`name`、`state`、`message`、`detail`、`created_at`。事件持久化前脱敏。分页按 `id > after_id`。

不再新增或长期读取 `status_detail.operation`。`status` 继续表示运行时健康（`AVAILABLE/UNKNOWN/UNAVAILABLE/CHECKING`），维护状态只来自操作表。

### 提交与共享原子锁

提交在短事务内：

1. 对环境行执行条件 no-op `UPDATE`（SQLite 写锁，MySQL 行锁）作为共享原子锁。
2. 用 `occupying_exists` / `blocking_session_exists` 检查 canonical 占用；只读 `check` 跳过占用拒绝。
3. 插入操作行（`active_environment_id` 唯一约束兜底并发）。
4. 提交。

`guard_environment_write` 改为咨询操作表：有非终态维护则 409。`RuntimeService.start`、环境保存/检测/删除、Worker 通道切换、调度器都使用同一判定。锁顺序固定为环境行 → 操作行，避免死锁。

### 执行器与租约

`app/runtime/maintenance.py` 提供：

- `submit_operation` / `get_operation` / `list_operations` / `append_event`。
- `claim_next(owner)`：条件更新 `QUEUED` 且租约为空/过期，写 `lease_owner`、`lease_until`、`state=PREFLIGHT`、`attempt`。
- `renew_lease(operation, owner)`：条件 `id+attempt+lease_owner+state`，失败即 fencing 丢失。
- `finish_operation`：条件 `id+attempt+lease_owner` 写终态与快照。
- `recover_expired()`：只处理 `lease_until < now` 的非终态操作，写 `INTERRUPTED`、`error_code=lease_expired`，保留 `active_environment_id`。
- `cancel_operation` / `retry_operation`。

FastAPI lifespan 新增 `maintenance_executor()` 任务，与 `supervise_sessions` / `sweep_worker_gateway` 并列：每隔短周期 recover 并 claim 一个操作；运行 `RemoteProvisioner` 时启动续租任务。续租失败立即取消执行（fencing），不写终态。lifespan 退出时把本进程持有的未完成操作以 shield 事务写为 `INTERRUPTED`。不使用 Celery 队列、不 `create_task` 后失去恢复能力。

### 阶段、保护与失败语义

- `PREFLIGHT`：只读端点、SSH/Worker 可达、仓库/配置存在性、占用与原生 idle；不写配置。
- `RUNNING`：安装/写配置/换装备。
- `VERIFYING`：健康、MCP、配置生效与 capability 观察。
- 每次危险步骤前调用 `assert_safe_to_disrupt()`：短事务 fenced 复检操作租约 + canonical 占用；原生 idle 检查后再次复检。
- `restart` 在线分支必须先读 Worker OpenCode `/session/status`，发现 busy/retry 拒绝；离线分支沿用 SSH 原生检查。
- `check` 永远只读，可在占用时执行。
- 维护成功写 `environment.status=AVAILABLE` 和实际版本；失败只记录操作错误。危险步骤已发出去后失败/取消，先做只读核对：健康则保留可用，不健康或未知则置 `UNKNOWN` 并禁止调度；`INTERRUPTED` 保持维护锁。
- 不使用 placeholder `unmanaged`、PID 或端口监听作为健康证据。

### 远程执行的幂等与凭据

- 保留 `remote_install.sh`、`_install`、`_gateway_key`、`_probe_gateway`、`_probe_mcp`、`_restart_*`。
- installer payload 固定 `{action: deploy|update, environment_id, opencode_server_port, config_path, repository{url,branch}}`；解析 `NEXUS_STEP` 与 `NEXUS_META`。每环境 Node/npm 在 `$HOME/.local/share/nexus/workers/{id}`，venv/wheel 在 `/opt/nexus/workers/{id}`，目标 OpenCode 入口是本环境 `bin/opencode`，不写全局 `/usr/local/bin`。
- wheel 引用 `worker_agent.__version__`、包名 `nexus-worker-agent`、固定时间戳/排序，源码相同则 bytes/digest 相同。env/unit/config/wheel marker 只在内容变化时写；仅必要才 pip、daemon-reload、重启 Worker/OpenCode。systemd 写 `WorkingDirectory=workspace` 与每环境 Node/npm PATH。
- `_install_units` 先读取远端现有 Worker env 与 DB 登记 token（`WorkerInstance.token_ciphertext` 可恢复）：digest 一致时沿用，不调用 `enroll`；远端与 DB 不一致或无法证明时明确要求人工修复，不盲目轮换。
- 装备期望/实际/差异由 `app/runtime/worker_health.py` 的 `desired_equipment` / `probe_equipment` / `equipment_rows` / `repository_payload` 提供；check、deploy/update/restart 结束后都做完整探测，`status_detail` 保存 `observed/deployed` 摘要，desired 仍只从 DB/Skill 源派生。
- `_gateway_key` 继续读取远端现有 Key 并复用。
- 新增 `key_applied` 标记：失败时只撤销尚未写入远端配置的新 Key；已写入配置的 Key 不撤销。
- 重试从 `PREFLIGHT` 开始，复用远端有效配置，不盲重发危险命令。

### API 契约（供 UI 阶段复用）

所有时间使用 UTC ISO8601；错误统一 `{"detail": "..."}`。

`POST /api/agent-workers/{environment_id}/maintenance`

```json
{"action": "check|deploy|update|restart|disable|restore", "requested_by": "console-admin"}
```

`disable` 只写现有 `DigitalEmployee.enabled=false`，不停止 OpenCode、不释放 Task；`restore` 先做真实健康探测，通过后才重新启用。`update` 使用 installer 的 `update` 语义（`fetch` + `ff-only`），已有仓库在 `deploy` 下只核对。

返回 `202`：

```json
{
  "id": "op_hex",
  "environment_id": 12,
  "action": "deploy",
  "state": "QUEUED",
  "stage": "queued",
  "attempt": 1,
  "requested_by": "console-admin",
  "requested_at": "2026-10-19T08:00:00Z",
  "lease_until": null,
  "cancel_requested": false,
  "error_code": null,
  "error_message": null
}
```

`GET /api/agent-workers/{environment_id}/maintenance?limit=50&before_id=` 返回 `{"items": [Operation...]}`。
`GET /api/maintenance/{operation_id}` 返回 Operation 加 `events`（最近 N 条）与 `before_snapshot/after_snapshot/target_revision`。

`GET /api/maintenance/{operation_id}/events?after_id=0` 为 SSE：

```text
id: 42
event: step
data: {"id":42,"operation_id":"op_hex","attempt":1,"type":"step","stage":"preflight","name":"任务占用","state":"passed","message":"无占用","created_at":"..."}
```

支持 `Last-Event-ID` 请求头（优先）与 `after_id` 查询参数；终态且事件发完后发送 `event: end` 并关闭。心跳为注释行 `: keepalive`。

`POST /api/maintenance/{operation_id}/cancel` 返回 Operation；对 `QUEUED/INTERRUPTED` 直接 `CANCELED` 并释放锁；对运行中设置 `cancel_requested`，执行器在安全检查点处理。

`POST /api/maintenance/{operation_id}/retry` 仅允许 `FAILED/INTERRUPTED/CANCELED`；同一操作行 `attempt += 1`，重置状态/阶段/错误，保留 `active_environment_id`，重新进入 `QUEUED`；有其它活动操作时 409。来自 `INTERRUPTED` 的重试先进入只读 `reconcile`：确认远端 installer flock 已释放、固定 request id 的 Worker restart 指令没有未决结果、真实健康与原生 Session 空闲后才继续。

`POST /api/maintenance/{operation_id}/reconcile` 仅允许 `INTERRUPTED`；以新 attempt 做同一只读核对。核对包括远端 installer `flock`（realpath workspace hash）、固定 `maint:{operation}:{attempt}` 的 Worker 指令 delivery/result、真实 `/global/health`（必须 `healthy=true`）与原生 Session。确认没有晚到危险命令后结束旧操作并释放维护锁；健康不好时仍可释放但环境保持 `UNKNOWN/UNAVAILABLE`，允许显式 retry/deploy 恢复。无法确认则保持 `INTERRUPTED`。`INTERRUPTED` 或 `reconcile_required` 的操作在取消时必须保留维护锁。`GET /events/page?after_id&limit` 返回持久事件分页；SSE 在终态或 `INTERRUPTED` 时 drain 全部事件后发 `end`。

`GET /api/agent-workers/{environment_id}/health` 返回 UI 已对齐的结构：

```json
{
  "environment_id": 12,
  "employee_id": "emp_x",
  "enabled": true,
  "connection": {"channel": "worker", "state": "online", "last_heartbeat_at": "...", "epoch": 3},
  "maintenance": {"state": "SUCCEEDED", "operation_id": "op_hex", "stage": "verify", "attempt": 1},
  "runtime_health": {
    "status": "AVAILABLE",
    "last_checked_at": "...",
    "capability": {},
    "equipment": [{"key": "opencode", "label": "OpenCode", "configured": true, "deployed": true, "verified": true, "desired_revision": "...", "actual_revision": "...", "diff": [], "error": "", "detail": {}}]
  },
  "repository": {"id": "repo", "url": "...", "target_branch": "main", "branch": "main", "commit": "...", "dirty": false, "authentication": "token", "error": null},
  "schedulable": false,
  "blocking": [{"code": "occupied_task", "message": "员工存在占用任务", "task_id": "T1"}]
}
```

健康观测由 lifespan 周期只读探测，带 `observed_at`/`observed_epoch` freshness 与 epoch gate；调度和手动 start 在观测过期、连接离线、装备未应用或活动维护时拒绝。维护失败本身不改写此前健康；只有真实探测失败/未知才写 `UNAVAILABLE`/`UNKNOWN`。HTTP 409 允许 `{code, message, blocking}`。

旧 `POST /api/employees/{employee_id}/environment/{deploy|restart}` NDJSON handler 与 `/environment/test` 已确认没有 UI 消费者，随本阶段删除，不保留历史执行或 HTTP 等待旁路。旧 `GET /api/workers` 与前端 `/workers` 重定向保持不变。

## Risks / Trade-offs

- 租约过期采用保守 `INTERRUPTED` + 保留锁，可能要求人工核对或显式取消；这是为避免盲目重发危险命令的代价。
- SQLite 与 MySQL 的锁语义不同；通过环境行条件写 + 活动环境唯一索引双保险，真实 MySQL 并发仍需集成验证。
- 在线重启的原生 idle 检查依赖 Worker OpenCode 接口，接口不可达时按“未知”拒绝危险操作。
- 真实 SSH/systemd/token/端口和 Linux 权限未在本阶段执行，需在真实 Worker 验收中补充。
