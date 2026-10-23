# Nexus v1.4.8 交互问题修复与验证

- 日期：2026-10-23（Asia/Shanghai）。
- 基线：`v1.4.8 / c3bd9c81e8d3ecba3a76cf0f16eea4448d9ad08d`。
- 修复分支：`codex/nexus-interaction-fixes`，隔离 worktree。
- 对照：原始 `nexus-interaction-test-20261023` 三份报告及证据保持原样。
- 修复栈：API **18420**、AgentScope **2.0.10 / 18400**、Task MCP **18402**，使用复制的 SQLite、独立 Redis DB 6，复用原隔离 Gateway **18310**。未改主开发库和既有 12 名员工；Gateway 复用会增加原隔离网关的请求记录。
- MySQL：本机隔离实例 **8.0.36 / 18243**，测试创建并删除自己的临时库，未使用公司数据库。

> 后续验证（2026-10-23）：真实 Worker 与经验发布/复用已继续执行，详见 [真实 Worker 闭环报告](../nexus-worker-experience-20261023/VERIFICATION.md)。本文下方保留当时的验证范围和历史结果。

## 一、结论

TC05、TC10、TC11、TC14、TC16 的关键失败路径已在修复栈重新验证；同时补了真实页面拒绝草稿、工具失败摘要、同事来源标签、带 SSE 的群内任务创建、游标重放和身份隔离验证。这里不是重新宣称全部 16 场景通过：原有 PASS 场景保留历史结论，代码相关行为另有自动化回归。

本轮没有新增审批门、多私聊、Agent DAG 或 Nexus 对话存储。AgentScope 仍是消息和记忆事实源。并发私聊采用**明确拒绝**，不承诺排队。交接回复通过原生历史读取，并在 A 下一轮加入带来源的上下文；没有实现 B 完成后自动唤醒 A。

## 二、逐项复核

| 缺陷 | 本轮结论 | 改动或证据 |
| --- | --- | --- |
| D01 岗位联系失败 | 已修复并真实复测 | 支持精确姓名、ID、唯一岗位；多义岗位返回候选而不猜选。TC05 直接用岗位成功投递。没有另建静态名册。 |
| D02 联系同事无确认门 | 按当前契约不是实现违约 | `chat-native-transport` 明确保留 dont_ask；本轮保持既有权限，不把提示词服从当审批机制。 |
| D03 DSML | 已增加接收侧防护；上游根因未闭环 | 对作为正文返回的原始 DSML 标记 `NEXUS_UPSTREAM_TOOL_PARSE_ERROR`，不当作可用同事方案。使用原始协议形态回归；本轮真实联系没有复现 DSML，不能断言上游适配已经修好。 |
| D04 同事回复不可见 | 已修复并真实复测 | 原生历史投影同事真实答复，显示来源标签；A 下一轮收到 assistant Msg 内的 HintBlock。2.0.10 不接受 user Msg 中的 HintBlock，已按真实接口验证。 |
| D05 并发输入 | 已修复并真实复测 | 忙碌返回 HTTP 409；5 并发为 1 次成功、4 次明确拒绝。页面失败时保留文字/附件、移除假成功气泡并显示错误，可重试；不增加队列。 |
| D06 引用中的 @ | 已修复并真实复测 | 跳过 fenced code、行内代码、引号和块引用；选人和 followup 都使用过滤后的正文。日志场景只有 A 发言。 |
| D07 群预算 | 契约调整后已复测 | `tokens` 保留 input+output 实际消耗；生成限额只累计 output，保留时间/轮次/单次输出上限。三人真实讨论 A/B/C 均发言，state=completed。 |
| D08 SQLite 锁 | 已定位根因并修复 | 同步等待写锁会阻塞异步 Task 事务提交。群调度写锁在线程等待；发送/打断走同步路由线程池，SSE 心跳也不在事件循环同步等锁。两条真实 SQLite 事务反例由失败转通过；MySQL 两项并发契约通过。 |
| D09 工具状态 | 失败摘要与短思考文案均已修复 | 原库 6 条 error tool_result、131 条 thinking 全有 finished_at。真实页面进一步复现：已完成但不足 1 秒的 thinking 因耗时被格式化为 0s，仍显示“思考中”；已改为根据真实运行状态选择文案。失败 Write 不再计成编辑。没有猜写结束时间。 |
| D10 不存在工具 | 旧错误有证据；本轮未新增 | 真实新增运行中没有 ToolNotFound 错误，工具集合仍按权限约束。不扩张 Bash/Write 权限，也不保证模型永远不会请求不存在的工具。 |
| D11 分类查询 | 已修复并真实复测 | 新增 `nexus_task_categories`，只返回已启用分类，复用原 Task MCP 鉴权。员工真实查询到 testing-platform。 |
| D12 标题前缀被当类型 | 已修复并真实复测 | 工具 schema 限定 ISSUE/REQUIREMENT；技能区分标题前缀和类型。带 E2E-TEST 前缀的需求已真实创建，数据库为 REQUIREMENT。 |
| D13 多私聊 | NOT_IMPLEMENTED，保留产品限制 | 相同员工与人类仍复用唯一私聊；已有回归核实，不移除已发布唯一索引或擅加多会话产品。 |
| D14 participants 不一致 | 已修复 | followup 追加员工时在渠道锁内合并并持久化参与者；实际发言从同一消息事实源读取。 |
| D15 SSE 缺来源 | 已修复并真实复测 | delta 携带 discussion_id、employee_id、seq；真实 after 游标重放顺序正确且来源完整。 |
| D16 mailbox MCP 403 | 已修复并真实复测 | 邮箱同步/交接时移除无可信发起者的 Task MCP。真实 workspace/mcp 返回 200，邮箱没有 nexus-task；普通私聊/群继续校验用户和岗位权限。 |
| D17 Skill 噪声 | 收紧技能适用说明；不增加平台意图编排 | 技能 frontmatter 与正文明确普通设计/职责问答不加载。核心真实复测新增调用为 contact_employee、分类查询、任务创建，没有 Skill 调用；这是本轮观察，不是模型行为保证。 |

额外修复：邮箱回复原先按请求正文匹配；本轮改为**投递 ID = HintBlock ID**，相同正文不会选到另一条交接答复。未完成的助手文本不会被投影为已完成回复。没有标识的历史交接无法可靠关联，不按正文或时间猜测回填；旧记录重发可能受原有去重与联系次数限制，需要人工核实后一次性处理。公司空库部署不需要导入这些测试记录。

## 三、验证证据

### 自动化

结果见 `evidence/validation-results.json` 和完整后端输出 `evidence/backend-tests.txt`。确定性测试只在外部 AgentScope 协议边界使用仓库已有替身，SQLite/MySQL 事务、Nexus HTTP/鉴权及渲染逻辑是真实代码；这些结果与下述无 Mock 的真实推理/浏览器验证分开计数。

- `pytest tests/platform tests/nexus`：**1283 passed、32 skipped**，无失败。跳过项主要要求另行提供的真实环境/凭据；不将其计为通过。
- 真实 MySQL 8.0.36：一次性邀请并发、事件游标提交顺序两项。
- `ruff check app nexus_api tests`。
- chat-island：17 项 Node 测试、类型检查、构建。
- web：类型检查、构建。chat-island 先构建，再构建 web，确认 public/dist 聊天资源一致。
- OpenSpec：strict 校验与按 Scenario 核实，详见对应 change 的 `verification.md`。

文本日志仅清理行尾空白。Vite 压缩包内的 HTML 实体模板包含有语义的行尾 TAB，`.gitattributes` 只对生成包关闭该空白检查，避免裁剪字符串改变运行语义；源码仍严格检查。

`evidence/counterexamples.txt` 保留旧代码失败的反例输出，覆盖岗位/多义解析、引用、预算、交接投影、忙碌拒绝、分类/枚举、工具摘要、SQLite 锁、回复关联和未完成答复。预算和拒绝状态属于明确记录后的契约调整，不把新断言冒充旧 Spec 的要求。

### 真实推理、接口与页面（无 Mock）

| 路径 | 结果 | 证据 |
| --- | --- | --- |
| TC05 按岗位联系 → B 答复 → A 引用 | PASS | `tc05.json`、`peer-ui.png` |
| TC10 5 条并发 | PASS（拒绝模式） | `tc10.json`，状态 200/409/409/409/409 |
| 页面拒绝后草稿保留 | PASS | `browser-summary.json`、`rejected-draft-ui.png`；真实后端 409，未拦截请求 |
| TC11 分类发现 | PASS | `tc11.json` |
| E2E-TEST 前缀的需求创建 | PASS（只创建） | `tc11-create.json`，`T-20261023-ACB350`，REQUIREMENT/PENDING/worker=null |
| TC14 日志引用不唤醒 B | PASS | `tc14.json` |
| TC16 A/B/C 各发言 | PASS | `tc16.json`，三名实际说话者、completed |
| 页面持续 SSE + 群内 Task MCP 创建 | PASS（只创建） | `group-task-sse.json`、`group-task-sse-ui.png` |
| SSE 游标重放与来源 | PASS | `sse-replay.json` |
| 身份/非成员探针 | PASS | `tc15-authorization.json`，伪造 UID 私聊 404、非成员群聊 403 |
| 工具失败摘要与已完成短思考文案 | PASS | `tool-failure-ui.png`、`browser-summary.json` |
| 邮箱不挂 Task MCP | PASS | `mailbox-mcp.json` |

页面草稿测试在专用复制库中临时禁用 C 来制造真实 409，并在 finally 恢复；没有修改主库或使用 Mock。这证明发送拒绝后的草稿恢复，忙碌 409 本身另由真实并发接口验证。

## 四、未验证与风险

1. **Worker 执行未验收**：隔离库只有 offline Worker；新建任务仍为 PENDING、未分配 Worker。本机 OpenCode CLI 为 1.19.3，Worker 契约为 1.18.35，未绕过版本校验冒充正式 Worker 执行。
2. **真实第三方 MCP 未验收**：现有目录绑定的是 E2E任务MCP，本轮验证的是内置 Task MCP，不能代替公司第三方 MCP 的工具调用和权限验收。
3. **DSML 上游根因仍未闭环**：只完成坏回复防护；没有足够的原始上游响应证明问题出在厂商、Gateway 或 AgentScope 哪一段。
4. **公司 MySQL/Linux 部署未验收**：本地 8.0.36 的两项并发验证不等于生产部署或整条 Worker 链路。
5. **交接时序边界**：本轮验证刷新读取及下一轮引用，没有 B 完成后自动推送/自动唤醒 A；多方同时向同一邮箱委托、嵌套转发与重启窗口未作全面 E2E 验收。
6. **预算变化**：输入仍计入总消耗，但不再重复消耗生成限额；长上下文讨论的实际费用可能高于旧策略，时间/轮次/每轮输出限制继续生效。
7. 构建保留大 chunk 警告；测试环境有既有 Python/pytest-asyncio 弃用警告。本轮不顺带改依赖。

## 五、代码与数据边界

未修改已发布 init、未新增迁移、未推送或部署。所有修复和新证据保存在隔离工作区。`evidence/environment.json` 记录进程 PID/create_time/cwd、代码 SHA-256 和环境限制，不包含密钥。
