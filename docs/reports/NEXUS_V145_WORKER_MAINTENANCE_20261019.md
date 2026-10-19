# Nexus v1.4.5 Worker 部署与运维实现报告（2026-10-19）

> **状态：实现已完成，真实 Linux 验收进行中，不能发布全量通过结论。**
>
> 分支：`codex/nexus-v1.4.5-workers`　基线：`ae6bc2e3e`
>
> 本报告记录截至 2026-10-19 的实现里程碑、已验证证据和未完成项。本轮提交与推送只代表实现进入可联调阶段，不代表真实 Linux 全量验收通过，也不代表可以合入 `main`、创建 PR 或打 release tag。

## 1. 结论摘要

| 项目 | 状态 |
| --- | --- |
| v1.4.5 Worker 维护核心实现 | 已完成 |
| aarch64 Ubuntu 虚拟机 Linux 回归 | 415 passed / 0 failed / 3 skipped（MySQL 1、Redis 2） |
| 真实浏览器问题修复后的后端定向回归 | 73 passed（维护 + remote + phase4） |
| Web type-check / build / 单测 | 通过；15 个测试通过 |
| ruff / OpenSpec strict / SQLite 升降级 | 通过 |
| 首次真实 Linux deploy | **未成功**：harness 漏 `/v1`，模型检查 404 后操作 `INTERRUPTED`，Worker 未 online |
| x86_64 CentOS 7 真实执行机 | 已完成只读盘点，独立 runtime 准备中，未执行 full deploy |
| arm 真实 full deploy、故障矩阵、私有 Git/PAT/push、MySQL | 未完成 |

## 2. 问题原因

1. **维护操作与 HTTP 生命周期耦合（W01）**：旧部署/重启由 `StreamingResponse` 内创建的 asyncio task 执行，浏览器断开时会被取消；操作状态复用环境 `status_detail.operation` JSON 作为锁与状态，刷新后无法可靠恢复。
2. **维护失败污染运行状态（W02）**：旧 `_finish` 将维护失败统一写为 `UNAVAILABLE`，把“维护失败”“连接状态”“运行时健康”“可调度性”混成一套状态，可能错误阻止调度或掩盖旧环境仍健康的事实。
3. **共享保护入口不统一（W03/W05）**：环境保存/检测/删除、Worker 通道切换、Task 启动与调度器分别判断维护状态，存在并发窗口；危险步骤前缺少统一的 canonical 占用与原生 Session 复检。
4. **远端安装非幂等（W03/W04）**：重复部署可能重复轮换 Worker token；已有仓库只核对 origin/分支，不显式 fetch；失败时无法清晰区分“已写入配置”与“仅生成未使用凭据”。
5. **旧旁路与命名残留（W07/W08）**：旧 `/api/employees/{id}/environment/{deploy|restart}`、`/environment/test` 有 UI 消费者；Worker 环境变量仍混用 `ASTRA_*` 与 `NEXUS_*`。
6. **首次真实 Linux 部署失败原因**：QA harness 的模型中转健康检查地址漏写 `/v1`，真实安装流程在“模型中转连接”阶段收到 HTTP 404；服务端未把 404 误判为成功，操作进入 `INTERRUPTED` 并保留待核对记录，Worker 保持离线、`schedulable=false`。这是 harness 配置问题，不是安装脚本成功的证据，也不能据此宣称真实部署通过。

## 3. 实现内容

### 3.1 维护操作持久化与执行器

- 新增 `worker_maintenance_operations`、`worker_maintenance_events` 模型及 Alembic `0048_worker_maintenance.py`，包含状态、阶段、attempt、租约、cancel、danger、reconcile、前后快照与错误字段。
- 新增 `app/runtime/maintenance.py`：提交、历史、详情、事件分页/SSE、取消、重试、`reconcile`；短事务原子 claim、租约续租、attempt/owner fencing；进程退出与租约过期将自有未完成操作持久化为 `INTERRUPTED`，不自动释放维护锁。
- FastAPI lifespan 内运行数据库驱动 executor；HTTP 只提交与订阅，客户端断线不取消执行。
- 维护动作覆盖 `check / deploy / update / restart / disable / restore / reconcile`；`disable` 只暂停新调度，不停止 OpenCode、不取消现有 Task；`restore` 基于真实健康探测。
- 重试与 `reconcile` 先做只读核对，避免盲目轮换 token、重复 enroll 或重复重启。

### 3.2 共享保护与状态分离

- 环境保存/检测/删除、Worker 通道、Task 启动与调度器统一读取维护操作表这一单一事实源。
- 危险步骤前检查 canonical 任务占用、`WAITING` 任务、未结束 Session 与原生 OpenCode Session；写配置/换装备/重启前再次检查，原生检查后做数据库 fenced 复检。
- 维护失败不再无条件写 `UNAVAILABLE`；维护状态、连接状态、运行时健康、`schedulable` 分开返回。真实探测失败或结果未知时保守禁止调度。
- 新增 `app/runtime/worker_health.py`、`app/worker_agent/inspection.py`、`app/runtime/redaction.py`：按期望/实际/差异计算 OpenCode、模型认证、MCP、Skill、Git 与版本装备事实，并对事件与 API 输出脱敏。

### 3.3 远端安装与幂等

- 重构 `remote_install.sh` 与 `remote_provisioning.py`：按环境隔离 Node/npm、OpenCode、Worker Agent venv、配置与 systemd 单元，不再共享旧 `/opt` 路径。
- 重复部署识别现有有效 Worker env/token 与 unit，避免无谓轮换；失败只撤销未写入有效配置的新 Key。
- 已有仓库在 `deploy` 中只核对，不隐式 fetch；`update` 显式 `fetch` + `merge --ff-only`，工作区脏、分支不一致或分叉时停止并保留现场。
- 私有 Git token 通过临时 SFTP 文件传入远端，不写入 operation payload、日志或 argv；配置写入采用临时文件与备份，失败保留现场。
- Worker 环境变量、provider、systemd 与文档统一使用 `NEXUS_*` / `nexus-*` 命名。

### 3.4 Web 与 API

- 新增 `/api/agent-workers/{environment_id}/maintenance`、`/api/maintenance/{id}`、events/page/SSE、cancel/retry/reconcile 与 `/health`。
- 删除旧 `/api/employees/{id}/environment/{deploy|restart}` NDJSON 与 `/environment/test` 旁路；保留 `/api/workers` 只读别名与前端 `/workers` 重定向。
- Web 新增维护 API、`useWorkerMaintenance`、`WorkerMaintenancePanel`、装备/仓库呈现与动作策略；详情页展示连接、运行时健康、装备差异、当前任务与维护记录；列表页统一 Agent Worker 术语。
- 初装离线、健康未知、装备待部署不再误禁用 deploy/update/restart；只有真实占用阻止危险操作；`INTERRUPTED` 保留 `check/restore`。

### 3.5 文档、运维与 OpenSpec

- 新增/更新 `docs/deploy/WORKER_MAINTENANCE.md`、`docs/design/nexus-v145-worker-deployment-maintenance.md`、`docs/deploy/REMOTE_EMPLOYEES.md`、`ops/deploy/DEPLOYMENT_GUIDE.md`、`ops/env/.env.example`。
- OpenSpec change `nexus-v145-worker-maintenance` 已建立 proposal/design/spec/tasks；本轮不 archive。

## 4. 验证结果

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| aarch64 Ubuntu 虚拟机 Linux 回归（17 个测试文件） | **415 passed, 0 failed, 3 skipped** | `docs/reports/evidence/nexus-v145-worker-maintenance/acceptance-final-latest.json`、`acceptance-final-pytest-20261019T042337Z-final.log` |
| 跳过项 | MySQL-only 1 项；Redis-only 2 项 | 同上 |
| ruff（`--no-cache app tests`） | All checks passed | `acceptance-final-ruff-20261019T042337Z-final.log` |
| 依赖准备/快照 | pip/import 正常；270 文件；运行后 drift=0 | `acceptance-final-prepare-20261019T042337Z-final.log`、`acceptance-final-latest.json` |
| 浏览器问题修复后的后端定向回归 | **73 passed in 5.28s**（维护 + remote + phase4） | `backend-fix-20261019T043332Z-stage-version.log` |
| remote + phase4 子组 | 28 passed | `backend-fix-20261019T042326Z-remote-phase4-full.log` |
| Web type-check / build / 单测 | 通过；15/15 tests | `ui-browser-typecheck-build-tests-20261019.jsonl` |
| stage 额外映射 type-check | 通过 | 同上 |
| OpenSpec strict | 通过 | 本轮变更文件；未修改 `openspec/specs/` 历史契约 |
| SQLite migration | `upgrade head` / `downgrade` / 再 `upgrade head` 通过 | `0048_worker_maintenance.py` |
| 首次真实 Linux deploy | **INTERRUPTED（失败待核对）**：harness 漏 `/v1` 导致模型检查 404 | `first-linux-deploy-interrupted-20261019.json`、`first-linux-deploy-qa-detail-20261019.json` |
| x86_64 CentOS 7 盘点 | 只读连接成功；未部署 | `target-x86_64-inventory-20261019.json`、`target-x86_64-runtime-discovery-20261019.json` |

补充说明：

- 首次真实 Linux deploy 的 `qa` 结果为 `failed`；维护操作终态 `INTERRUPTED`，健康接口返回 Worker `offline`、`runtime_health.status=UNKNOWN`、`schedulable=false`，并列出 `maintenance_active`、`worker_offline`、`runtime_unknown` 等阻塞项。该记录已保留，等待核对/恢复，不能称为部署成功。
- x86_64 执行机 `47.77.219.113` 为 CentOS 7 / kernel 3.10 / glibc 2.17 / systemd 219，仅有 Python 2.7.5，未发现 `python3`；独立 runtime 仍在准备。本报告与证据不包含 SSH 密码、root secret 或 `/tmp/private` 原始私密文件。

## 5. R01-R10 映射

| 编号 | 状态 | 实现与验证 | 未完成/说明 |
| --- | --- | --- | --- |
| R01 维护任务持久化 | 已实现 | operation/event 表、executor、SSE 续传、显式取消；单测与 415 回归覆盖断线不取消、刷新恢复 | 真实浏览器断网场景仍需最终复验 |
| R02 状态分离 | 已实现 | 维护/连接/运行时健康/`schedulable` 分开返回；维护失败不直接覆盖旧健康 | 真实故障注入未完成 |
| R03 任务保护 | 已实现 | 提交与危险步骤前 canonical 占用、`WAITING`、Session、原生 idle 检查；fenced 复检 | 真实并发与原生 busy 场景仍需 Linux 验收 |
| R04 部署可重试与恢复 | 部分完成 | 幂等安装、token 复用、失败只清理新 Key、`reconcile` 只读核对 | 真实安装失败/进程崩溃/网络中断矩阵未完成 |
| R05 工程仓库完整管理 | 部分完成 | `deploy` 只核对；`update` 显式 fetch + ff-only；脏工作区/分叉保留现场；记录 commit | 私有 Git/PAT/push 未验证；OpenSpec 8.2 未勾选 |
| R06 装备一致性 | 部分完成 | 期望/实际/差异、装备状态与版本差异展示；单测覆盖 | 真实 MCP/Skill/模型全矩阵未完成；8.3 未勾选 |
| R07 真实健康检查 | 部分完成 | OpenCode、模型认证、MCP、Skill、Git 真实只读探测；freshness/epoch 投影 | 真实 Linux 全链路探测矩阵未完成；8.4 未勾选 |
| R08 Worker 生命周期操作 | 已实现动作 | `check/deploy/update/restart/disable/restore/reconcile`，在线优先 Worker 通道、离线 SSH，统一审计 | 真实 Linux 重启/停用/恢复未验收 |
| R09 界面与术语归一 | 已完成 | Agent Worker 列表/详情/维护面板；旧 `testEnvironment` 删除；`/workers` 重定向保留 | 浏览器最终视觉复验未完整重跑 |
| R10 Nexus 命名清理 | 部分完成 | Worker env、provider、systemd、文档改为 `NEXUS_*` / `nexus-*` | 对外全量 `ASTRA_*`/`astra-*` 清理未完成；8.5 未勾选 |

## 6. 剩余风险与阻塞

1. **真实 Linux 首次部署未成功**：harness 漏 `/v1` 导致模型检查 404，操作 `INTERRUPTED`，Worker 未 online；必须先用 `reconcile` 只读核对远端是否存在晚到安装/重启，再决定恢复或重试。
2. **x86_64 CentOS 7 兼容性**：glibc 2.17、systemd 219、仅 Python 2.7、无 `python3`；独立 runtime 准备中，尚未执行 full deploy。
3. **aarch64 仅完成回归测试**：415 passed 是测试与快照证据，不等同于 arm 真实 full deploy；arm full deploy 仍未完成。
4. **故障矩阵未完成**：断网重连、OpenCode 崩溃、Node/npm/pip 安装失败、权限与密钥检查、多 Worker 同机等场景仍需真实 Linux 验收。
5. **私有 Git/PAT/push 与 MySQL 未完成**：当前 Git 证据来自 VM 本地 `file://` bare 仓库；MySQL 仅做了 MySQL-only 跳过项标记，未执行真实 MySQL 维护回归。
6. **浏览器最终复验不完整**：阶段/目标版本问题已修复并由 type-check/build/策略单测覆盖，但修复后未完整重新点击全部浏览器流程。
7. **发布边界**：不合 `main`、不创建 PR、不打 release tag；OpenSpec change 不 archive，`tasks.md` 8.2-8.6 仍未勾选。

## 7. 证据文件

目录：`docs/reports/evidence/nexus-v145-worker-maintenance/`

- `acceptance-final-latest.json`：aarch64 Linux 回归汇总（415/0/3，drift=0）。
- `acceptance-final-pytest-20261019T042337Z-final.log`：完整 pytest 列表与结果。
- `acceptance-final-ruff-20261019T042337Z-final.log`：ruff 通过。
- `acceptance-final-prepare-20261019T042337Z-final.log`：依赖准备与快照信息。
- `backend-fix-20261019T043332Z-stage-version.log`：维护 + remote + phase4 73 passed。
- `backend-fix-20261019T042326Z-remote-phase4-full.log`：remote + phase4 28 passed。
- `first-linux-deploy-interrupted-20261019.json`：首次真实 Linux deploy 的 `failed` 汇总。
- `first-linux-deploy-qa-detail-20261019.json`：`INTERRUPTED`、404、Worker offline、阻塞项与操作事件明细。
- `target-x86_64-inventory-20261019.json`：真实执行机只读盘点。
- `target-x86_64-runtime-discovery-20261019.json`：glibc/Python/systemd/工具与端口发现。
- `ui-browser-typecheck-build-tests-20261019.jsonl`：Web type-check/build/15 tests 与 stage 映射验证记录。

以上证据均为脱敏后的测试/盘点输出，不包含 SSH 密码、root secret、token 值或 `/tmp/private` 原始私密文件。

## 8. OpenSpec 与提交状态

- change：`openspec/changes/nexus-v145-worker-maintenance/`，未 archive。
- `tasks.md` 7.5 随本提交完成；8.2-8.6 保持未勾选，其中 **8.6 真实 Linux Worker 验收**仍是后续工作。
- 本提交为实现里程碑 Conventional Commit，推送 `codex/nexus-v1.4.5-workers`；不 force、不合 `main`、不创建 PR、不打 release tag。
