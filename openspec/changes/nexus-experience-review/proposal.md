## 为什么

平台助手移除后，任务完成后不再自动沉淀经验。数字图书馆仍保留经验候选、审核、发布与检索能力，但缺少可信的生产者。需要由平台侧在人工确认完成后独立复盘真实执行轨迹，生成领域经验与 Harness 工作方法改进候选。

## 变更范围

- 新增 `t_nexus_experience_review_jobs`、`t_nexus_experience_review_segments`，扩展 `t_nexus_experience_evidence` 的原生来源字段。
- 人工确认完成事务内创建复盘 Job；Celery 扫描、认领、租约、重试与恢复。
- 分页读取 OpenCode 原生 Session，标准化、因果切片、Map/Reduce 与 Harness 对比分析。
- 服务端证据校验后，通过 `KnowledgeService` 写入 candidate；人工审核发布。
- 新增复盘查询、证据查看、重试 API 与任务详情入口。

## 非目标

- 不恢复平台助手、TaskReview、TaskSummary 或 14097 Runtime。
- 不给数字员工新增写知识 MCP Tool。
- 不自动发布经验，不自动修改数字员工 Prompt、Skill 或 Harness 配置。
- 本阶段不实现 Harness² 的自动 Propose–Probe–Compose。

## 影响

- 新增 0054 迁移；开启 `NEXUS_EXPERIENCE_REVIEW_ENABLED` 后才产生 Job。
- 复盘失败、超时、取消都不改变 Task 状态。
- 复盘模型通过现有 Gateway 调用，使用独立 Key 与预算。
