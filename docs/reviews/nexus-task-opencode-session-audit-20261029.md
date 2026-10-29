# Nexus v1.4.9 Task / OpenCode Session 状态定向排查

日期：2026-10-29。代码基线：本地 `v1.4.9`，`46bd6a629337dd75b64dc0c030376b274d23d568`。

本次只检查 Task 生命周期、Task MCP、责任员工求助、相关 API 和任务页面。未修改业务代码、状态契约或数据库，未操作正在运行的业务任务。
本地比已有 `origin/v1.4.9` 跟踪引用落后两条提交；没有 fetch 或合并。对这两条提交的差异检查显示，它们涉及员工形象和界面，不修改本报告的 Runtime / Task MCP 核心链路。工作区原有及并发出现的未跟踪文件均保留。

临近交付时还发现其他工作并发修改了 `app/runtime/dispatcher.py`、`lifecycle.py`、`maintenance.py`、`session_state.py`。本报告的代码行号和状态表以开头记录的基线及首次读取内容为准；这些并发改动涉及 UNKNOWN、超时停止核对和调度占用，未由本轮修改或纳入验收。已查看的差异没有改变本文的正常结束投影、OpenCode MCP 续答限制、MCP resume 与 confirm 入口结论。下述测试是本次执行时工作区的结果，不代表这批后续并发修改已通过验证。

## 1. 结论与证据等级

**当前代码没有“OpenCode `stop/end_turn` 自动把 Task 标成已完成”的映射。** 正常结果为 Session `SUCCEEDED`、Task `WAITING`，等待原因 `EXECUTION_RESULT_REVIEW`。`AUTO` 与 `MANUAL_CONFIRM` 现在都使用此行为。

已经确认的缺口是：

1. **OpenCode 普通回合续答没有接通 Task MCP。** 底层 RuntimeService 和工作区 API 可以复用原 Session，但 MCP 求助查询固定返回 `can_input=false`，输入工具明确拒绝 OpenCode。
2. **MCP 的 resume 实际执行 restart。** 它与 HTTP resume 的含义不一致；在已有 Session 时会走创建新 Session 的实现，不能作为原会话续答替代品。
3. **MCP 可以直接确认业务完成，没有核验人的验收证据。** Session 正常结束只是调用完成确认的技术前提；数字员工调用 confirm 即可将 Task 置为 `SUCCEEDED`。代码中的“人工确认”文案不能证明人确实确认过。
4. **任务发现与表达不足。** MCP get/list 不返回 Session 状态、等待原因和当前执行回合标识；创建幂等只覆盖相同会话与相同内容，不能识别换了描述的同一业务工作。
5. **任务列表存在刷新和显示口径差异。** 详情页有事件刷新，列表页没有持续刷新订阅；列表还自行覆盖状态展示，容易出现旧快照或“本轮已完成”被误解。

尚未确认：现场的任务是否真的进入了 Task `SUCCEEDED`；是否调用过 `nexus_task_confirm` / HTTP confirm；运行中的服务是否来自本次基线；是否只是把 Session 的成功误认为业务成功；重复任务具体由哪次工具调用产生。没有现场任务编号、工具轨迹和部署版本证据，不能把这些路径认定为已发生的事故根因。

## 2. 真实对象与状态关系

概念链路为 Task → 执行轮次 → 责任数字员工 / 执行 Worker → OpenCode Session。**这版执行链路没有独立的 Run ORM / Run 状态机**，不能套用旧 Run 逻辑解释当前行为。

- `Task.responsible_employee_id`：发起并管理任务的数字员工。
- `Task.employee_id`：执行用 AgentWorker，MCP 返回为 `worker_id`，不是上面的责任员工。
- `Task.current_session_id` → `RuntimeSession`：当前运行时绑定；OpenCode 执行事实来自原生 Session。
- `Task.last_message_id`、`turn_started_at`、`pending_message_id`：当前输入轮次及受理事实。它们不是新的业务状态机。
- `RuntimeSession.status=SUCCEEDED`：当前轮正常结束，不表示 Session 被销毁或禁止再次输入。

主要代码：`services/nexus-api/app/models.py:193`、`:234`、`:259`；`app/runtime/lifecycle.py:243`；`app/task_tools.py:630`。

| 原生事实 / 操作 | RuntimeSession | Task | 说明 |
| --- | --- | --- | --- |
| 提交已受理；原生 busy / retry | RUNNING | RUNNING | 新轮复用当前绑定；清理上轮等待原因 |
| 当前 Session 或子会话有 question / permission | WAITING | WAITING | `HITL_QUESTION` / `HITL_PERMISSION`，优先于 busy |
| 当前轮 assistant 有完成时间、`finish=stop/end_turn`，且有可见文本回复 | SUCCEEDED | WAITING | `EXECUTION_RESULT_REVIEW`，不自动验收 |
| assistant 原生 error | FAILED | WAITING | 保留绑定与错误，可恢复；不是 Task FAILED |
| idle 超过宽限期且当前轮无 assistant 回复，或完成回复无可见文本 | FAILED | WAITING | `no_reply`，保留恢复入口 |
| idle 后 assistant 结果不完整 | INTERRUPTED | WAITING | `incomplete`，不猜业务成功 |
| 当前轮超时 / 仅停止当前回复 | INTERRUPTED | WAITING | `TIMEOUT` / `MANUAL_INTERVENTION` |
| 绑定 Session 缺失 / 连接不可达 | UNKNOWN 或保留已有终轮事实 | 原 Task 状态保留 | 写连接异常 / 恢复原因，不据网络故障决定任务成败 |
| 输入尚未确认 | 保留待对账事实；已进入原生判定时视为 RUNNING | 保留 / 进入 RUNNING | `pending_message_id` 防止把旧回复判作新轮成功 |
| 显式 confirm，通过 Session 空闲、无 HITL、无未确认输入等检查 | SUCCEEDED | SUCCEEDED | 写 `confirmed_at` / `completed_at`；此后普通输入被拒绝 |
| 显式终止整个任务 | 中断 / 停止核对 | CANCELED | Worker 释放还受停止确认、输入受理与绑定一致性约束 |

`TaskStatus.FAILED` 枚举和通用 TaskService 转移能力仍存在，但当前 OpenCode Session 失败、无回复、超时等投影统一落在 Task `WAITING`，没有发现本链路自动写 Task `FAILED` 的入口。初始化示例数据不属于运行时映射。

事实归一：`app/runtime/session_state.py:95`（判定顺序）、`:169`（正常轮结束）；业务投影：`app/runtime/lifecycle.py:371`—`:466`。错误、超时和断线对账：`:1699`、`:1826`—`:1918`。

任务等待期间仍占用执行 Worker，员工状态投影为 `WAIT_HUMAN`；业务完成后才释放，避免阶段等待时把 Worker 分给别的任务。见 `app/runtime/occupancy.py:34`、`:107`。

## 3. AUTO / MANUAL_CONFIRM 的历史与现状

| 变更 | 当时行为与影响 |
| --- | --- |
| `f2a5e2eab`，2026-09-20，`separate Session completion from Task business success` | 旧 `backend/app/runtime/lifecycle.py` 中，Session 成功时 MANUAL_CONFIRM → Task WAITING，AUTO → Task SUCCEEDED；确认入口限定 MANUAL_CONFIRM |
| `e9189ab59`，2026-09-30，`keep employee tasks open until human completion` | 删除完成策略分支和等待验收期间禁止普通输入的拦截；默认改为 MANUAL_CONFIRM；统一显式结束业务任务。当时正常轮结束仍显示 Task RUNNING |
| `8a91bb524`，2026-10-17，`project finished OpenCode rounds to task waiting` | 把正常执行轮结束收敛为 Task WAITING，等待确认 |
| `51c0950b0`，2026-10-18（北京时间），`record waiting reasons and resume the same session` | 引入现有等待原因及保留 Worker、worktree、Session 的 continue 路径 |

当前 `app/schemas.py:270` 明示字段只为存量接口保留，默认 MANUAL_CONFIRM。`app/services.py:62` 仍保存字段，模型和前端类型也保留 AUTO / MANUAL_CONFIRM，但 Runtime 不再读取它决定完成。

因此，历史 AUTO 自动完成确实存在，**不能据此判断当前 v1.4.9 仍然如此**。切换 completion_policy 不能修复当前 MCP 续答缺口，也不应恢复旧自动完成分支。

历史 OpenSpec `archive/2026-10-13-retired-astra-simplify-employee-task-flow/` 只用于解释设计沿革；不把已退役 Astra 契约当作新的 Nexus 实施对象。现有 Nexus 求助 change `nexus-employee-worker-assistance` 明确支持 DSH 回合续答，OpenCode Scenario 只要求读取并汇报执行结果，反映了当前能力边界。

## 4. 会话续答：能力在底层存在，MCP 没有接通

### 4.1 可用路径

`RuntimeService.send()`（`app/runtime/lifecycle.py:915`）接受当前绑定 Session 的新输入，不因 Task WAITING 或 Session SUCCEEDED 拒绝。它执行待受理输入、终态、取消、HITL、Worker 占用和绑定一致性检查，并向原 `/session/{id}/prompt_async` 发消息，受理后回到 RUNNING。

工作区代理 `app/api/workspace_routes.py:1041` 已复用该方法。HTTP `POST /api/tasks/{id}/resume` 在有绑定时调用 `continue_task()`（`:686`）；但 `continue_task()` 本身只记录继续操作，**不接收或发送补充指令，也不凭空把 Task 改为 RUNNING**。真正续跑仍需要 send。

### 4.2 MCP 的确定性阻断

- `app/runtime/assistance.py:90`：OpenCode 分支无条件 `can_input = False`。
- `app/runtime/assistance.py:176`—`:179`：`input()` 要求 `runtime_engine == dsh`，否则返回“仅支持按当前 DSH 回合续答”。
- `app/task_mcp.py:259`：`nexus_task_input` 的描述也仅限 DSH。
- `services/agent-service/skills/task-management/SKILL.md:48`：责任员工指引只说明 DSH 正常回合续答；OpenCode 按原生 question / permission 回复。

这不是已有 Asking / Permission 转发失败：后者有 request_id，可走 `nexus_task_reply`。题述正常轮结束后没有原生请求，仅有人的新指令，所以该入口不能代替普通 prompt。

### 4.3 resume 命名与实现不一致

`app/task_tools.py:344`—`:351` 对 WAITING 任务执行：

```python
restart = bool(task.current_session_id)
RuntimeService(self.session).start(task.id, restart=restart)
```

已有绑定就传 `restart=True`。`RuntimeService.start()` 的新 Session 创建在 `app/runtime/lifecycle.py:763`，随后替换当前绑定并 supersede 旧绑定。这保留 Task 编号，但丢失“继续原 Session”的语义。MCP 工具却只称“从等待状态恢复任务”（`app/task_mcp.py:207`）。

## 5. 为什么仍可能出现业务已完成与重复 Task

### 5.1 已确认：可由数字员工直接关单

`nexus_task_confirm` → `TaskTools.confirm()`（`:535`）→ `RuntimeService.confirm_completion()`（`:1954`）→ `_finish_confirmation()`（`:2007`）→ Task SUCCEEDED（`:2073`）。

这些检查证明“当前执行轮可结束”，不证明“代码修改、人工检视、Commit 等完整业务目标已验收”。没有人的确认消息引用或同等验收事实校验；MCP 说明也没有强调区分阶段通过与整个任务完成。

因此，员工如果把“本轮完成”误当成“整体完成”并调用 confirm，后端确实会关单。临时探针验证了这一调用路径。但**没有现场工具调用记录，不能断言员工已经这样调用过**。

关单后的阻断是明确且一致的：`RuntimeService.send()` 的 `_task_result_concluded()` 检查拒绝；`WorkerAssistance.context()` 只接受 RUNNING / WAITING；工作区代理 `workspace_routes.py:986` 返回 409；详情页 `TaskDetailView.vue:66` 对业务终态设为只读。不能通过放宽终态写入检查来修复误关单。

### 5.2 已确认：幂等边界不能覆盖业务续答

`app/task_tools.py:647` 的派生 key 包括来源 Session、类型、分类、标题、说明和仓库。`app/services.py:37` 按相同 key 返回已有 Task，并处理并发唯一键冲突。

同一会话、相同内容重试能去重。但“修改代码”与“检视通过，请 Commit”的描述不同，会生成不同 key；不同来源会话也不相同。没有按语义识别同一工作或自动沿用旧 Task 的机制。`nexus_task_update` 只是修改说明，不会把说明发送给 OpenCode。

结合 OpenCode input 不可用、resume 会换 Session、confirm 后原 Task 拒写，**重新 create 是可能的模型行为，非平台自动创建的必然结果**。目前没有代码证据表明后端会因续答自行复制 Task。

## 6. 后端、API 与前端一致性

- 后端 Task 中文状态源为 `app/runtime/presentation.py:53` 与 `task_status.json`；Task WAITING 为“等待人工或外部条件”，SUCCEEDED 为“已完成”。
- HTTP get/list 在 `app/api/routes.py:651` 统一附加 `session_status` 和 `session_terminal_reason`，没有把 Session SUCCEEDED 写成 Task SUCCEEDED。普通 get 读取已持久化投影，不同步等待一次远程对账。
- 后台 `app/runtime/recovery.py:43` 用原生 SSE 与有界对账更新 Task，正常轮结束仍监视 WAITING；业务终态停止监视。Task 转移会发事件，等待原因变化也有再次唤醒。
- MCP `TaskTools.get()` 同样只读库，`_brief()`（`:630`）缺少当前执行 Session、Session 状态、等待原因和续答能力字段。员工必须进一步查询 assistance，但该工具在 OpenCode 路径又禁止输入。
- 详情页 `TaskDetailView.vue:296` 区分“本轮已完成，待确认”和“已确认完成”，确认按钮要求 Task WAITING + Session SUCCEEDED（`:307`）；没有发现按原生回复结束事件自动调用 confirm 的逻辑。
- 详情页通过 `/api/events/stream` 订阅 `TASK_` / `RUNTIME_`，连接成功重新读取（`:258`—`:276`）；收到工作区 ready 也刷新。`loadTask()` 同时读取 Task 和 workspace（`:423`），并非同一事务快照，事件刷新后会再收敛；未做浏览器并发场景实测。
- 列表页 `TaskListView.vue:255` 自行覆盖显示：Task WAITING + Session SUCCEEDED 展示“本轮已完成”；连接异常甚至把观察对象的 `runtime_state` 改为 WAITING。后者不改数据库状态，但存在展示事实混用。`App.vue:88` 的 15 秒定时器只刷新 AgentScope 连通性，**不刷新任务列表**。列表目前依赖进入页面、用户刷新和页面操作后刷新，长时间停留可显示旧快照。

没有发现“前端把 WAITING 自动提交为 SUCCEEDED”的证据。界面存在显示和刷新缺口，但它本身不能解释数据库真的被关单。

## 7. 最小整改方案（未实施）

不新增业务状态、不恢复 Run 状态机、不读取模型回复文本猜完成、不新增一套调度器。

1. **把现有 `nexus_task_input` 扩展到 OpenCode。** 在 WorkerAssistance 保留现有责任员工、来源对话、Session、当前回合和停止 / pending 检查；OpenCode 根据原生 idle、无 question / permission、当前轮已正常结束判定 can_input。输入统一调用现有 `RuntimeService.send()`。把 `expected_submission_id` 对当前 anchor 的比较纳入 send 的短事务预留检查，避免查询结束到发送之间已进入新轮时仍接受旧续答。不能只在 assistance 查询时检查一次。
2. **收敛 resume 语义。** 已绑定任务调用 `continue_task()`，未绑定任务才 start；resume 只恢复操作入口，实际补充文本走 input。显式重开仍走已有 restart，不能让 resume 隐式换 Session。保留终态拒写和 Worker 占用规则。
3. **完善现有 MCP 返回和责任员工指引。** get/list 返回已有 Task / RuntimeSession 事实，如 current_session_id、session_status、waiting_reason；可输入能力按上述统一判定返回。工具描述与员工 task-management 指引明确：同一工作先查询和续答；阶段检视通过、允许 Commit 是执行输入；只有人明确确认整个业务任务结束时才调用 confirm。create 继续保留现有 key 幂等，不做标题相似度去重，不限制同一对话只能有一个任务。
4. **明确 confirm 的业务边界与审计。** 保留显式业务确认，禁止以 Session 成功自动触发。记录确认来源、责任员工与所依据的来源消息，便于区分人的确认和员工误调用；若产品要求后端强制核验人的确认，需在实施契约中明确可信消息关联和校验规则，单靠新增一个布尔参数或修改提示词不能声称提供此保证。
5. **补齐展示与刷新。** 复用既有事件流，让任务列表在 Task / Runtime 事件及重连后重新加载；Task 状态保留业务含义，连接异常作为连接事实显示。列表采用“本轮结束，等待处理 / 可继续”，详情明确可继续原会话；展示规则收敛到既有 presentation 与共享前端映射，不增加页面各自的新状态推断。
6. **completion_policy 暂不动数据库。** 本次不重新启用 AUTO 分支；在接口文档明确已统一显式完成。字段清理若另做版本收敛再定契约，不修改已发布 init，也不为旧数据加入双套映射。

OpenCode 续答涉及现有 MCP 能力边界和业务契约，实施前应在既有 Nexus 求助 capability 上提出增量 OpenSpec change，以该 change 的 tasks.md 为唯一实施清单；不修改历史 Astra specs。本轮仅交付排查与方案。

建议验收场景：同一 Task 修改代码 → stop/end_turn → WAITING → 人检视通过要求 Commit → input 原 Session → RUNNING → 新轮结束仍 WAITING → 明确整体验收 → SUCCEEDED；全程 Task、Worker、workspace、Session 标识保持不变。还须覆盖 AUTO / MANUAL_CONFIRM、HITL 阻断、来源 / 回合过期、取消 / 重开竞争、ACK 丢失、断线恢复，以及列表和详情事件刷新。

## 8. 验证结果与未验证范围

运行现有定向回归：

```bash
cd services/nexus-api
.venv/bin/pytest -q tests/platform/test_session_state.py \
  tests/platform/test_waiting_reason.py \
  tests/platform/test_acceptance_review.py \
  tests/platform/test_worker_assistance.py \
  tests/platform/test_session_workspace.py
```

**206 通过，1 失败，34.16 秒。** 失败在 `test_acceptance_review.py:80`：旧断言要求文案包含“回答问题”，当前真实文案为“等待数字员工处理问题或权限请求”。该用例尚未执行后续验收唤醒断言，不能把它计为通过。本轮没有修改测试以掩盖失败。

使用临时测试文件、现有 SQLite / 原生 Runtime 替身 fixture 补充 7 个审计探针，**7 通过，1.76 秒**，文件随临时目录删除：

- AUTO / MANUAL_CONFIRM × stop / end_turn 四种组合：Session SUCCEEDED、Task WAITING；MCP can_input=false、input 被 DSH 限制拒绝；直接复用 RuntimeService.send 后原 Session 恢复 RUNNING，RuntimeSession 数量仍为 1。
- 无人的确认凭据、仅调用当前员工 MCP confirm，即可将已结束轮次的 Task 置为 SUCCEEDED。
- MCP resume 路由传给 start 的参数确为 restart=True（截获路由调用；新 Session 创建另有源代码证据，不把该探针称为真实重启 E2E）。
- question 等待转为执行结果等待时，Task 仍 WAITING，产生新的等待结果唤醒，验证旧文案失败用例未执行到的核心行为。

现有通过用例还覆盖原 Session 续答、终态拒写、延迟 ACK、取消竞争、工作区代理及列表 / 详情 API 状态一致性。部分旧测试名称含 `projects_succeeded` / `projects_failed`，应以实际断言为准；当前断言是 Task WAITING。

**未验证**：现场任务的数据库与工具轨迹、正在部署的 commit / 进程、真实 Worker / OpenCode / 公司模型 E2E、MySQL、浏览器实时刷新和并发快照。没有前端改动，因此未运行前端构建。本次不能声称事故已复现或问题已修复。

若继续现场归因，优先取同一 Task 的 `TASK_WAITING`、`TASK_SUCCEEDED`、`confirmed_at`、当前 / 历史绑定和工具调用序列；`TASK_COMPLETION_CONFIRMED` 仅在开启经验复盘时写入，缺少该事件不能单独证明没调用过 confirm。区分 create、resume、input、confirm 的发生顺序后，再确定是否属于误确认、换 Session 或重复创建。


## 9. 实施补充（2026-10-29）

用户明确授权直接修复，禁止使用 OpenSpec 及相关技能；本轮没有使用或生成这些产物。
用户进一步明确：数字员工判断完整任务已完成时，保留 MCP 关单能力，不强制新增人工验收。
因此第 7 节“只有人明确确认整体结束才 confirm”的建议已被此决定替代：
由数字员工依据完整目标与验收要求决定是否调用现有 confirm，不能仅因单轮结束就关单。

已实施：

- OpenCode assistance 返回真实 can_input；Task MCP input 复用 RuntimeService.send 向原 Session 续答。
- send 在原生核查后、输入预留事务中再次校验回合与责任员工 / 来源会话，拒绝过期续答。
- 保留原生 busy、question / permission、未确认输入和业务终态检查，不通过放宽终态写入解决问题。
- MCP resume 保留原 Task / Session，不隐式 restart，也不发送虚构的执行指令。
- get / list 补充当前 Session、Session 状态与等待原因；创建和续答工具描述明确优先继续原任务。
- 员工任务技能明确阶段检视通过后的 Commit 等工作走原任务续答，并保留 confirm 整体关单。
- 修正完成确认审计文案，不再把员工 MCP 的确认自动写成“人工确认”。
- 列表订阅 Task / Runtime 事件并在重连后刷新；连接异常不再伪造 Task WAITING，区分本轮结束与业务完成。
- 修正已有验收唤醒测试的过时文案断言，改断言 HITL_QUESTION 事实。

验证：新增 `tests/platform/test_opencode_task_continuation.py` 的完整多轮链路和竞争测试通过。
定向任务 / 求助 / 验收 / 归属回归 87 项通过；补充任务归属及 DSH Runtime 回归 43 项通过（两组存在归属测试重叠，不相加计总数）。
前端类型检查、构建、41 项测试及本轮文件 Ruff 检查通过。
扩大回归为 232 通过、3 失败，失败集中于 `test_waiting_reason.py`：
一项仍构造 INTERRUPTED/unconfirmed，与并发改动的 UNKNOWN 语义不一致；两项 `_Idle` 替身缺少并发改动现在会读取的 messages_page。
这些并发 Runtime 改动及其测试由其他工作产生，本轮未回滚或掩盖，整体工作区回归不能声称全绿。
未执行真实 Worker / 公司模型 / MySQL / 浏览器 E2E，未重启部署服务，未提交或推送。


## 10. v1.4.9 发布验证

发布基线：最新远端 `origin/v1.4.9` 的 `2edb40ff5`，在独立工作区只带入本轮修复。
共享 `lifecycle.py` 仅提取 send 的回合 / 来源检查及完成确认文案；未带入其他工作的 UNKNOWN、调度、维护和停止核对改动。

- 隔离工作区后端定向回归：278 项全部通过（含 Session 状态、等待原因、MCP 多轮续答、原生人工交互、任务归属、取消 / ACK 竞争及 DSH）。
- Ruff、前端类型检查及构建通过；最新 v1.4.9 前端 43 项测试全部通过。
- 更新主前端构建及其 office / avatars 静态资源，不发布构建时复制产生的无关聊天岛变化。
- 原工作区并发变更保持原样；没有运行 OpenSpec、修改数据库或部署服务。

因此第 9 节的 3 项失败属于混合工作区记录，不是本次隔离发布快照的验证结果。
