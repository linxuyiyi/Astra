# 任务经验复盘（Experience Review）

> 状态：已实现核心链路，默认关闭。开启：`NEXUS_EXPERIENCE_REVIEW_ENABLED=true`；`NEXUS_EXPERIENCE_REVIEW_SHADOW_MODE=true` 时只分析不写入候选，正式启用需设为 `false`。

## 边界

- 只由人工确认完成触发；不恢复平台助手、TaskReview、TaskSummary。
- 复盘不改变 Task 状态、不占用执行 Worker、不创建 AgentScope 长期 Agent。
- 只写 `candidate`，人工审核后才能 `published`；不自动修改员工 Prompt/Skill/Harness 配置。
- 模型通过现有 Gateway `/v1/chat/completions` 调用。

## 链路

```text
POST /api/tasks/{id}/confirm
  → 同一事务写 TASK_COMPLETION_CONFIRMED Event + PENDING Job
  → Celery nexus-review worker
  → COLLECTING：按来源 Session 分页读取 OpenCode 原生消息
  → MAP：因果切片 + 局部 observation（片段结果持久化）
  → REDUCE：合并 observation，输出 domain / harness_improvement 候选
  → 服务端证据校验（Session 归属、message/part、密钥/伪造）
  → WRITING：KnowledgeService.mcp_submit_experience 写 candidate
  → SUCCEEDED / NO_FINDINGS
```

## 数据

| 表 | 作用 |
|---|---|
| `t_nexus_experience_review_jobs` | 一次确认对应一个 Job，保存状态、租约、来源快照与候选 ID |
| `t_nexus_experience_review_segments` | 轨迹片段与局部分析结果，支持断点恢复 |
| `t_nexus_experience_evidence` | 扩展 `source_system`、`native_session_id`、`source_digest`、`review_job_id`、`validation_status` |

不保存全量消息正文；片段只保存脱敏结构化 observation 与证据引用。

## 状态

`PENDING → COLLECTING → ANALYZING(MAP/REDUCE) → WRITING → SUCCEEDED / NO_FINDINGS`

可重试：`RETRY_WAIT`；终止：`FAILED`、`SUPERSEDED`、`SKIPPED_SOURCE_MISSING`、`SKIPPED_TOO_LARGE`。

## API

| API | 权限 | 说明 |
|---|---|---|
| `GET /api/library/review-jobs?task_id=` | reviewer/admin | 任务复盘列表 |
| `GET /api/library/review-jobs/{id}` | reviewer/admin | 状态、阶段、片段进度、候选 ID、错误 |
| `GET /api/library/review-jobs/{id}/evidence/{trace_ref}` | reviewer/admin | 按 trace 引用重新读取授权证据 |
| `POST /api/library/review-jobs/{id}/retry` | admin | 重试失败/无发现/来源缺失作业 |

任务详情页显示复盘状态、片段进度、候选数量与重试按钮。

## 配置

见 `ops/env/.env.example` 的 `NEXUS_EXPERIENCE_REVIEW_*`。模型建议使用独立 Gateway Key，限制模型与额度。人工确认的最终用量读取受 `NEXUS_FINAL_USAGE_MAX_MESSAGES` 限制。

## 当前限制

- 远程 Worker 使用只读 `read_review_trace` 命令分页读取并裁剪到约 128 KiB；单条消息仍可能被截断，完整原文需再定向读取。
- REDUCE 已实现“批次归纳 + 最终合并”两级；超过最终候选上限时由合并器去重，未实现无限层级树。
- restart 已把旧 Job 标记 `SUPERSEDED`，任务删除会把未完成 Job 标记 `SKIPPED_SOURCE_MISSING`；旧候选的过期/撤回策略仍可加强。
- 任务详情显示状态、片段进度和重试；数字图书馆详情显示改进对象、建议改动与原生证据，独立的前后对比视图仍可加强。
