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

见 `ops/env/.env.example` 的 `NEXUS_EXPERIENCE_REVIEW_*`。模型建议使用独立 Gateway Key，限制模型与额度。

## 当前限制

- 远程 Worker 仍复用现有 `messages_page` 通道；超大单条消息的专门分帧协议尚未实现。
- REDUCE 目前按批次归纳后合并，未实现无限层级 Reduce 树；批次上限由 `NEXUS_EXPERIENCE_REVIEW_REDUCE_INPUT_TOKENS` 控制。
- restart 后旧 Job 的 `SUPERSEDED` 标记、任务删除的完整清理仍需补齐。
- 任务复盘 UI 只显示状态与候选入口，数字图书馆的“改进前/后/证据”专用视图待补。
