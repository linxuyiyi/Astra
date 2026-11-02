## Context

当前 `v1.4.9` 分支的 Agent Worker 部署与状态探测横跨 24 个核心文件逾万行代码，存在五处结构耦合：
1. 环境表 `t_nexus_opencode_execution_environments` 同时承担“期望配置”与“探测结果/已部署快照”写入，导致保存任意环境字段会抹掉部署态并争抢行锁；
2. 远端无 `systemd` 时以两套 `{name}-process.json` 管理平级裸进程，`opencode serve` 崩溃后无人拉起；
3. 部署收尾仍保留历史 `ssh -> worker` 通道切换与四重串行探测；
4. 维护状态机引入 `INTERRUPTED + /reconcile`，前后端状态判定不一致导致 `409` 死锁；
5. DSH 代码穿插于 22 个核心公共模块。

本设计遵循已确认的工程边界：**完全不使用 `systemd`**、**完整保留已验证的 Python 3.11 / Node / OpenCode 引导模块**、**DSH 归档独立分支后从主线剥离**、**保存模型/Skill/MCP 配置不自动触发在线热同步（仅标记待同步，由管理员按需手动点击一键部署/同步）**。

## Goals / Non-Goals

**Goals:**
- 将期望配置、实时运行观测、维护操作记录拆分为单写者正交存储，根除行锁竞争与保存配置误伤就绪态。
- 在无 `systemd` 条件下实现远端单守护排他运行、`opencode serve` 子进程崩溃自动拉起、孤儿清理与宿主机重启后的 2 秒级轻量唤醒。
- 用“连接认证首帧 + 心跳增量快照推送 + 纯函数就绪求值”取代后台拉取轮询、多轨修订号和列表页 N+1 `/health` 请求。
- 将维护操作收敛为 5 状态有界模型，消除 `INTERRUPTED` 死锁，修复调度器候选循环队头阻塞。

**Non-Goals:**
- 不使用 `systemd` 或任何系统级 init 服务。
- 保留 `remote_bootstrap.py` 的 Python 检测/源码构建及 `remote_artifacts.py` 的固定版本、来源白名单与摘要校验流程。真实 E2E 发现公开华为 npm 源返回相对 tarball，后者仅补齐同 registry 的路径解析，不添加备用源或绕过校验。
- **不新增保存配置时的自动在线热同步指令（`sync_config`）**：低频的模型、Skill、MCP 变更保存后仅静态展示“待同步”，统一复用幂等快速 `deploy` 应用配置，避免维护两套配置渲染与落盘代码路径。
- 不复制或改动 AgentScope 内部的 Agent/Session/Skill/Knowledge 事实源；Task 与原生 OpenCode Session 状态仍以既有 `RuntimeService` 对账为准，不靠进程退出推断任务终态。

## Decisions

### 1. DSH 处置：归档分支隔离，主线保持纯 OpenCode
- **决策**：先从当前 `v1.4.9` 切出 `archive/dsh-acp` 分支完整保存现有 DSH 实现与测试，随后在 `v1.4.9` 主线移除 16 个 DSH 专属文件及 22 个公共文件中的 `runtime_engine == "dsh"` 分支（含移除 Worker Wheel 中捆绑的 `ops/runtime/dsh/package-lock.json`）。待 OpenCode 重构彻底稳定后，若需演进 DSH 再从稳定基线单开分支。
- **舍弃方案**：在主线保留 22 处 `if runtime_engine == "dsh"` 或立即抽象双引擎插件层。主线生产仅使用 OpenCode，剥离后可净减约 2,200 行分支复杂度。

### 2. 存储事实源分离：`Desired`（环境表） vs `Observed`（实例表）
- **决策**：通过新增 Alembic migration（不修改已发布 `0001_init`）：
  - `t_nexus_opencode_execution_environments` 仅作为期望配置事实源（仅由配置 API 写入），废弃 `worker_channel` 列及远程模式下的 `status_detail["deployed" | "capability" | "equipment"]`；
  - `t_nexus_worker_instances` 新增 `target_digest`（`host:port:username:workspace_root:opencode_port` 摘要，部署签发时写入）、`observed_snapshot`（JSON）与 `observed_at`，仅由 `WorkerGateway` 在 `authenticate` 与 `heartbeat` 处理中按 `WHERE id = :id AND epoch = :epoch` 原子更新。
- **舍弃方案**：继续往 `OpenCodeExecutionEnvironment.status_detail` 合并写入观测数据。单表混写是行锁争用和 `PUT /environment` 抹除部署态的直接原因；放入 `WorkerInstance` 还能直接复用网关现成的 `epoch` 并发栅栏。

### 3. 配置变更生效路径：单一幂等 `deploy` 写盘，不造第二套在线 `sync_config`
- **决策**：保存环境配置（`PUT /environment`）只写数据库。控制面纯函数 `evaluate_worker_readiness()` 实时对比期望值（含从 Skill 目录实时计算的期望内容哈希）与 `WorkerInstance.observed_snapshot`：
  - 仅改名称/描述/标签 → 期望运行规格未变，保持 `READY`；
  - 改模型/Skill/MCP/分支 → 标记 `DRIFTED`（待同步），正在执行的任务不受影响，界面呈现“待同步（请执行一键部署）”；管理员在空闲时点击 `deploy`，因远端 Python/Node/OpenCode 与 Wheel 版本已匹配，幂等快速路径跳过制品重装，仅更新 `opencode.jsonc` 与 `skills/` 并热重载子进程。
- **舍弃方案**：新增 WebSocket `sync_config` 在线写配置指令。模型与 MCP 变更频率低，保留唯一一套 SSH 幂等写盘入口可避免在 `nexus-api` 与 `worker_agent` 两端重复维护配置渲染与回滚逻辑。
- **删除绑定的判定**：复用期望规格现有的单个 `revision` 摘要，部署时写入守护环境，随观测快照上报 `spec_revision`。纯函数同时对比该摘要与实际装备内容，覆盖删除最后一个 Skill/MCP 绑定；不将额外发现的非受管 Skill 误判为配置漂移，也不向环境表补写已部署快照。

### 4. 无 `systemd` 进程模型：`flock` 单例 + `OpenCodeSupervisor` + 快速唤醒
- **决策**：
  - 远端唯一入口为 `/opt/nexus/workers/env-{id}/ctl.sh {start|stop|restart|status}`，部署同时生成聚合脚本 `/opt/nexus/workers/start-all.sh` 供容器启动钩子选用；
  - `nexus-worker-agent` 启动即持有 `worker.lock`（`flock LOCK_EX | LOCK_NB` + `FD_CLOEXEC`）；拉起 `opencode serve` 子进程组时注入 `NEXUS_WORKER_ENV_ID={id}`，启动前仅清扫当前 UID 下匹配该环境变量的孤儿残留进程，废除 `opencode-process.json` 与 `worker-process.json`；
  - `OpenCodeSupervisor` 监控子进程退出与本地 `/global/health`，异常时立即推送含最近 30 行输出尾部的 `last_error` 并按 1s→30s 退避自动拉起；
  - 节点在线时 `restart / update / check` 走网关指令执行；节点因宿主机重启离线但已注册时，`restart` 走轻量 SSH 执行 `ctl.sh start` 秒级唤醒，免重跑全量安装。
- **舍弃方案**：`systemd --user`（目标环境不具备 `systemd`）或继续使用双平级裸进程 + JSON PID 文件（存在 PID 复用误判与子进程崩溃无人拉起问题）。

### 5. 探测 Push 化与 5 状态维护队列
- **决策**：
  - 删除 `health_observer` 后台轮询与离线 SSH 探测；`worker_agent` 每 15s（及状态变化即时）做纯本地探测（不跑 `git ls-remote`），摘要变化或满 60s 时搭车 `heartbeat(kind="report")` 推送；`git ls-remote` 与模型可达性仅在部署、`update` 或手动 `check` 时按需执行；
  - `WorkerMaintenanceOperation` 状态收敛为 `QUEUED | RUNNING | SUCCEEDED | FAILED | CANCELED`，取消或进程重启租约过期直接进入终态并释放 `active_environment_id`，远端并发由 `/opt/nexus/workers/.locks/deploy-env-{id}.lock` 内核 `flock` 兜底；
  - `GET /api/agent-workers` 单次查询直出 `readiness_summary`，前端移除列表页 N+1 `/health` 请求与 `/reconcile` 逻辑；`Dispatcher.claim_once()` 在候选循环内检查就绪态，不可调度节点直接 `continue`。

## Risks / Trade-offs

- **[无 `systemd` 宿主机整机重启后若未挂载 `start-all.sh`，Worker 不会自动自启]** → 网关断连后立即置为 `OFFLINE` 停止派单，不产生脏任务；管理员点击“启动/重启”即可通过轻量 SSH `ctl.sh start` 在 2 秒内唤醒恢复，容器化环境挂载 `/opt/nexus/workers/start-all.sh` 即可全自动恢复。
- **[移除主线 DSH 后，历史测试或配置若残留 `runtime_engine="dsh"` 会报错]** → 在归档 `archive/dsh-acp` 分支后，同步清理前端引擎下拉项与 DSH 测试套件，环境默认固定为 `opencode`。
- **[任务执行中 `opencode serve` 崩溃重启可能打断当前 SSE 流]** → `OpenCodeSupervisor` 仅负责恢复进程可达性，不触碰任务状态；既有 `watch_session` 自动重连并调用 `RuntimeService.refresh()` 以原生会话持久态为准完成恢复对账。

## Migration Plan

1. 从当前 `v1.4.9` HEAD 创建归档分支 `archive/dsh-acp` 保存完整 DSH 代码，随后在主线独立提交剥离 DSH。
2. 新增单个 Alembic revision：为 `t_nexus_worker_instances` 增加 `target_digest`、`observed_snapshot`、`observed_at` 字段，移除 `t_nexus_opencode_execution_environments.worker_channel` 及 `t_nexus_worker_maintenance_operations` 的废弃核对字段。
3. 升级部署后，既有远端 Worker 重新执行一次幂等 `deploy` 即可切换至新单守护布局（自动清理旧裸进程并接管端口）。

## Open Questions

无；DSH 分支剥离方式与配置保存不自动触发热同步均已确认。
