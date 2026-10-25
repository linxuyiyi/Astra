# Verification Report: nexus-collaboration-closure

## Summary

| 维度 | 结果 |
|---|---|
| 完整性 | 8/8 任务，4/4 Requirements |
| 正确性 | 7/7 Scenario 有代码与验证证据，真实 QA 闭环通过 |
| 一致性 | 复用既有 Task/Event/Outbox、群日志、Agent Service 和复盘记录；无新 Agent 内部编排或重复状态机 |

## Scenario 映射

| Scenario | 实现 | 验证 |
|---|---|---|
| 群内联系真实成功 | `nexus_api/main.py:2919` | `test_peer_http_response_matches_committed_delivery`，旧代码完整 HTTP 500 反例；真实 A/B 联系工具成功 |
| 正常多步链与预算隔离 | `services/agent-service/group_security.py:67`；`nexus_api/collaboration.py:1440` | `test_group_toolkit_filters_tools_by_server_origin` 验证副本预算 8、原值 99；真实 Skill/分类/仓库/建单；规则补充后单条问题完成 A→B→A |
| 讨论结束后的任务交付 | `app/runtime/task_wakeup.py`；`nexus_api/collaboration.py:925`；`GroupApp.tsx:163` | `test_task_notification_survives_closed_discussion_and_is_idempotent`；真实 WAITING/SUCCEEDED 回原群并点击任务链接 |
| 重试与归属 | 同上、`main.py` 的 wake-target | 错来源拒绝、内部认证、幂等及私聊通知测试；真实事件 158 两次重投仍只一条 |
| 总结查询 | `app/task_mcp.py:212`；`app/task_tools.py:399` | 注册/参数、空 Job、真实 Job/候选测试；真实新任务 NO_FINDINGS/0、原任务 SUCCEEDED/1 与 published/verified 知识 |
| 防止越权与私密元数据泄漏 | `TaskTools._owned` 与候选 source_task_id 筛选 | `test_summary_uses_real_review_and_preserves_ownership`；最终凭据/上下文扫描 0 命中 |
| 流失联期间完成 | `web/workspace-ui/src/stream-recovery.ts`、`adapter.tsx:99`、`TaskDetailView.vue:232` | EOF/读异常重连与主动取消及重连取消竞态 3 项测试；最终构建真实受控断线 18 秒后自动恢复历史和已完成状态；窗口内恰好完成的长任务未另做生产实测 |

## 验证结果

- 扩大后端 1338 passed / 33 skipped，最终相关回归 55 passed；Web 30、聊天岛 17、SSE 3 项通过。
- Ruff、三套前端类型检查及构建、OpenSpec strict、差异与实际凭据扫描通过。
- 真实 Worker 交付：旧代码 6 项失败→修复 7 项通过；独立复验 7 边界与 6 排列通过。
- 本轮 Task 已确认完成；3 个 QA Worker 0 占用，原生 Session 空闲，后台复盘与群讨论结束。

## Issues

CRITICAL：无。

WARNING：Worker 自然断线根因未定位，修复范围是有反例的页面恢复；模型回传已补规则并实测，但首次建单链仍用过一次提醒；没有 MySQL 公司生产或长期无人值守验收。

完整证据与实际进程/构建范围见 `docs/reviews/nexus-collaboration-closure-fix-20261025/VERIFICATION.md`。本 change 保持活动状态，后续代码合入与远端推送不代表 DSH 或生产已验收。
