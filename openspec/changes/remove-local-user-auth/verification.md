# 清理验证：remove-local-user-auth

日期：2026-10-24。分支：codex/remove-local-user-auth；起点为 v1.4.8。原目录的未跟踪文件保持原样，未提交、未推送、未操作现有部署数据库。

## 完整性

6/6 项实施任务已执行。删除注册、登录、退出、账号列表、权限编辑、密码哈希和登录 Cookie 会话；前端无本地权限门。临时 UID 沿用 nexus_owner_id + nexus_uid / X-Owner-Id，明确不代表认证。服务间密钥、Worker token、群角色、讨论 lease/generation 与员工能力保留。

## 正确性与场景覆盖

| 场景 | 代码证据 | 验证 |
| --- | --- | --- |
| 旧账号入口删除 | nexus_api/auth.py 不再注册账号路由；App.vue、api.ts 删除登录 | test_local_account_and_permission_routes_are_removed；真实浏览器旧 API 不存在（GET 404，POST 在静态页兜底下 405）；无密码输入框 |
| 临时 UID 记录与复用 | App.vue / api.ts / auth.py | 浏览器输入 UID、localStorage 与 Cookie、/me 返回、刷新后仍进入聊天；test_me_records_temporary_browser_uid_cookie |
| 群归属与员工能力 | collaboration.py / group_task_auth.py | 群成员退出、撤销 SSE、工具配置与退役绑定测试；浏览器创建群组成功；Task MCP 服务密钥测试 |
| 迁移清理 | 0004_remove_local_auth.py、CURRENT_REVISION | 空库初始化、重复执行、从 0001 与 0003 升级、schema metadata 一致；验证账号和会话表删除、会话 owner 与群角色 ID 保留 |

临时 UID 的浏览器证据在 /tmp/nexus-remove-auth-browser-evidence/：01-uid.png、02-chat.png、03-group-dialog.png、04-group.png、report.json。浏览器无 pageerror。运行的是隔离 SQLite 和真实本次 FastAPI 路由/前端，未接真实 AgentScope；不等同公司生产验收。

## 验证结果

- 最终相关后端回归：187 passed，6 skipped（tests/nexus + unified_worker_auth + task_mcp_auth + gateway_speech + fresh_sql_storage）。
- Ruff：app、nexus_api、tests 与新 migration 均通过；git diff --check 通过。
- web：type-check/build 通过，交付 dist 中确认无本地登录表单/权限门文案及 auth/session 调用。
- chat-island：type-check/build 通过；无源代码改动，已撤回此次无关构建 hash 变化。
- 两个 E2E 脚本 node --check 通过；群聊完整真实模型 E2E 未运行，原账号进入流程已改为临时 UID。
- OpenSpec validate 通过；按此次 specs 的全部 Scenario 完成源代码、测试与浏览器证据核对。

## 剩余风险与未验证

扩大平台回归未全绿：test_deployment_gateway.py::test_existing_wiki_never_invents_credentials 失败（安装器未按用例拒绝缺失 Wiki token），在该点前 76 passed；排除该文件继续，test_employee_serve.py::test_two_employees_start_and_reuse_independent_serve 失败（实际 OpenCode 1.18.35，用例路径要求 1.18.33），此前 92 passed。这些文件本次未修改，不将全量回归宣称通过。

MySQL 8.0.36 实库迁移与公司登录未验证。本次 migration 只在隔离 SQLite 执行；部署环境需执行 alembic upgrade head 才会删除旧凭据表。新身份目录只记录临时 UID/展示名，不复制旧账号。临时 UID 不提供身份认证，后续由公司登录替换；历史本地账号与公司身份之间没有自动猜测映射。

## 一致性结论

实现符合用户补充的“之前 UID 临时记录”约定，没有另建公司登录或本地账号权限系统。已发布 init 和 0002/0003 未改写。完整性、正确性和设计一致性未发现此次改动的未实现项；全量平台回归与 MySQL/真实模型边界如上保留。
