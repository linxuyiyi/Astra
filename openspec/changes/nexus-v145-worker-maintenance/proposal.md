## Why

当前 Linux 远程 Worker 的部署与重启由 HTTP 流内的 asyncio task 执行，浏览器断开就会取消远端操作；操作状态复用环境的 `status_detail.operation` JSON 作为锁和状态，失败还会统一把环境写成 `UNAVAILABLE`，导致维护事实、连接健康与可调度性混在一起。v1.4.5 需要先让维护操作独立持久化、可恢复、可显式取消，并且不破坏正在执行或等待人工确认的任务。

## What Changes

- 新增维护操作与事件表：操作拥有独立状态机、阶段、attempt、租约（owner/lease_until）、请求人、目标版本、前后快照、错误与 `cancel_requested`；步骤事件按操作独立存储并支持分页/SSE 续传。
- HTTP 只负责提交、查看、订阅、取消与重试；执行由现有 FastAPI lifespan 中的数据库驱动 executor 领取，客户端断线不取消运行中的操作。
- 用短事务原子 claim、租约续租与 fencing 防陈旧写；进程退出把自有未完成操作持久化为 `INTERRUPTED`，租约过期由恢复器先标记中断，重试前重新预检，不盲目重发 token 或重启。
- 维护锁改为单一事实源（操作表）。环境保存/检测/删除、Worker 通道切换、Task 启动与调度器统一读取该锁；任务占用继续使用现有 `occupancy.py` 的 canonical 判定。
- 维护动作执行前、写配置/换装备/重启前都做占用与原生 Session 检查；在线重启补齐原生 idle 检查；所有危险步骤前再次做数据库 fenced 复检。
- 维护失败不再直接把环境置为 `UNAVAILABLE`；连接、运行时健康、维护状态与 `schedulable` 分开返回。真实探测失败或结果未知时保守禁止调度。
- 复用现有 `RemoteProvisioner` 的 SSH/安装脚本/Worker 命令；重复部署识别有效 Worker token 与配置，避免无谓轮换；失败只清理未写入有效配置的新 Key。
- 新增 `/api/agent-workers/{environment_id}/maintenance`、`/api/maintenance/{id}` 及 events/cancel/retry、`/api/agent-workers/{environment_id}/health`。旧 `/api/employees/{employee_id}/environment/{deploy|restart}` 保留为统一 handler，供现有页面过渡。
- 本 change 的后端范围内不做：大规模历史命名替换、真实 VM 验收、自动回滚 token/数据库/Git。Web 已由同版 UI 阶段接入新维护 API。

## Capabilities

### New Capabilities

- `nexus-worker-maintenance`: Agent Worker 维护操作的持久化执行、租约恢复、任务/原生占用保护、显式取消与重试、健康与可调度状态分离、维护 API 与事件续传。

### Modified Capabilities

- 无。Nexus 不修改 `openspec/specs/` 中的 Astra 历史契约；旧部署接口只作为过渡适配保留。

## Impact

- 数据：新增 `worker_maintenance_operations`、`worker_maintenance_events` 及 Alembic migration；移除环境 JSON operation 作为锁与状态的长期来源。
- 后端：`app/runtime/remote_provisioning.py`、新增维护服务与 executor、`app/runtime/worker_channel.py`、`app/runtime/lifecycle.py`、`app/runtime/dispatcher.py`、`app/api/environment_routes.py`、新增维护路由、`app/main.py` lifespan、`app/models.py`。
- 过渡兼容：旧员工部署/重启路由保留响应形态，但内部改为提交并订阅持久化操作；前端本阶段不改。
- 未验证：真实 Linux Worker 的安装/重启、真实 SSE 网络断线、跨主机多 API 进程部署；本阶段以针对性单测和 SQLite 迁移验证为准。
