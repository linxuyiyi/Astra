## 1. DSH 分支归档与主线净空

- [x] 1.1 基于当前 `v1.4.9` HEAD 创建归档分支 `archive/dsh-acp` 保留完整 DSH 实现，随后在 `v1.4.9` 移除 DSH 专属文件（`app/worker_agent/dsh_acp/`、`app/runtime/dsh_*.py`、`DshWorkbench.vue`、`tests/dsh/`）及 22 个公共文件中的 `dsh` 分支与 Wheel 打包残留，验证后端 `pytest` 与前端 `type-check` 通过。

## 2. 数据模型分表与单一就绪判定

- [x] 2.1 新增单个 Alembic migration 与 ORM 更新：在 `t_nexus_worker_instances` 增加 `target_digest`、`observed_snapshot`、`observed_at` 字段，移除 `t_nexus_opencode_execution_environments.worker_channel` 及 `t_nexus_worker_maintenance_operations` 的废弃核对字段（不修改 `0001_init`），并通过迁移与 schema 校验测试。
- [x] 2.2 扩展 `WorkerGateway` 在 `authenticate` 与 `heartbeat`（`kind="report"`）中按当前 `epoch` 原子写入 `observed_snapshot`，实现纯函数 `evaluate_worker_readiness()`（对比期望配置、目标指纹、Skill 内容哈希与观测快照），下线 `capability.py`、`worker_channel.py` 与 `health_observer` 后台轮询，覆盖“改展示字段保持就绪 / 改模型或能力仅标待同步且不自动热同步 / 改主机指纹阻断旧连接”单测。
- [x] 2.3 修复 `PUT /api/agent-workers/{id}/environment`（只写期望配置，不抹除观测态、不中断在途任务）与 `Dispatcher.claim_once()`（候选循环内跳过不可调度节点继续分配后续健康节点），并在 `GET /api/agent-workers` 及详情接口内嵌就绪与装备摘要，通过调度与接口单测验证。

## 3. 无 systemd 单守护自愈与部署/维护收敛

- [x] 3.1 重构 `app/worker_agent`：实现基于 `worker.lock`（`flock LOCK_EX | LOCK_NB` + `FD_CLOEXEC`）的单实例守护、`OpenCodeSupervisor` 子进程保活（退避拉起、`NEXUS_WORKER_ENV_ID` 孤儿清理、`last_error` 上报、终止联动，且任务运行中重启子进程不猜测任务终态）与纯本地周期自检（日常自检不跑 `git ls-remote`），删除 `process_manager.py` 与 `channel_switch.py`，补充守护进程与探针测试。
- [x] 3.2 精简 `remote_provisioning.py`（保留 `remote_bootstrap.py`；真实 E2E 后对 `remote_artifacts.py` 补充 registry 相对制品路径解析，保留来源与完整性校验）：生成远端标准入口 `ctl.sh` 与容器聚合脚本 `start-all.sh`，移除收尾 `ssh -> worker` 通道切换与重复探测，删除 `remote_processes.py`，支持已装环境快速幂等更新配置并重载 OpenCode，通过部署流程单测验证。
- [x] 3.3 收敛维护服务为 5 状态无死锁模型（`QUEUED | RUNNING | SUCCEEDED | FAILED | CANCELED`）：移除 `INTERRUPTED` 状态、`/reconcile` 接口与 `disable/restore` 异步动作，实现租约过期/取消自动释放互斥锁、在线 `restart / update / check`（含按需 `git ls-remote` 深度检查）及离线已部署节点轻量 SSH `ctl.sh start` 快速唤醒，通过维护状态机与并发/取消/重启恢复测试。

## 4. 前端收敛与全链路回归验证

- [x] 4.1 更新 `services/nexus-api/web` 员工列表与维护面板：移除列表页 N+1 `/health` 请求、`/reconcile` 接线与 `INTERRUPTED` 死锁提示，`enabled` 改为同步开关，展示“待同步/一键部署”状态与 `opencode` 启动错误摘要，仅在活跃操作期间订阅 SSE。
- [x] 4.2 运行统一后端测试（`pytest tests/platform tests/nexus`）、`ruff check app nexus_api tests`、前端与聊天岛 `npm run type-check && npm run build`；检查 `git diff` 确认无多余兼容逻辑或无关修改，对无真实远端 SSH 宿主机的物理机重启场景如实标注验证边界。

验证结果和真实环境边界见 [verification.md](verification.md)。

## 5. 真实 Linux 部署验收（用户追加）

- [x] 5.1 在用户原 Linux 测试机清理已确认归属的旧 Worker 环境，以当前源码完成首次部署、幂等部署、配置漂移与手动同步、在线维护、故障自愈和离线唤醒，并记录真实模型验证、失败修复及整机重启等未验证边界。

真实 Linux 结果、失败修复、计时与未验证边界见 [real-e2e-verification.md](real-e2e-verification.md)。
