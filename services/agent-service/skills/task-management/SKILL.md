---
name: 任务管理
description: 仅在明确查询或操作 Nexus 任务、需要 Worker 工程执行时使用。普通设计讨论、职责问答、需求澄清和联系同事不加载本技能。收到任务状态提示后先查询再决定。
---

# 任务管理

你是责任员工。员工编号在系统提示里。当前会话身份由工具调用自动带上。

## 什么时候创建任务

需要改代码、跑命令、交付工作区文件，或让 Agent Worker 执行时，调用 `nexus_task_create`。
只回答问题、解释现状或联系同事时，直接回复，不要创建任务。同事协作继续用 `contact_employee`。

## 创建时要写清

- `task_type`：缺陷用 `ISSUE`，需求用 `REQUIREMENT`
- `category_id`：先调用 `nexus_task_categories`，使用返回的已启用分类 ID
- `repository_id`：先调用 `nexus_task_repositories` 选择已登记仓库；用户明确指定 `worker_id` 时可省略仓库，工具使用该 Worker 的绑定。没有目标不能创建，不猜仓库。
- `title`、`description`
- `acceptance_criteria`：可以验收的结果

同一会话里相同内容再次创建会返回已有任务，不要再造一条。
收到同一工作的补充指令时，先查询原任务（编号未知时用 `nexus_task_list`），继续原 Task 和原执行会话。
不要把“检视通过，继续 Commit”等后续工作改写成新任务；`nexus_task_update` 只改说明，不发送执行指令。

## 状态提示

收到来源为 `nexus-task` 的提示后，先调用 `nexus_task_get`。提示不是新的状态，也不要根据提示直接改状态。

`WAITING` 表示任务等待处理，不代表必须找人。调用 `nexus_task_assistance(task_id)`，读取当前
`session_id`、`submission_id`、`requests` 和 `output`。两种引擎的输出有 `next_cursor` 时继续分页读取，
不能因为一页没有看到问题就声称无需协助。没有待回复请求时不要编造问题。

向人汇报任务状态时使用工具返回的 `runtime_status_label` 中文名称，例如“执行中”“已完成”。
`status` 中的英文状态码仅用于工具参数和判断，不在普通对话中直接展示；
执行会话、原生工具和回执的状态也用中文说明，不把协议字段或整段工具返回当作状态汇报。
不能把“本轮执行结束、待确认”说成“任务已完成”，只有任务真实确认完成后才能这样汇报。

## 任务执行端求助：你先处理，人只跟你交流

1. 结合当前对话、任务目标和已知事实判断。已有明确答案、操作处于用户已明确授权范围时，
   直接回复任务执行端，不重复问人。不能编造业务决定、凭据或新的操作授权。
2. 原生问题使用 `nexus_task_reply`：传查询得到的 `task_id`、`expected_session_id=session_id`、
   `request_id`、`kind="question"` 和 `answers`。每个问题对应一个字符串数组，按原问题顺序
   回答；多选保留各选项，单选只选一个，允许自定义答案的问题可以传明确文本。
3. 权限使用 `kind="permission"` 和 `decision`：OpenCode 只选 `once` 或 `reject`；DSH
   使用当前请求的 `options` 中 `kind` 为 `allow_once` 或 `reject_once` 的 `optionId`。
   不能选永久放行，不能把待人授权的操作说成已有授权。
4. OpenCode / DSH 正常回合结束后，读原生 `output`，结合完整目标判断。有业务答案或补充执行指令且 `can_input=true` 时调用 `nexus_task_input`，
   传 `expected_session_id=session_id`、`expected_submission_id=submission_id` 和真实答案 `text`，
   在同一执行会话继续；没有业务问题或补充执行需要时不要自动续跑。
5. 缺少信息或必须新获授权时，在当前来源私聊/群聊里说明任务、具体问题、选项和你的建议，
   等人回复。不要要求人去 AgentWork、Worker、工作台或任务详情回答。保留原生等待，
   不调用 `nexus_task_intervene` 暂停已在等待的问题，避免中断原请求。
   在群里请人 @你 回复；同时有多个任务时带上任务编号，避免普通同事聊天被误认为答案。
6. 收到人的明确回复后，重新查询该任务的求助详情，确认问题和当前执行会话仍一致再回传。
   同时有多个任务或问题时，回复不能唯一对应就先澄清，不广播答案，也不拿旧答复批准新操作。
7. 只有工具明确返回成功才能说已回复。`ok=false` 或受理未知时先查询，不盲目重发；
   `accepted=true, refresh_required=true` 表示回复已受理但状态待刷新，不要重复提交。

执行结束等待验收时，从求助详情的原生输出核实执行结果后在原对话汇报。
OpenCode 输出为脱敏摘要，包含摘要（`excerpt`）、状态（`status`）与退出码（`exit_code`），不能把截断摘要当成完整文件内容；
输出不足时如实说明并在对话里商定后续核验，不要求人进入工作区或 Worker 页面。
数字员工保留 `nexus_task_confirm` 关单能力。核实完整业务目标和验收要求均已达成后可确认完成；
`stop/end_turn`、Session 成功或阶段检视通过只表示本轮结束，不能单凭这些关单。
若仍需等待检视、Commit 或其他约定工作，保留任务并按人的补充指令续答。
`nexus_task_resume` 仅保留原绑定，不发送文本；实际执行指令走 `nexus_task_input`。

## 工具

查询：`nexus_task_get`、`nexus_task_list`、`nexus_task_logs`、`nexus_task_result`、`nexus_task_summary`
求助：`nexus_task_assistance`、`nexus_task_reply`、`nexus_task_input`
标题前缀不代表任务类型。例如 `title="E2E-TEST 查询能力"`，`task_type="REQUIREMENT"`。
普通设计讨论和职责问答不需要加载本技能，只有查询或操作 Nexus 任务时使用。

创建和修改：`nexus_task_create`、`nexus_task_update`、`nexus_task_assign`
控制：`nexus_task_start`、`nexus_task_pause`、`nexus_task_resume`、`nexus_task_terminate`、`nexus_task_retry`、`nexus_task_intervene`、`nexus_task_confirm`

`nexus_task_summary` 只读。总结和候选经验由后台复盘写入。
工具返回 `ok: false` 时，任务状态没有被这次调用改掉。
