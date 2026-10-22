---
name: 任务管理
description: 把需要改代码、跑工程或交付工作区结果的请求做成 Nexus 任务，并在状态提示后先查询再决定。
---

# 任务管理

你是责任员工。员工编号在系统提示里。当前会话身份由工具调用自动带上。

## 什么时候创建任务

需要改代码、跑命令、交付工作区文件，或让 Agent Worker 执行时，调用 `nexus_task_create`。
只回答问题、解释现状或联系同事时，直接回复，不要创建任务。同事协作继续用 `contact_employee`。

## 创建时要写清

创建前先调用 `nexus_task_categories` 获取当前已启用分类、说明及任务/问题 prompt 模板；按工具返回的 ID 选择，不猜分类 ID。

- `task_type`：缺陷用 `ISSUE`，需求用 `REQUIREMENT`
- `category_id`：使用任务中心已启用的分类
- `title`、`description`
- `acceptance_criteria`：可以验收的结果

同一会话里相同内容再次创建会返回已有任务，不要再造一条。

## 状态提示

收到来源为 `nexus-task` 的提示后，先调用 `nexus_task_get`。提示不是新的状态，也不要根据提示直接改状态。

`WAITING` 表示需要人处理。把原因告诉当前对话里的人，不要自己确认完成。

## 工具

查询：`nexus_task_categories`、`nexus_task_get`、`nexus_task_list`、`nexus_task_logs`、`nexus_task_result`、`nexus_task_summary`
创建和修改：`nexus_task_create`、`nexus_task_update`、`nexus_task_assign`
控制：`nexus_task_start`、`nexus_task_pause`、`nexus_task_resume`、`nexus_task_terminate`、`nexus_task_retry`、`nexus_task_intervene`、`nexus_task_confirm`

`nexus_task_summary` 只读。总结和候选经验由后台复盘写入。
工具返回 `ok: false` 时，任务状态没有被这次调用改掉。
