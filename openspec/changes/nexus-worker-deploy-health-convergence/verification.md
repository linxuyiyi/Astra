# Worker 部署与健康收敛核验

> 本文记录本地测试阶段。后续用户要求的真实 Linux 部署、真实模型任务与故障恢复已执行；最新结果和修复见 [real-e2e-verification.md](real-e2e-verification.md)，下文“未进行真实远端”是当时的验证边界。

核验日期：2026-11-02。工作分支：`v1.4.9`；基线和 DSH 归档分支均为 `e08f50933c12c553caa6753d511aa4b45a2c1626`。本报告按 `/opsx-verify` 检查完整性、正确性与设计一致性，不代表远端生产验收。

## 原因与本轮补齐

交接实现尚未完成前端契约迁移，部分测试、隔离检查脚本仍引用已删除模块；同步完整应用测试还会跨事件循环清理 Redis，已改为异步路由测试或隔离应用，同时保留主应用挂载断言。继续核查发现：部署验收拼接阻断对象会报错、启动错误字段读取错误；新连接缺失快照时可能继承旧连接的就绪结果；更换主机时复用凭据不能隔离旧连接；仅比较仍保留的装备项会漏掉删除 Skill/MCP 绑定；本机检查调用签名与真实探针不一致；重启等待超时后协程可能继续执行。

本轮已修复上述问题，统一由 `worker_readiness.py` 判定就绪，删除只为旧测试保留的装备快照参数和重复健康映射。部署等待 Worker 上报快照，去掉补拉巡检；复用现有期望规格摘要，通过守护环境和观测快照对比已应用配置。保存配置不自动同步，不根据进程退出推断任务终态。

前端列表读取内嵌 `worker_health`，展示待同步；维护状态与动作收窄为约定集合；调度开关同步 PATCH；错误摘要直接展示；仅活动操作订阅 SSE。

## Scenario 对照

| 契约场景 | 代码事实源 | 验证证据 |
| --- | --- | --- |
| 展示字段修改保持就绪；模型、Skill、MCP、分支修改仅标待同步 | `app/runtime/worker_readiness.py`、`app/api/routes.py` | `test_worker_readiness.py::test_display_fields_stay_ready_while_functional_or_host_edits_mark_drifted`；`test_worker_maintenance.py::test_list_workers_embeds_health_and_patch_enabled_plus_put_environment` |
| 删除绑定仍需手动部署；不把额外非受管 Skill 误判为漂移 | `spec_revision` 与实际内容对比 | `test_removing_managed_bindings_stays_drifted_until_redeploy`；`test_worker_equipment_v145.py::test_managed_skill_revision_ignores_unmanaged_extra_skills` |
| 主机变更阻止旧节点派单；新连接不沿用上一连接观测 | 网关 epoch、目标指纹、凭据轮换 | `test_redeploy_to_new_host_rotates_token_and_fences_old_connection`；`test_reconnect_without_snapshot_cannot_reuse_previous_connection_readiness`；`test_gateway_heartbeat_snapshot_is_fenced_by_epoch` |
| 列表一次响应返回健康摘要；跳过不健康候选 | 批量列表接口、Dispatcher | 列表接口测试；`test_dispatcher_skips_drifted_or_offline_worker_and_assigns_healthy_peer`；隔离 UI 回归断言列表零次 `/health` 请求 |
| 本地变化增量推送，日常不访问远端仓库 | `worker_agent/connection.py`、`inspection.py` | `test_worker_daemon_supervisor.py` 的 crash/report 与 periodic/deep 探针测试；`test_worker_runtime_inspection_v145.py` |
| 单实例锁、锁不传给子进程、强杀后重入、定向孤儿清理 | `worker_agent/supervisor.py`、`__main__.py` | 真实本地进程 flock/FD_CLOEXEC 测试；隔离 `/proc` 夹具清理测试 |
| OpenCode 崩溃自愈并上报错误，守护终止清理子进程 | `OpenCodeSupervisor` | 本地真实 HTTP 子进程崩溃、退避重拉起、错误上报及停止测试；重启超时取消测试 |
| 任务运行中连接或进程异常不猜测 Task/Session 终态 | 既有 RuntimeService/recovery | `test_stage5_regression.py::test_network_timeout_and_process_restart_keep_the_same_session`；会话恢复回归 |
| 离线节点轻量唤醒；未安装时拒绝 | `maintenance.py::_execute_restart`、`ctl.sh` | 在线 Gateway、离线 `ctl.sh start`、入口缺失、OpenCode 不健康的维护测试（SSH 为夹具） |
| 部署直接验收，无历史通道切换及重复收尾探测 | `remote_provisioning.py::_converge_readiness` | `test_deploy_acceptance_reports_snapshot_blocker_without_command_probe`；幂等安装与凭据真实 Git 测试 |
| 取消、租约过期、服务关闭后释放维护锁；占用时拒绝破坏性动作 | `maintenance.py` 的五状态流程 | `test_worker_maintenance.py` 并发提交、取消、过期、关闭、占用与恢复测试 |
| 按需深度检查；调度开关同步生效 | `inspection.py`、PATCH 接口、维护面板 | deep/local 探针测试；enabled 接口测试；隔离 UI 回归确认两次 PATCH、零次异步启停操作、终态无 SSE、活动态订阅 SSE |
| DSH 归档、纯 OpenCode 主线、迁移不修改已发布 init | 归档引用、新 `0007`、主线源码 | 归档 SHA 核对；运行源码无 DSH 路径；`test_fresh_sql_storage.py` 初始化、增量升级和中断锁清理测试 |

## 验证结果

- 针对性后端回归：102 passed。
- Web 单元测试：79 passed。
- Web 和 chat-island：各自 type-check、build 通过。构建保留现有大 chunk 提示。
- 隔离浏览器回归：`tools/e2e/nexus_worker_deploy_health.mjs` 通过；截图和结果位于 `/tmp/nexus-worker-deploy-health-ui/`。
- Ruff、OpenSpec validate、`git diff --check` 通过。
- 原始提交的 ASR 路由测试：2 passed；本次失效由导入已迁移的私有入口造成，已改为验证新事实源。
- 最终全量后端回归：`.venv/bin/pytest -q -x tests/platform tests/nexus`，1354 passed、32 skipped，耗时 211.74 秒。32 项可选环境验证未运行；保留 Python 3.14/pytest-asyncio 弃用等现有警告，不计为验收通过的额外场景。

## 剩余风险与一致性结论

未进行真实远端 SSH 宿主机或容器重启验证；守护自愈、锁和退出联动证据来自本地进程与脚本测试，离线唤醒使用 SSH 夹具。`start-all.sh` 需由部署方接入容器或宿主机启动钩子；未接入时使用管理员轻量唤醒。尚未在真实 MySQL 8.0.36、真实模型及真实远端 Worker 组合环境执行部署验收，2 秒级恢复时间未实测。

已发布迁移 `0001`–`0006`、`remote_bootstrap.py`、`remote_artifacts.py` 与交接列出的无关目录保持原状。本轮不提交、不推送、不修改历史 `openspec/specs/`，保留 change 供审核。完整性：9/9 项任务完成；正确性：6/6 项 Requirement、13/13 个 Scenario 有实现与对应自动化证据；设计一致性核验通过。无阻塞代码问题，真实远端环境验收仍按上述风险保留。
