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

## 状态提示

收到来源为 `nexus-task` 的提示后，先调用 `nexus_task_get`。提示不是新的状态，也不要根据提示直接改状态。

`WAITING` 表示需要人处理。把原因告诉当前对话里的人，不要自己确认完成。

## 工具

查询：`nexus_task_get`、`nexus_task_list`、`nexus_task_logs`、`nexus_task_result`、`nexus_task_summary`
标题前缀不代表任务类型。例如 `title="E2E-TEST 查询能力"`，`task_type="REQUIREMENT"`。
普通设计讨论和职责问答不需要加载本技能，只有查询或操作 Nexus 任务时使用。

创建和修改：`nexus_task_create`、`nexus_task_update`、`nexus_task_assign`
控制：`nexus_task_start`、`nexus_task_pause`、`nexus_task_resume`、`nexus_task_terminate`、`nexus_task_retry`、`nexus_task_intervene`、`nexus_task_confirm`

`nexus_task_summary` 只读。总结和候选经验由后台复盘写入。
工具返回 `ok: false` 时，任务状态没有被这次调用改掉。
