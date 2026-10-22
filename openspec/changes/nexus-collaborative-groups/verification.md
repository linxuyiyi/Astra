# nexus-collaborative-groups 验证记录

日期：2026-10-23（Asia/Shanghai）。
分支：`codex/nexus-group-collaboration`。
环境：macOS 26.5.2 arm64、Python 3.14.3、Node 25.4.0、MySQL 8.0.36、AgentScope 2.0.10。
隔离验收服务：API `127.0.0.1:18240`、Agent Service `127.0.0.1:18241`、网关 `127.0.0.1:18242`、MySQL `127.0.0.1:18243`。

## 结论

- `openspec/changes/nexus-collaborative-groups/tasks.md` 的 12 项任务全部完成。
- Ruff 通过；后端全量回归通过；两套前端 type-check/build 通过；真实 MySQL 8.0.36 迁移与 Alembic check 通过；两个真实浏览器用户 + 真实 AgentScope/网关模型的 E2E 通过。
- 本轮没有关闭认证、没有替换 AgentScope、没有改已发布 `0001_init`。

## 1. 静态检查

```bash
cd services/nexus-api
.venv/bin/ruff check app nexus_api tests
```

结果：`All checks passed!`。

## 2. 后端回归

```bash
cd services/nexus-api
.venv/bin/pytest tests/platform tests/nexus -q -p no:cacheprovider --tb=short
```

结果：`1233 passed, 32 skipped, 10027 warnings in 175.41s`，退出码 `0`。完整输出保存为 `docs/reports/evidence/nexus-collaborative-groups/backend-pytest.log`。

32 个 skip 中，12 个是本轮为 Darwin 明确标注的 Linux-only 用例（`openat2`/`O_PATH`/`remote_install.sh`）；其余为仓库既有 skip。它们不是本轮功能的跳过。

## 3. 群协作针对性用例

```bash
.venv/bin/pytest tests/nexus/test_collaboration.py tests/nexus/test_groups.py -q
```

结果：`23 passed`。其中：

- `test_collaboration.py` 16 项：身份冒充、邀请/退出/撤权、并发幂等分页、有序真实上下文、新消息打断、不同人类排队、SSE 撤权、附件撤权、配置越权、工具策略、Token 上限、过期租约恢复、未确认停止保留租约。
- `test_groups.py` 7 项：既有员工邀请 workspace、Agent Service 内部密钥、群运行围栏来自原始输入、工具来源过滤、复用 Session 无围栏拒绝。

## 4. 最后修改的回归

| 最后修改 | 覆盖方式 |
| --- | --- |
| `collaboration.py` 旧讨论停止未确认时保留租约 | `test_expired_lease_interrupts_stale_run_then_processes_queue`、`test_unconfirmed_stop_keeps_lease_for_recovery` |
| Token 上限与输出上限 | `test_token_budget_stops_following_employee_and_caps_output`，断言 `MAX_OUTPUT_TOKENS=768`、累计 usage 超预算后不再调用第二名员工 |
| Agent Service 工具围栏与 MCP 来源 | `test_group_toolkit_filters_tools_by_server_origin`、`test_group_toolkit_rejects_reused_session_without_matching_fence`、`test_tool_policy_requires_user_grant_and_matching_mcp_ref` |
| Agent Service 服务间密钥 | `test_agent_service_requires_service_key` |
| Task MCP 发起者与群会话归属 | `test_middleware_does_not_trust_employee_header_alone`（合法私聊 200、伪造/他人 Session 403） |
| 群任务不能覆盖服务端来源 | `test_group_task_cannot_override_server_side_source` |
| E2E 建群/加入导航时序 | 脚本等待新 `group` 查询参数、邀请链接非空、加入后群组一致、退出撤权轮询 |

本轮修复的两个实现问题：

- 过期租约发现旧运行未停止时，原实现让 `TimeoutError` 逃出后台任务；现改为记录告警并保留租约等待恢复。
- 群工具在无原始 `discussion_id` 围栏时会先组装 toolkit 再拒绝；现改为先校验围栏，再组装工具。

## 5. MySQL 与迁移

真实 MySQL 8.0.36 上并发契约：

```bash
NEXUS_TEST_MYSQL_URL='mysql+pymysql://root@127.0.0.1:18243/' \
  .venv/bin/pytest tests/nexus/test_group_mysql.py -q
```

结果：`2 passed`（一次性邀请并发只允许一人加入；群事件游标跟随提交顺序）。

迁移与 Alembic check：

```bash
ASTRA_DATABASE_URL='mysql+asyncmy://root@127.0.0.1:18243/nexus_alembic_check' \
  .venv/bin/alembic upgrade head
ASTRA_DATABASE_URL='mysql+asyncmy://root@127.0.0.1:18243/nexus_alembic_check' \
  .venv/bin/alembic check
```

结果：`0001_init -> 0002_collaborative_groups` 成功，`No new upgrade operations detected.`。临时库已删除。

## 6. 前端

```bash
cd services/nexus-api/chat-island && npm run type-check && npm run build
cd ../web && npm run type-check && npm run build
```

结果：两套均退出码 `0`。聊天岛产物写入 `services/nexus-api/web/public/nexus-chat`，Web 产物写入 `services/nexus-api/web/dist`；旧的带哈希静态文件已由构建清理，新的哈希文件已纳入提交。

## 7. 浏览器 E2E

脚本：`tools/e2e/nexus_group_collaboration.mjs`。两个独立 Chromium、两名新注册人类用户，真实 API / Agent Service / 网关模型。

```bash
NEXUS_E2E_URL=http://127.0.0.1:18240 \
NEXUS_E2E_OWNER_REGISTER=1 \
NEXUS_PLAYWRIGHT_MODULE=/Users/niuniu/程序/Astra/frontend/node_modules/playwright/index.mjs \
NEXUS_E2E_EVIDENCE=/tmp/nexus-group-browser-final \
node tools/e2e/nexus_group_collaboration.mjs
```

结果：

```json
{"passed":true,"channel_id":"ch_d38ebb897578","human_realtime":true,"message_alignment":true,"concurrent_order":true,"leave_revoked":true,"page_errors":[]}
```

同次运行中两名员工按顺序回复：

- 小牛码先自我介绍，并向小掌柜提出岗位问题。
- 小掌柜读取到小牛码的真实公开回复后回答，没有人类重复 @。

证据：`docs/reports/evidence/nexus-collaborative-groups/`（`results.json`、`browser-run.log`、`owner.png`、`colleague.png`、`discussion.png`）。

验收前已重启隔离 API 与 Agent Service，使进程加载本轮最后代码。

## 8. 旧群聊测试替换后的覆盖

`tests/nexus/test_groups.py` 原先的跨 Session 合并/游标用例被群公开日志取代，覆盖对应关系如下：

| 旧用例 | 新事实源与覆盖 |
| --- | --- |
| `test_backlog_keeps_original_ids_until_the_cursor_moves` | 新实现不再跨 Session 合并；`test_ordered_real_previous_reply_and_finite_turns` 断言后发言员工拿到前序真实消息 ID/姓名/正文 |
| `test_timeline_dedupes_humans_and_team_hints_and_pages` | 跨 Session 去重随旧架构移除；`test_humans_concurrent_idempotent_paginated_and_no_model_calls` 覆盖分页、幂等、两人并发与无模型触发 |
| `test_timeline_includes_the_employees_own_reply` | `test_ordered_real_previous_reply_and_finite_turns` 断言员工回复以 `sender_type=employee` 持久化且正文与 AgentScope 原生内容一致 |
| `test_human_event_skips_the_sender` | 群 SSE 向全员广播持久事件；浏览器 E2E 断言每个页面自己/他人消息各一条，历史无重复 |
| `test_channel_mention_replays_the_unmentioned_line_with_its_id` | `test_ordered_real_previous_reply_and_finite_turns` 与 `test_no_mentions_and_single_mention` 覆盖员工输入包含真实前序人类消息、无提及时的路由与单人提及 |

## 9. OpenSpec 场景核查

| Requirement / Scenario | 证据 |
| --- | --- |
| 禁止冒充 | `test_identity_cannot_be_claimed_by_headers_or_cookie`、`test_config_management_is_not_granted_to_chat_users` |
| 成员生命周期 | `test_lifecycle_permissions_invite_read_leave_and_revocation`、`test_existing_sse_connection_stops_when_human_leaves`、E2E `leave_revoked` |
| 多人并发与重试 | `test_humans_concurrent_idempotent_paginated_and_no_model_calls`、MySQL `test_group_event_cursor_follows_commit_order`、E2E `concurrent_order` |
| 历史与流一致 | `test_ordered_real_previous_reply_and_finite_turns`、E2E `human_realtime`/`message_alignment` |
| 无提及的招呼与职责问题 | `test_no_mentions_and_single_mention`，真实环境见 `docs/reports/evidence/nexus-local-qa-20261016` 之外的本次 E2E 与历史 live 场景 |
| 提及选择与连续交流 | `test_ordered_real_previous_reply_and_finite_turns`、E2E `employee_discussion` |
| 人类正常交流 | `test_humans_concurrent_idempotent_paginated_and_no_model_calls`、E2E 两名人类互见 |
| 限额与打断 | `test_new_message_interrupts_and_replaces_goal`、`test_token_budget_stops_following_employee_and_caps_output`、`test_expired_lease_interrupts_stale_run_then_processes_queue` |
| 双重权限 | `test_tool_policy_checks_human_and_employee_and_retired_binding`、`test_tool_policy_requires_user_grant_and_matching_mcp_ref`、`test_group_toolkit_filters_tools_by_server_origin` |
| 消息身份与布局 | E2E 左右对齐与 1440×900 无横向溢出断言，截图见证据目录 |

## 10. 未验证与剩余风险

- 12 个 Linux-only platform 用例在 Darwin 跳过（`test_worker_agent_files.py` 整体、Phase 4 安装文件命令、`remote_install.sh` 两个用例）。它们与本轮群协作无关，但正式验收应在 Linux 上完整执行一次。
- 浏览器 E2E 使用新注册的普通账号，不覆盖初始管理员账号的首账号引导；管理员认领旧群只由后端测试覆盖。
- E2E 的模型回复内容随真实模型变化，脚本只断言状态、人数、顺序与身份；没有断言具体措辞。
- 没有在真实多进程部署中杀进程验证租约恢复；租约恢复由数据库状态与假 AgentScope 在集成测试中覆盖。
- 全量回归有既有 SQLAlchemy `SAWarning` 与 `pytest_asyncio` 弃用警告，不影响结果，未在本轮顺手修改。
