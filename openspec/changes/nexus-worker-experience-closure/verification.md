# nexus-worker-experience-closure 验证

2026-10-23。当前实施清单为本 change 的 tasks.md；运行报告与证据位于 `docs/reviews/nexus-worker-experience-20261023/`。按 openspec-verify-change 流程读取 status、apply context、proposal、design、三份 delta spec，并核实真实代码和以下证据。

## 完整性、正确性与一致性

| 维度 | 结论 |
| --- | --- |
| 完整性 | 9/9 实施任务；7 条 Requirement、16 个 Scenario 有实现与测试/运行证据 |
| 正确性 | Worker、登录账号和服务间认证分别复用其既有机制；仓库目标确定；上下文、复盘、读取归因及人工释放实际贯通 |
| 一致性 | 没有新 Agent/Memory、任务状态机或长期兼容分支；唯一占用谓词和正式 worker 执行保持；旧异常只用显式一次性脚本 |

## Scenario 映射

| Capability / Scenario | 实现 | 验证 |
| --- | --- | --- |
| Worker 无浏览器会话 | nexus_api/auth.py；既有 worker_gateway 路由依赖 | test_unified_worker_auth：有效 token、管理密钥、无效 token；test_worker_gateway/data_channel 的 epoch 和归属回归；正式 Linux Worker 真实执行 |
| 管理密钥不足 | 同上 | 未携管理密钥登记返回协议 401，不受其他账号 API 放宽影响 |
| 管理员正常维护 | nexus_api/service.py 依赖适配 | test_maintenance_uses_login_uid_without_legacy_identity；真实官方部署/更新成功 |
| 维护伪造身份 | 同上 | 同一测试 spoof 参数；真实登录 UID 记录 |
| 未登录/普通聊天维护 | 认证中间件与既有维护权限 | 真实中间件返回 401/403，无维护记录 |
| 取消后确认释放 | models.Task.execution_released；workspace_routes；TaskDetailView | 两个真实取消任务；native idle 独立核验；页面明确确认；current_task_count=0；MySQL 8.0.36 相同占用/释放边界 |
| 审核/停止身份不可伪造 | 统一组合根 library_principal/execution_stop_actor | test_library_audit_identity_uses_authenticated_account、test_stop_release_uses_login_and_exposes_canonical_occupancy；普通聊天请求仍由统一权限门拒绝 |
| 创建指定 Worker | task_tools.create | test_task_mcp_selects_executable_repository(worker)；三个真实员工创建的任务保存正确目标并执行 |
| 显式仓库入池 | task_tools.create/repositories | 同测试 explicit；只返回仓库 id/url/branch，不读取 GitCredential |
| 缺少/冲突目标 | 同上 | missing/mismatch 测试不写 Task；真实旧反例 repository_id=null 保留；新创建拒绝空目标 |
| 调整 Worker | task_tools.assign | test_task_reassignment_preserves_repository：跨仓库拒绝，保留目标及 Worker |
| 相同内容不同仓库 | 派生幂等键含 repository_id | test_repositories_do_not_leak_credentials_and_task_keys_include_target；重复同一目标仍返回同一 Task；MySQL 同目标幂等通过 |
| 新任务读取正式经验 | runtime_context、trace_normalizer、evidence | 原生 ignored canary 真实对照；最终新任务首次 library_get 有真实知识/Task/Session 关联；归一化与候选层均拒绝 ctx 泄露 |
| 复盘失败恢复 | worker trace_read；coordinator；candidate_writer | 真实旧 source_unavailable→官方更新→重试 SUCCEEDED；5 类路径回归含允许边界内的另一 Git 仓库；清旧错误及 review_job_id 回归；29 条实际证据关联正确 |
| 编译来源页 | library_service.mcp_get | 真实 Wiki YAML sources 与已发布源路径匹配；最终新任务首次读取归因正确，知识详情显示引用任务、reference_count=2 |
| 模糊/伪造引用 | 同上 | test_llm_wiki：正文 ID、混合外部来源、多来源不猜测；旧 runtime_context 测试夹具补真实发布 source_path，不恢复旧正文匹配行为 |

## 验证结果

- 后端全量：1306 passed、32 skipped、0 failed；补强凭证和边界内另一仓库测试后，再运行经验/知识/上下文/统一鉴权针对性集合。
- Ruff、web 30 Node 测试、type-check/build、真实浏览器检查通过。
- 三次真实任务完成，各自复盘成功；最终新任务自动领取/执行/首次读经验/人工确认/新候选完整通过。重复确认不增加 Job。
- MySQL 8.0.36 独立进程、新库 Alembic head 和针对性仓库/占用测试通过，实例已停止；全链真实任务仍使用隔离 SQLite。
- OpenSpec strict 验证结果见验收证据 `openspec-validation.txt`。

## CRITICAL

无未完成实施任务或已知未覆盖的本 change 必须行为。

## WARNING / 适用边界

- 未把本地 Linux Worker + SQLite 验证扩大为公司 MySQL 全链部署、非 root 权限、运行中断电或外部 Git push 验收。
- 真实公司第三方 MCP 和 DSML 上游根因仍待独立验收。
- Worker 曾使用管道掩盖测试退出码；本轮已独立重跑证明 old=1、fixed=0。工具 completed 不等于测试成功，平台没有通用命令语义守卫。
- 数字经验沉淀是人工审核后的正式图书馆检索，不代表 AgentScope 个人 Memory 自动更新。相近候选仍需人工合并。
- 旧 QA Job 只一次性修复可确定的关联；历史通知失败和早期未归因读取保持原事实。

本 change 保持可审阅状态，不修改历史 Astra Spec，不以验证完成代替公司上线验收。
