# OpenSpec 验证：nexus-interaction-reliability

## 完整性

唯一实施清单 tasks.md 的 10 项已完成。反例与最终结果位于 `docs/reviews/nexus-interaction-fixes-20261023/`；没有第二份实施计划。当前变更仅新增 Nexus capability，未改历史 Astra specs。

## 正确性：Scenario 对照

| Scenario | 实现 | 证据 |
| --- | --- | --- |
| 忙碌时连续发送 | main.send_private 传播拒绝状态；useMessages 返回失败，TextInput 保留草稿/附件 | 真实 TC10，页面真实 409 + 上传附件保留；test_busy_chat_is_an_explicit_conflict |
| 按岗位交接并追问 | employee_by_name 支持唯一岗位/ID；投递标识关联 HintBlock；原生投影及下一轮 assistant Hint 上下文 | 真实 TC05、来源标签截图；test_mailbox_hop_is_scoped_to_the_conversation_and_reply_is_visible |
| 岗位多义或协议输出 | 多义返回候选；DSML 不作为可用方案；只读已完成回复 | 多义/协议回归；相同正文不同投递与未完成回复反例 |
| 分析日志 | routing_text 排除引用/代码，选人和 followup 共享过滤结果 | 5 类引用反例，真实 TC14 |
| 三人长上下文 | 输入仍计总消耗，生成预算独立；参与者追加持久化，delta 带来源 | 真实 TC16，budget/followup 回归；真实游标重放 |
| 失败工具与邮箱 | 失败工具不计成功操作；0s 不是运行态；邮箱不挂无来源任务 MCP | 2 项摘要 Node 测试，真实页面；workspace/mcp 200 且无 nexus-task |
| 发现分类并创建 | categories 复用启用分类与原鉴权；task_type 枚举 | 分类/枚举回归，真实 REQUIREMENT/PENDING 任务及 SSE 群创建 |

SQLite 两条异步事务反例证明原有事件循环同步写锁等待会自阻塞；调度/租约/持久化锁等待在线程执行，发送与打断走同步路由线程池，SSE 心跳等待后重查成员。真实 MySQL 8.0.36 的邀请并发及事件提交顺序 2 项通过。

## 一致性

AgentScope 保留消息事实源，Nexus 不增对话数据库或 Agent 编排。人工确认、多私聊保持原有契约。API 与 Agent Service 必须一起更新/重启，新的投递 ID 通过 HintBlock 贯穿；历史无标识记录不按正文猜测回填。并发私聊明确采用拒绝模式，不承诺队列。

## 最终检查

- 后端：1283 passed，32 skipped；跳过不计通过，保留原依赖弃用警告。
- Ruff：通过。
- chat-island：17 项测试、类型检查、构建通过。
- web：类型检查、构建通过；聊天岛先构建，public/dist 一致。
- OpenSpec strict、diff check、运行态密钥值扫描：通过。

## 边界

CRITICAL：无本变更契约范围内的已知未实现项。

WARNING：DSML 只完成坏回复防护，上游成因未闭环；真实 Worker 执行、公司部署、第三方 MCP 未验收；多方同时委托/嵌套邮箱与重启窗口没有全面 E2E。详见复核报告。当前不以本地结果代替生产验收，不归档为公司部署完成。
