# 同一数字员工多任务并发与延迟人工介入补测

日期：2026-10-23。业务代码基线：`6b84b1727978638b57b0b2069faefba3a9a686ab`，分支 `codex/nexus-interaction-fixes`。本轮只增加测试报告和证据，**未修改业务代码，未使用 Mock**。

## 结论

同一数字员工可以同时负责多条未结束任务。任务等待人工不会阻塞员工普通聊天，也没有阻止其他 Worker 推进。回答各自工作区的原生问题后，仅对应任务沿用原 Session 继续；共用 Worker 的下一项任务需要等上一项确认完成后才能执行。

但是，**在员工聊天中给出明确的任务答案，目前不能把答案提交给 Worker 的原生 question**。员工尝试 `contact_employee(Worker ID)` 失败，任务仍然等待。这是本轮确认的能力缺口，不能将“聊天中说了答案”视为人工介入已完成。

收尾还实际遇到模型请求 400 与后续网关熔断 503，人工重新发送后恢复。错误卡可见，已接受的用户消息保留，任务状态没有损坏；这次失败仍计 FAIL，不能用恢复成功抹去失败。

13 项观察：**11 PASS、1 NOT_IMPLEMENTED、1 FAIL**。其中模型是否澄清、是否自作决定属于本轮模型表现，不等于平台有强制确认门。

## 环境与任务

- 同一责任员工 `emp_90d1baf5766d`，同一私聊 `conv_d16484f610bc`，同一 AgentScope Session `1b6aa4388c4e446597e0fe79e6a68845`。下文 A/B/C/D 是任务标签。
- API 18520、AgentScope **2.0.10 / 18500**、Task MCP 18502、Redis 18579；独立 Nexus 与 AgentScope SQLite 数据。Worker/后台复盘经过本轮网关 18511。**复制的 AgentScope credential 仍指向历史 QA 网关 18310/v1**；并非所有模型调用都经过 18511。
- Lima Ubuntu arm64，正式 `ASTRA_EXECUTOR_TYPE=worker`，OpenCode **1.18.35**。复用独立 QA environment 5，正式部署新增 environment 6、7，各自独立 systemd、端口、workspace。没有启用旧复制 Worker，没有改原开发数据库或既有员工。
- 三条任务的未完成 Session 生命周期重叠；业务 `RUNNING` 区间有两两重叠，**没有三条同时保持 RUNNING 的区间**。本轮覆盖执行、等待与通知交叠，未做三路持续推理压测。

| 任务 | 实际 ID | Worker 环境 | 内容 | 最终状态 |
|---|---|---:|---|---|
| A | T-20261023-36B2C6 | 5 | 原生问题 ALPHA/OMEGA，等回答后写 decision-A.txt | SUCCEEDED |
| B | T-20261023-32FE23 | 6 | 原生问题 BETA/DELTA，等回答后写 decision-B.txt | SUCCEEDED |
| C | T-20261023-243DF2 | 7 | 独立写 proof-C.txt 并实际读取校验 | SUCCEEDED |
| D | T-20261023-7D1053 | 5 | A 占用期间创建，排队后写 proof-D.txt | SUCCEEDED |

部署操作 ID、进程身份见 [environment.json](evidence/environment.json)；执行与确认时间线见 [timeline-and-reviews.json](evidence/timeline-and-reviews.json)。其中数据库时间为 UTC，聊天和截图显示北京时间。

## 场景与实际结果

| 编号 | 操作和判据 | 实际结果 | 结论 |
|---|---|---|---|
| CT01 | 同一员工同一私聊创建三项任务，三个 Worker 分别执行 | 三项均真实创建、领取，各有独立 worktree 和 OpenCode Session；A/B 等人时 C 已执行完 | PASS |
| CT02 | 三项均进入等待后，不发送人工答案或确认，保留观察至少 120 秒 | 两个快照间 **132.69 秒**；A/B 原问题仍存在，C 仍待确认，三个 Worker 各占用 1；没有自动代答或确认 | PASS |
| CT03 | 三项任务先后通知同一员工，核对投递和实际处理 | 初始三条 wake 均 delivered；A/B 提示合并在一次员工推理，C 后续处理；员工均实际调用 task_get，区分等答案与等验收 | PASS |
| CT04 | A/B 等人时，向员工发送普通 Python 问题 | 接受并正常回答；三个任务未因聊天被继续、取消或确认 | PASS |
| CT05 | 员工回复中再次发消息，检查是否排队或丢失 | 第一条 200；直接 API 第二条明确 409；聊天岛保留草稿，运行时按 Enter 没有第二次提交，回复后草稿仍在；**不会自动排队发送** | PASS（现有拒绝契约） |
| CT06 | 只说“好的，继续吧。” | 员工澄清指哪项，没有挑一项执行；A/B/C 绑定和状态未变 | PASS（模型表现） |
| CT07 | 聊天中点名任务 A，明确给 ALPHA，要求转给 Worker 并沿用原 Session | 员工查询任务后尝试 contact_employee(Worker ID)，返回“没有找到可联系的员工”；没有答案提交工具，原 question 仍在，A 没有恢复 | NOT_IMPLEMENTED |
| CT08 | 经 B 的任务代理提交 A 的 question ID | 403：“请求不属于此任务 Session”；没有回答 A 或 B | PASS |
| CT09 | 真实浏览器先回答 B=DELTA，再回答 A=ALPHA | B 恢复时 A 问题仍在；两项均沿用原 Session，分别写 DELTA/ALPHA；SSH 独立读取一致，没有对方的输出文件 | PASS |
| CT10 | A 等答案期间创建同 Worker 的 D；A 回答完但未确认时继续观察 | D 持续 PENDING、没有 Session；Worker 没有因原生执行变 idle 就提前接下一项 | PASS |
| CT11 | 核验 A 后，在员工聊天中只授权确认 A | 员工只确认 A；D 随原调度自动领取执行，B/C 保持待确认；D 文件实际校验正确 | PASS |
| CT12 | 核验 B/C/D 后逐项在真实页面确认；检查占用与后台复盘 | 四项全部 SUCCEEDED；三个 Worker IDLE、占用 0；十条 wake delivered；每任务一个复盘 Job，四个 NO_FINDINGS，0 候选 | PASS |
| CT13 | 收尾通知后再次发送人类只读查询，并检查错误与恢复 | 先发生模型 invalid_request 400；下一条已接受的人类消息遇到熔断 503，显示“回复失败”；人工再次发送后 13.63 秒正常答复，任务状态不变 | FAIL，重试恢复 |

状态、问题、投递、Worker 快照见 [state-snapshots.json](evidence/state-snapshots.json)。[16 项证据一致性断言](evidence/independent-assertions.json)全部通过；这些断言验证上述事实，**不表示 CT07/CT13 已通过**。

关键证据：

- [员工完整本轮对话](evidence/employee-dialogue.json)、[聊天转答失败](evidence/08-explicit-answer-in-chat.json)、[模糊回复](evidence/07-ambiguous-continue.json)。
- [忙碌时 API 与草稿](evidence/06-chat-busy-ui.json)、[草稿截图](evidence/06-chat-busy-draft.png)。
- [A 的原生问题](evidence/04-waiting-A.png)、[B 的原生问题](evidence/04-waiting-B.png)、[跨任务拒绝](evidence/10-cross-task-answer-rejected.json)。
- [A 实际提交](evidence/11-answered-A.json)、[B 实际提交](evidence/11-answered-B.json)、[原生执行轨迹](evidence/native-execution.json)、[SSH 独立核验](evidence/16-independent-with-D.json)。
- [页面确认结果](evidence/18-confirmed-ui.json)、[D 确认后页面](evidence/18-confirmed-D.png)。

## 原因与尚未解决的缺口

### H1：聊天中的人工答案无法到达 Worker 原生问题（建议 P1）

`app/task_mcp.py` 提供 start/resume/intervene/confirm 等控制，却没有任务原生问题的查询和回答工具。`nexus_task_get` 只有“请在 Workspace 回答问题或权限请求”这类状态描述。员工工具 `contact_employee` 的目标是员工 Agent Session，不能把 Worker ID 变成 OpenCode question 的回答目标。

现有任务工作区代理已经校验 Task → 当前 Session → question 归属，真实回答可以正常继续。所以断点在员工聊天到这条既有能力的接入。后续实现应复用现有绑定和代理，携带可信的人类身份、明确问题 ID 与答案；不另建任务状态机，也不能让员工自行替人选择答案。**本轮未实施此变更。**

### H2：责任员工缺少核验交付与人工操作的可读证据（建议 P2）

`TaskTools.result()` 只给进度、路径、Session 和结束时间；员工读不到实际交付内容、验证命令输出、原生问题的实际回答，也无法直接查询 Worker 占用。本轮员工多次明确表示不能验收，只能让人去工作区查看。A 的确认依靠人先独立核验并授权，B/C/D 由测试人通过页面确认；不能把员工复述状态当作独立验收。

后续需要从已有执行事实源提供有边界的结果与人工操作记录读取能力。当前工作区实际证据和 SSH 核验均存在，不能由“员工工具看不到”推断平台没有这些执行事实。

### H3：模型协议异常与网关熔断连带影响后续聊天（P1 待定位）

真实日志记录上游 400：`The reasoning_content in the thinking mode must be passed back to the API.` 原文中的字段由反引号包围；完整错误与 request ID 保存在 [模型集成失败证据](evidence/23-model-integration-failure.json)。随后历史 QA 网关 18310 的请求日志连续出现 503 / `NO_ELIGIBLE_NODE`，AgentScope 记录 CIRCUIT_OPEN，当前人类消息以 error/upstream 结束。

真实聊天岛显示两张错误卡，用户消息保留；[错误与恢复截图](evidence/22-model-error-ui.png)中也能看到人工重试后正常回复。后台通知已经逐项查询到四项完成，任务与 Worker 占用没有因聊天失败回退。

已确认请求被上游拒绝及随后的熔断，**尚未确认 reasoning_content 在哪一层丢失、是否由通知交叠触发**。熔断发生在历史 QA 网关 18310，不冒充已在 18511 复现；本轮没有重启或改动历史网关。当前网关源码对流式失败也会记录节点失败，仍需在独立网关补做非法输入与有效请求的对照后判断修复范围。此项没有宣称根因闭环或修复完成。

## 最终状态与验证边界

- 四个真实任务均结束；A/B 回答前后 Session 未换；D 使用新的独立任务 Session；三个 Worker 最终空闲。
- 四个复盘 Job 使用 prompt_version=2，均为 NO_FINDINGS，attempts=0，无错误、无新候选。简单标记写入没有被强行总结成“三条经验”，本轮未发布知识。
- 无人回复期间只观察了约 133 秒的稳定窗口；源码的执行超时路径排除存在原生待答问题的情况。本轮未验证数小时/隔夜等待、重启恢复、断网、原生 Permission 授权或大规模并发。窗口内没有催办或重复 wake，不能据此声称有自动催办机制。
- 普通消息忙碌时的明确拒绝与保留草稿属于现有行为；任务通知则使用 AgentScope 原生 inbox/wakeup。两者不能混称为统一消息队列。
- 本轮没有跑全套单元测试或生产 MySQL 全链；实际 HTTP、真实模型、三个 Linux Worker、浏览器、原生问题、独立文件核验与数据库事实是本轮证据。没有把此前的 1311 PASS 重计成本轮结果。
- 只新增本目录报告与脱敏证据，原业务代码没有变更。JSON 已对当前运行凭据和原始 runtime_context 前缀扫描通过，选用截图已人工检查。隔离栈与 QA Worker 保留，便于复核。
