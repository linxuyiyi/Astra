# Tasks

> 本 `tasks.md` 是 v1.4.5 R01-R10 的唯一实施清单。后端维护核心、命名/别名、旧旁路清理、UI 与真实 Linux 验收均已完成；8.1-8.6 按最终回归与故障矩阵证据勾选。

## 1. 契约与 OpenSpec

- [x] 1.1 完成 `nexus-v145-worker-maintenance` 的 proposal/design/spec/tasks，明确操作状态机、租约、API JSON、状态分离与旧接口过渡。验证：`openspec validate --strict` 通过，且 `openspec/specs/` 历史契约未修改。
- [x] 1.2 将用户定稿复制到 `docs/design/nexus-v145-worker-deployment-maintenance.md` 便于追踪。验证：内容与定稿一致，不另写计划。

## 2. 持久化与迁移

- [x] 2.1 新增 `WorkerMaintenanceOperation`、`WorkerMaintenanceEvent` 模型，覆盖 state/stage/attempt/lease/cancel/danger/快照/错误字段。验证：模型可建表，唯一活动环境约束生效。
- [x] 2.2 新增 Alembic migration，创建两表、索引与唯一活动环境约束。验证：新 SQLite 库 `upgrade head` 成功，`downgrade` 可回退。

## 3. 维护服务与执行器

- [x] 3.1 实现提交、查询、历史、事件追加与脱敏；提交在短事务内锁环境、检查 canonical 占用并插入活动操作。验证：并发提交只有一个成功。
- [x] 3.2 实现 claim、续租、fencing、finish、过期恢复与 `INTERRUPTED` 持久化；多进程不抢占有效租约。验证：陈旧 owner/attempt 写入被拒绝。
- [x] 3.3 在 FastAPI lifespan 增加数据库驱动 executor 与优雅退出中断；不进入 Celery 队列。验证：断线后继续执行，进程退出后操作可恢复。

## 4. 共享保护与状态分离

- [x] 4.1 把环境保存/检测/删除、Worker 通道、Task 启动与调度器的维护锁统一改为操作表判定。验证：活动维护期间这些入口返回冲突，结束后恢复。
- [x] 4.2 危险步骤前做 canonical 占用与原生 idle 检查，在线 restart 补齐原生 idle；native 检查后做数据库 fenced 复检。验证：WAITING/busy/并发启动被拒绝。
- [x] 4.3 维护失败不无条件置 `UNAVAILABLE`；连接、运行时健康、维护状态与 `schedulable` 分开返回。验证：失败但旧环境健康时仍可调度，未知时禁止调度。

## 5. 远程执行复用与幂等

- [x] 5.1 复用 `RemoteProvisioner` 的 SSH/安装脚本/Worker 命令，改由持久化操作驱动并持久化步骤事件。验证：部署/重启/检查走同一 executor。
- [x] 5.2 `_install_units` 识别现有有效 Worker env/token 与 unit，避免重复 enroll 轮换；重试复用远端有效 Key 与配置。验证：重复维护不轮换 token。
- [x] 5.3 失败只撤销未写入配置的新 Key，已写入配置的 Key 保持；不承诺自动回滚 Git/DB/凭据。验证：失败清理不破坏当前配置。

- [x] 5.4 每环境 Git token/askpass 安全装配；OpenCode unit 与 Worker env 共用 `safe_git_environment` 的五项变量，在线与离线探测均可认证；token 不进入 argv/日志/环境，凭据内容变化不重启。
- [x] 5.5 Worker 真实只读 `git ls-remote --exit-code` 认证核验，返回 `authentication_verified`；controller 在线/离线消费同一字段，失败阻调度且不宣称 push 权限。

- [x] 5.6 最近维护/history 按最近 attempt 的 `updated_at` 排序，分页顺序一致；dirty Git 描述性 diff 不再阻断已核验装备的调度，真实装备差异仍阻断。

- [x] 5.7 通道切换底层幂等：同配置已生效不写不重启，`verified` 作为实际地址核验唯一事实；busy/unknown/失效 gate 仍阻断。

- [x] 5.8 在线 restart 的原生 idle 判断收敛到 Worker 本地命令；明确停止可启动，busy/unknown/malformed 本地拒绝，控制面占用/租约/gate 保持。
- [x] 5.9 在线/离线 restart 后等待真实就绪：离线在 `systemctl restart` 后有界轮询 `/global/health`（2xx JSON `healthy=true`）再等 Worker online，在线以 Worker 命令结果与健康为准；超时/401/错误 JSON 失败，不提前 `SUCCEEDED`。验证：Linux 定向 117 passed；offline SSH final 4.07s SUCCEEDED 无需 reconcile；final5 460 passed。

- [x] 5.10 Worker env、systemd unit 与 wheel 统一走同目录临时文件、SFTP 回读校验、原子 rename；env/unit 保留 `.nexus-backup`，写入/校验/rename 失败保留旧目标，同内容不写不重启。验证：原子写入故障测试、真实 SSH/SFTP 临时目录验收与 Linux 回归通过；旧备份权限 600，目标严格读取失败不覆盖。

## 6. API 与旧接口

- [x] 6.1 新增环境维度维护提交/历史、操作详情、events SSE（`Last-Event-ID`/`after_id`）、cancel、retry、health。验证：SSE 续传不重复，取消/重试语义正确。
- [x] 6.2 删除无消费者的旧 `/api/employees/{id}/environment/{deploy|restart}` NDJSON handler；UI 删除最后 `environment/test` 调用后同步删除该路由与 schema，不保留 HTTP 等待 check 旁路。验证：rg 无活跃消费者。
- [x] 6.3 保留 `/api/workers` 只读别名与前端 `/workers` 重定向；新接口使用 environment_id 并返回 employee_id 以区分。验证：现有消费者测试通过。

## 7. 测试与验证

- [x] 7.1 targeted pytest 覆盖：HTTP 断线不取消、SSE 续传、lease/fenced stale writer、两并发维护、Task-start 竞争、WAITING/native busy、取消危险半完成、失败但旧环境健康。验证：新增测试通过。
- [x] 7.2 运行受影响回归（remote provisioning、channel switch、worker occupancy、lifecycle/dispatcher）与 `ruff check` 变更范围。验证：命令输出留档；macOS 上 installer/O_PATH 用例按 Linux 回归处理。
- [x] 7.3 新 SQLite 库执行 Alembic `upgrade head` 并验证 schema。验证：迁移成功。
- [x] 7.4 更新既有 `tests/test_remote_provisioning.py` 等对 JSON operation 的过时断言，避免双套状态兼容。验证：无 `status_detail.operation` 长期状态依赖。
- [x] 7.5 检查 `git diff` 无无关修改、调试代码和旧 JSON/表双套状态，提交 Conventional Commit。验证：diff 仅覆盖本 change。

## 8. 同一 v1.4.5 后续阶段（本阶段不实做，不是范围外）

- [x] 8.1 Web：Agent Worker 列表/详情、维护进度离开页面可恢复、健康四态与阻塞项展示；旧 EmployeeConfig 已迁移到新维护 API 并删除最后 `testEnvironment` 调用。验证：UI type-check/build 由 UI 阶段执行，后端 rg 无旧消费者。
- [x] 8.2 Git 完整管理：`update` 显式语义、fetch/pull、工作区变更保护、commit 记录与人工冲突提示。验证：x86 dirty/wrong branch 拒绝并保留现场、fixture fast-forward 成功，HTTP Git/PAT 正确/错误认证、clone/fetch/pull/push 通道核验；final5 460 passed。
- [x] 8.3 装备一致性：Skill/MCP/模型/版本期望与实际差异、仅必要时更新。验证：aarch64 与 x86 双 Worker 的 6 项装备均 configured/deployed/verified，同配置 deploy `changed` 全 false 且 PID/token/config/unit/worktree 不变；x86 MCP 断连/Skill 缺失/模型 401 三项故障均被识别并恢复；final5 460 passed。
- [x] 8.4 Runtime 详细健康核验：OpenCode API、模型网关认证、MCP 连接、Skill 实际发现的真实探测矩阵。验证：x86 MCP 断连/Skill 缺失/模型 401 三项与实际恢复 PASS；ARM 停机/崩溃恢复、controller crash 与离线 SSH restart PASS；final5 460 passed。
- [x] 8.5 对外命名清理：`ASTRA_*`/`astra-gateway`/`astra-backup` 与协议/配置/测试同步替换。验证：Worker 外部路径（`remote_install.sh`、`remote_provisioning.py`、`channel_switch.py`、`inspection.py`、`worker_health.py`）无 `astra`；provider/unit/env/配置/文档统一 Nexus，旧 Worker `ASTRA_*` 被拒绝；`.astra-tasks` 为内部工作区目录，不是外部别名。
- [x] 8.6 真实 Linux Worker 验收：x86_64/aarch64 部署、重启、断网重连、故障注入、权限与密钥检查。验证：两架构 full deploy、x86 双 Worker、幂等、WAITING/native busy、Git、disable/restore、装备故障三项、ARM fault3/fault4、离线 SSH restart 与最终 fault matrix 全部 PASS；final5 460 passed / 1 MySQL skip。

## 9. 最终闭环复检（2026-10-19，当前源码）

- [x] 9.1 同 owner 过期租约：`run_maintenance_once` 每轮先恢复所有过期租约；续租失败后下一轮可恢复。验证：同 owner 过期与续租失败回归通过。
- [x] 9.2 reconcile 完整探测与 native idle：完整装备探测写真实 projection；独立读取原生 `/session/status`，busy/unknown/malformed 不恢复 `AVAILABLE`。验证：旧新鲜快照 + model/MCP 坏、native busy/malformed 回归通过。
- [x] 9.3 晚到控制指令：同环境未终态 `restart_opencode`/`switch_channel` 保留 `INTERRUPTED` 锁。验证：内部 switch_channel pending 指令回归通过。
- [x] 9.4 原子写与 Git askpass：严格读取、备份 600、PAT 只经 0600 文件路径；真实 HTTP Git 与真实 SSH/SFTP 临时目录验收通过。
- [x] 9.5 系统目录与内部动作：`/etc/systemd/system` 已存在不 chmod；内部 switch_channel retry/execute 拒绝，reconcile 释放路径保留。验证：临时目录与回归测试通过。旧安装遗留的 QA VM `/etc/systemd/system=700 root:root` 已在核实普通目录、非 symlink、root、700 后一次性恢复 755，仅修该隔离 VM，QA OpenCode PID 40203 / Worker PID 40204 前后不变；未增加长期兼容逻辑。证据：`docs/reports/evidence/nexus-v145-worker-recheck/systemd-directory-oneoff-20261019.json`。
- [x] 9.6 API 与事实投影：事件末页 `next_cursor=null`；restart/check 实际执行通道 fencing 写入；update 目标 commit、after snapshot 实际版本/模型/仓库仅取 installer/probe；Skill actual revision 用语义 content hash。验证：目标/实际未知与 Skill 漂移回归通过。
- [x] 9.7 当前源码 Linux 定向回归：final5 的 21 文件 + `test_fix_b3.py`，**503 passed / 1 skipped**，ruff 通过，源码 manifest `13917201…62000` 前后一致，宿主与 guest `app/tests` 218 文件逐文件 hash 漂移 0。证据：`docs/reports/evidence/nexus-v145-worker-recheck-20261019/`。
- [x] 9.8 QA Worker A 真实 deploy/redeploy：两次同配置 `SUCCEEDED`，6 项装备 verified，token/config/unit/worktree/PID 不变，API 响应敏感值命中 0。首次 preflight 因 API 重启后 Worker 短暂离线未提交部署，等待 online 后重跑。
- [x] 9.9 Skill 版本只聚合绑定集合：额外未管理 Skill 保留但不改变绑定 revision；绑定内容缺失时 actual 为 null。验证：绑定一致 + 额外 Skill、额外漂移、绑定漂移与缺失测试通过。
- [x] 9.10 Node 探测：使用每环境绝对 Node 路径，不依赖首次部署时的全局 node。验证：3 个真实子进程测试通过。
- [ ] 9.11 未验证边界：x86_64 本轮未复跑；MySQL 未跑；非 root Worker 未验证；模型仅认证/路由可见性，未推理。临时 QA 凭据需轮换，未更改生产凭据。
