# Agent Worker 维护与恢复

本文只说明 Worker 环境的部署、检查、更新、重启、暂停与恢复。任务/Session 状态仍由 `services/nexus-api/app` 的现有状态机维护，维护操作不释放占用。

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

- 远程安装/更新/离线重启需要 SSH 管理通道，且当前一键部署要求 SSH 用户为 root：安装脚本直接写 `/etc/nexus`、`/etc/systemd/system` 并直接调用 `systemctl`，没有 sudo 升权路径。非 root 用户即使 SSH 可达也不受支持，不要通过扩展 sudo 架构来支持非 root。在线重启与在线探测优先使用 Worker 通道。
- 主控需要 `NEXUS_REMOTE_GATEWAY_LOCAL_URL`、`NEXUS_MCP_RESOURCE_SERVER_URL`、`NEXUS_WORKER_AGENT_GATEWAY_URL` 指向 Worker 可达的内网地址。
- `deploy/update` 需要 `NEXUS_MCP_TRANSPORT=streamable-http`；图书馆 MCP 不再要求共享 Bearer key，入口由 Worker 网段 ACL 限制。
- 图书馆 MCP 的 DNS rebinding 保护保持开启。允许的 Host/Origin 只来自 `NEXUS_MCP_RESOURCE_SERVER_URL`、`NEXUS_MCP_ISSUER_URL`、`NEXUS_MCP_HOST:NEXUS_MCP_PORT` 和 loopback；修改这些地址后需重启 MCP 服务。禁止使用 `*` 或请求自带 Host 扩大允许范围。
- 私有 Git 仓库的 token 只在部署时通过临时 SFTP 文件传入远端，不写入 operation payload、日志或 argv；Git 认证与模型 Key 的自动生成已在 x86_64/aarch64 真实 Worker 验收（见维护报告）。

## 每环境路径

- Node / npm：`$HOME/.local/share/nexus/workers/{environment_id}/node-v{version}`、`.../npm`
- OpenCode 目标入口：`$HOME/.local/share/nexus/workers/{environment_id}/bin/opencode`
- Worker Agent venv / wheel：`/opt/nexus/workers/{environment_id}/venv`、同目录 wheel 与 `wheel.sha256`
- Worker 环境文件：`/etc/nexus/worker-{environment_id}.env`
- systemd：`nexus-opencode-{environment_id}.service`、`nexus-worker-{environment_id}.service`
- 配置：`/etc/nexus/opencode/{environment_id}/opencode.jsonc`（备份后缀 `.nexus-backup-*`）

Worker env 与 systemd unit 更新时先写同目录临时文件，SFTP 回读校验通过后再 `mv -f` 原子替换；旧 env/unit 保留为 `<path>.nexus-backup`。同内容不写、不触发重启。

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

## 验证与边界

- 真实 Linux 验收：aarch64（Lima `nexus-v145-qa`）与 x86_64（CentOS 7）均完成 full deploy、同配置幂等、占用/原生 busy、Git、装备故障、重启与离线 SSH 恢复；命令、结果和证据见 `docs/reports/NEXUS_V145_WORKER_MAINTENANCE_20261019.md` 与 `docs/reports/evidence/nexus-v145-worker-maintenance/`。
- MySQL：未跑真实 MySQL 维护回归；无 MySQL 环境时相关方言用例跳过。
- 模型：只验证 `/models` 认证与模型可见性，未调用真实 LLM 推理。
- Git：HTTP Git/PAT 使用自有私有 fixture，不代表第三方托管或用户生产凭据。
- 安装故障：Node/npm/pip 安装失败由 Linux 安装脚本 fixture 验证，未通过真实断网破坏全局包。
