# Agent Worker 维护与恢复

本文只说明 Worker 环境的部署、检查、更新、重启、暂停与恢复。任务/Session 状态仍由 `services/task-platform` 的现有状态机维护，维护操作不释放占用。

## 动作语义

| 动作 | 是否改远端 | 是否要求空闲 | 说明 |
| --- | --- | --- | --- |
| `check` | 否 | 否 | 只读探测 OpenCode、模型认证、MCP、Skill 与 Git；可在任务占用时执行 |
| `deploy` | 是 | 是 | 建/校验每环境 Node、OpenCode、Worker Agent、仓库与配置；已有仓库只核对，不自动 fetch |
| `update` | 是 | 是 | 只对已绑定仓库执行 `fetch` + `ff-only`；工作区脏、分叉或分支不一致时停止并保留现场 |
| `restart` | 是 | 是 | 在线优先走 Worker 通道，离线走 SSH；重启前检查数据库占用与原生 Session |
| `disable` | 否 | 否 | 设置现有 `DigitalEmployee.enabled=false`，只暂停新调度；不停止 OpenCode，不取消现有 Task |
| `restore` | 否（只读探测） | 否 | 真实健康通过后恢复 `enabled=true`；健康坏时保持停用，先 `restart` 再重试 |
| `reconcile` | 否 | 否 | 只用于 `INTERRUPTED`：核对 installer 锁、晚到 Worker 指令与真实健康，成功后结束旧锁 |

所有动作写入 `worker_maintenance_operations`，步骤写入 `worker_maintenance_events`。HTTP 只提交与订阅，浏览器断线不会取消执行。

## 运行前提

- 远程安装/更新/离线重启需要 SSH 管理通道，目标用户需能使用 `sudo`/root 安装系统依赖（Node、Git、flock 等）；在线重启与在线探测优先使用 Worker 通道。
- 主控需要 `NEXUS_REMOTE_GATEWAY_LOCAL_URL`、`NEXUS_MCP_RESOURCE_SERVER_URL`、`NEXUS_WORKER_AGENT_GATEWAY_URL` 指向 Worker 可达的内网地址。
- `deploy/update` 需要 `NEXUS_MCP_TRANSPORT=streamable-http` 与 `NEXUS_MCP_API_KEY`。
- 私有 Git 仓库的 token 只在部署时通过临时 SFTP 文件传入远端，不写入 operation payload、日志或 argv；Git 认证与模型 Key 的自动生成仍需在真实环境验收。

## 每环境路径

- Node / npm：`$HOME/.local/share/nexus/workers/{environment_id}/node-v{version}`、`.../npm`
- OpenCode 目标入口：`$HOME/.local/share/nexus/workers/{environment_id}/bin/opencode`
- Worker Agent venv / wheel：`/opt/nexus/workers/{environment_id}/venv`、同目录 wheel 与 `wheel.sha256`
- Worker 环境文件：`/etc/nexus/worker-{environment_id}.env`
- systemd：`nexus-opencode-{environment_id}.service`、`nexus-worker-{environment_id}.service`
- 配置：`/etc/nexus/opencode/{environment_id}/opencode.jsonc`（备份后缀 `.nexus-backup-*`）

不共享旧的 `/opt/nexus/worker/venv`，也不把 OpenCode 安装到 `/usr/local/bin/opencode`。

## 启动与恢复

- 维护操作由 FastAPI lifespan 中的数据库执行器领取，使用短事务 claim、租约续租与 attempt/owner fencing；进程退出会把自有未完成操作写为 `INTERRUPTED`。
- 租约过期由恢复器标记 `INTERRUPTED`，但不会自动释放维护锁，也不会因为时间过去就让未知状态 Worker 接任务。
- `INTERRUPTED` 或 `reconcile_required` 的操作必须先 `POST /api/maintenance/{id}/reconcile`（或显式 retry，重试会先做同一只读核对）。核对确认没有晚到 installer / Worker 指令后才结束旧锁；无法确认时保持 `INTERRUPTED` 并给出原因。
- 危险步骤已发出后取消会进入 `INTERRUPTED`，不能直接恢复调度；需要先核对或显式重试。
- 不承诺自动回滚 Git 工作区、数据库、token 或 Agent 配置；失败时保留现场并由人工决定。

## API 摘要

```text
POST /api/agent-workers/{environment_id}/maintenance
GET  /api/agent-workers/{environment_id}/maintenance
GET  /api/agent-workers/{environment_id}/health
GET  /api/maintenance/{operation_id}
GET  /api/maintenance/{operation_id}/events          SSE，支持 Last-Event-ID
GET  /api/maintenance/{operation_id}/events/page     after_id + limit 分页
POST /api/maintenance/{operation_id}/cancel
POST /api/maintenance/{operation_id}/retry
POST /api/maintenance/{operation_id}/reconcile
```

旧 `/api/employees/{employee_id}/environment/{deploy|restart}` NDJSON 与 `/environment/test` 入口已确认无消费者并删除，维护统一走新 API。

## 未验证

真实 Linux x86_64/aarch64 上的 systemd、权限、私有 Git、模型 Key 生成、断网重连与故障注入仍需独立验收；本仓库只完成静态实现、针对性测试与 SQLite 迁移验证。
