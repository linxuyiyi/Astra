# Nexus v1.4.5 Worker 部署与运维实现报告（2026-10-19）

> **历史快照（final5 时点）**：以下 PASS 仅代表当时源码、当时 QA 环境与当时范围，不是当前源码的无条件结论。当前最终闭环复检见 [NEXUS_V145_WORKER_RECHECK_20261019.md](NEXUS_V145_WORKER_RECHECK_20261019.md)。

> **最终状态：PASS。** 分支 `codex/nexus-v1.4.5-workers`，基线 `ae6bc2e3e`。
>
> 覆盖 aarch64 / x86_64 真实部署、双 Worker、维护幂等、Git、装备、健康、占用保护与 ARM 故障矩阵。MySQL 未跑，模型只验证 `/models` 认证与可见性，未调用真实推理；HTTP Git 使用自有私有 fixture。详见“边界与未覆盖”。

## 1. 最终结论

| 项目 | 结果 |
| --- | --- |
| 最终 Linux 回归 | **460 passed / 0 failed / 1 skipped**；唯一 skip 为 MySQL-only 方言用例 |
| Redis / ruff | Redis DB14 真实 `PING` 与两个原跳过用例通过；ruff All checks passed |
| aarch64 full deploy | PASS；14 步全通过，6 项装备 verified |
| x86_64 full deploy + 双 Worker | PASS；Worker A/B 均在线，OpenCode 正常，6 项装备 verified |
| 同配置幂等 | PASS；ARM 与 x86 均未轮换 token、未改 unit/config/worktree，PID 不变 |
| dirty / 错误分支 / fast-forward | PASS；dirty 与错误分支拒绝并保留现场，正确分支 `merge --ff-only` |
| disable / restore | PASS；disable 不停止 OpenCode/Worker、不释放任务；restore 基于真实健康 |
| WAITING / 原生 busy | PASS；WAITING 不释放，busy 时 deploy/update/restart 全部拒绝 |
| HTTP Git + PAT | PASS；正确/错误 PAT、clone/fetch/pull/push、askpass 与日志脱敏均验证 |
| x86 装备故障 | PASS；MCP 断连、Skill 缺失、模型 401 三项均被识别并恢复 |
| ARM fault3 / fault4 / 离线 SSH | PASS；停机 restart、控制器崩溃恢复、离线 SSH 停机 restart 全部通过 |
| Web | type-check、build、15/15 tests 通过；中文阶段标签补齐；1280x720 真实页面 |
| OpenSpec | strict 通过；change 未 archive；历史 `openspec/specs/` 未修改 |

## 2. 最终 QA 环境

| 环境 | 事实 |
| --- | --- |
| ARM / aarch64 Lima | VM `nexus-v145-qa`，SSH `127.0.0.1:56060`；最终 Worker A `AVAILABLE / schedulable=true`，OpenCode PID 40203、Worker PID 40204，6 项装备 verified；QA 预览 `http://127.0.0.1:61275` |
| x86_64 CentOS 7 | Worker A/B 均在 `root@172.17.13.60:22`；OpenCode 端口 44145/44146，均“连接在线 / OpenCode 正常”；6 项装备 verified；用户环境预览 `http://127.0.0.1:61875` |
| 原始主 checkout | `/Users/niuniu/程序/Astra` 保持 `c046abeda`，18 个未跟踪条目未改动 |

## 3. 原因与实现改动

1. **维护操作不再依赖 HTTP 生命周期**：部署/重启/检查改为 `worker_maintenance_operations` + events 持久化，FastAPI lifespan executor 领取、续租、fencing；HTTP 只提交与订阅，断线不取消，刷新可恢复。
2. **状态分离**：维护状态、连接状态、运行时健康、`schedulable` 分开计算；维护失败不直接改写旧健康，只有真实探测失败/未知才进入 `UNAVAILABLE/UNKNOWN`。
3. **统一保护入口**：环境写入、Worker 通道、Task 启动、调度器共用维护锁；危险步骤前检查 canonical 占用、WAITING、Session 与原生 idle，并在原生检查后做 fenced 复检。
4. **部署与 Git 幂等**：已有 Worker env/token/unit 内容一致时复用；`update` 显式 `fetch` + `merge --ff-only`，dirty、错误分支、领先/分叉保留现场；Git token 按环境装配 askpass，不进入 argv/unit/env/日志。
5. **停机就绪等待与实际一致**：在线 restart 由 Worker 本地命令完成并返回真实健康；离线 SSH restart 在 `systemctl restart` 后增加有界真实 `/global/health`（2xx JSON `healthy=true`）等待，再等待既有 Worker online，之后才允许操作成功，不提前 `SUCCEEDED`。
6. **装备与命名收敛**：模型/MCP/Skill/Git/版本按期望/实际/差异展示，仅必要时更新；Worker 外部路径统一 `NEXUS_*` / `nexus-*`，旧 Worker `ASTRA_*` 被拒绝。

## 4. 验证结果

### 4.1 回归、Redis、ruff

- 最终 final5：**460 passed, 0 failed, 1 skipped, 4 warnings**，耗时 99.13s。
- 唯一 skip：`test_sweep_statements_execute_on_mysql`（无 `ASTRA_TEST_MYSQL_URL`，只编译 MySQL 方言）。
- Redis：harness 自有 DB14，`PING` 成功；两个此前因无 Redis 跳过的用例真实运行；未触碰业务 DB1。
- ruff：`All checks passed!`；宿主与客户机源码漂移 0。

### 4.2 两架构部署与幂等

- aarch64：14 个部署步骤全 passed，6 项装备 verified；同配置第二次 deploy `SUCCEEDED`，`changed` 全 false，OpenCode/Worker PID `22283/28171` 不变，token/config/unit/worktree 哈希一致。
- x86_64：Worker A `file://` 仓库，Worker B HTTP Git fixture + PAT；双 Worker 最终均 `AVAILABLE / schedulable=true`，6 项装备 verified；重复 deploy 后 PID、unit/config/worktree、token 哈希不变。

### 4.3 Git 与 Update

- HTTP Git fixture：4 个提交，head `f1dd3ae6…`，`receivepack=true`；正确 PAT `ls-remote` rc=0，错误 PAT rc=128；clone/fetch/pull/push 通道验证；日志无 PAT、无 Authorization。
- x86 Worker B：`with_askpass_rc=0`、`without_askpass_rc=128`，token 文件哈希与 PAT 一致，最终 Git verified。
- dirty update 进入 `INTERRUPTED` 并保留 sentinel、HEAD/tree、PID、配置与 token；只读 reconcile 后释放。错误分支 update/deploy 均拒绝。正确更新通过 `merge --ff-only` 快进，reflog 有 `Fast-forward`。

### 4.4 占用、disable/restore

- ARM fault1：WAITING Task/Session 在 Worker 重启后不释放，Worker PID 30812→31208、OpenCode PID 22283 不变。
- ARM fault2：原生 Session busy 时 deploy/update/restart 全部失败，PID、unit、工作树、token、配置哈希不变。
- x86：占用时 deploy/update/restart 返回 409；disable 仍成功且保留 WAITING 任务与进程；restore 后恢复 `schedulable=true`。

### 4.5 装备故障（x86）

- MCP 断连：`/mcp` 返回 failed，健康 MCP `verified=false`，diff“MCP 未连接”；恢复后重新连接。
- Skill 缺失：Skill 不在 `/skill` 列表，健康 `verified=false`，diff“Skill 未发现”；恢复后重新发现。
- 模型 401：`/models` 返回 401，健康模型 `verified=false`、error“模型网关认证失败”；恢复后 200 且模型可见。
- 恢复后 env1/env2 均 `AVAILABLE / schedulable=true`，6 项装备 verified；26 份装备报告无原始 secret 命中。

### 4.6 ARM 故障矩阵

- fault3 retest：停机后正式 restart `SUCCEEDED`；SIGKILL 后 systemd 恢复；持久停止可观测；最终 Worker online、6 项 verified、token 不变。
- fault4 controller crash：部署 RUNNING 时按身份 kill API；操作 `INTERRUPTED` 并保留锁；SSE 从 cursor 续传无重复；reconcile 确认无晚到危险命令；retry `SUCCEEDED`，最终 Worker healthy。
- 离线 SSH 停机：精确 stop Worker + OpenCode，确认 offline/stopped 后正式 restart；`systemctl` 后真实 `/global/health` 就绪等待 + Worker online；操作 **4.07s SUCCEEDED，无需 reconcile**，6 项 verified、可调度、token 不变，无手动 start。
- 最终 fault matrix：`status=PASS`，deploy 幂等、fault3、fault4、offline SSH 全部 PASS。

### 4.7 Web

- 最终中文阶段标签补齐后：`npm test` 15/15、`npm run type-check`、`npm run build`（4019 modules）全部通过。
- 1280x720 真实页面：Worker A 详情显示“连接 在线 / OpenCode 正常 / 维护 成功 / 可调度”；双 Worker 列表显示两台均在线、OpenCode 正常；服务器 `root@172.17.13.60:22`，工作目录 `/opt/nexus-v145-qa-20261019/workers/*`。

## 5. R01-R10 Scenario 映射

| 编号 | Scenario | 实现 | 验证 | 状态 |
| --- | --- | --- | --- | --- |
| R01 维护持久化 | 浏览器断开继续；刷新恢复；取消显式 | operation/event 表、executor、SSE、cancel | final5 断线/续传/取消测试；fault4 SSE 续传无重复 | **PASS** |
| R02 状态分离 | 维护失败旧环境仍健康；远端未知；连接与运行时分开 | `maintenance.py`、`worker_health.py`、health API | fault2 维护失败旧环境可用；fault3/offline 连接在线但运行时不可用；INTERRUPTED/UNKNOWN 保守拒绝调度 | **PASS** |
| R03 任务保护 | 执行/WAITING 拒绝；原生 busy 拒绝；check 只读；维护与启动并发 | 提交与危险步骤 canonical 占用；原生 idle + fenced 复检 | ARM fault1/fault2；x86 fault E 409；final5 并发与只读测试 | **PASS** |
| R04 重试与恢复 | 阶段幂等；危险前/中取消；中断后识别已完成；重试不破坏旧环境；失败只撤销新 Key | lease/fencing、reconcile、幂等安装、Key 清理 | ARM/x86 幂等；fault4 崩溃→INTERRUPTED→reconcile→retry 成功；offline 无需 reconcile | **PASS** |
| R05 仓库管理 | 首次 clone；已有仓库核对；update 显式 fetch/ff；dirty/分支保护；记录 commit | `remote_install.sh` repository stage；inspection | x86 clone、dirty、wrong branch、fast-forward；HTTP Git/PAT；快照记录 commit | **PASS** |
| R06 装备一致性 | 配置/部署/验证/异常；模型/MCP/Skill/Git/版本差异；仅必要时更新 | `worker_health.py`、inspection、配置合并 | 两架构 6 项 verified；同配置 changed 全 false；MCP/Skill/model 三项故障识别与恢复 | **PASS** |
| R07 真实健康 | Gateway 在线；OpenCode API；模型认证；MCP；Skill | freshness/epoch + 真实只读探测 | fault3/4/offline；x86 装备故障；模型仅 `/models` 认证/可见性 | **PASS** |
| R08 生命周期 | 在线重启服务已停止；离线 SSH 停机；deploy/check/update/disable/restore；在线 Worker、离线 SSH；审计 | 统一维护动作、在线优先/离线 SSH、就绪等待 | fault3/fault4/offline；disable/restore；deploy/update/check 真实通过 | **PASS** |
| R09 界面 | 环境/仓库/装备/连接/健康/任务/最近维护；别名 | Agent Worker 列表/详情、维护面板、中文阶段 | 15/15 tests、type-check/build；1280x720 详情与双 Worker 列表 | **PASS** |
| R10 命名 | 对外服务/日志/模板/文档统一 Nexus；内部键同版本替换；不做兼容层 | Worker env/provider/unit/配置/文档；旧 Worker `ASTRA_*` 拒绝 | 外部路径无 `astra`；`NEXUS_*`/`nexus-*` 收敛；`.astra-tasks` 为内部工作区目录，不是外部别名；final5 回归 | **PASS** |

## 6. 边界与未覆盖

- **MySQL**：未执行真实 MySQL 维护回归；唯一 skip 为 MySQL-only 方言用例。
- **模型**：只验证 `/models` 认证与模型可见性，未调用真实 LLM 推理。
- **HTTP Git**：使用自有私有 fixture，验证认证、clone/fetch/pull/push 与日志脱敏；不代表第三方托管或用户生产凭证。
- **安装失败**：Node/npm/pip 安装失败由 Linux 安装脚本 fixture 验证，未通过真实断网破坏全局包。
- **范围**：未宣称生产全产品验收；本报告覆盖 Worker 部署、维护、Git、装备、健康、UI 与故障矩阵。

## 7. 证据

目录：`docs/reports/evidence/nexus-v145-worker-maintenance/`，架构核对与历史替代见 `EVIDENCE_INDEX.md`。

- `arm-acceptance-final5-460-20261019T054735Z.json`、`arm-acceptance-final5-pytest-460-20261019T054735Z.log`、`arm-acceptance-final5-ruff-20261019T054735Z.log`、`arm-acceptance-final5-prepare-20261019T054735Z.log`、`arm-acceptance-final5-redis-preflight-20261019T054735Z.log`：final5 回归、Redis、ruff、快照。
- `final-deploy-aarch64-idempotent-20261019T052402Z.json`、`arm-worker-a-final-idempotency-20261019T052328Z.json`：ARM 部署与幂等。
- `arm-fault1-occupancy-20261019T052552Z.json`、`arm-fault2-native-busy-20261019T052747Z.json`：占用与原生 busy。
- `arm-fault3-retest-20261019T054219Z.json`、`arm-fault4-controller-crash-20261019T054402Z.json`、`arm-offline-ssh-recovery-final-20261019T054840Z.json`、`arm-offline-readiness-117-20261019T054807Z.log`、`arm-fault-matrix-final-20261019T054952Z.json`：ARM fault3/fault4/离线 SSH 与最终矩阵。
- `x86-fault-matrix-batch1-20261019T053515Z.json`：x86 双 Worker、幂等、Git、disable/restore、占用。
- `x86-equipment-faults-final-20261019T053946Z.json`、`x86-equipment-faults-final-health-20261019T053946Z.json`、`x86-equipment-faults-secret-scan-20261019T053946Z.json`：MCP 断连/Skill 缺失/模型 401 与恢复。
- `x86-git-http-fixture-20261019T053515Z.json`、`x86-secret-scan-20261019T053515Z.json`：HTTP Git fixture 与脱敏扫描。
- `ui-final-validation-20261019.json`、`ui-final-validation-20261019.log`：最终 UI 15/15、type-check、build。
- `x86-ui-worker-a-final-1280x720.png`、`x86-ui-worker-a-overview-final-1280x720.png`、`x86-ui-workers-list-final-1280x720.png`：1280x720 真实页面与双 Worker 列表。

## 8. OpenSpec 与任务

- change：`openspec/changes/nexus-v145-worker-maintenance/`，未 archive；`openspec validate --strict` 通过。
- `tasks.md` 8.1-8.6 均按最终事实勾选；在线/离线停机与就绪等待契约已同步到 design/tasks。
- `openspec/specs/` 历史契约未修改。
