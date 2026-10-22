## Why

数字员工缺少独立日程入口，同一员工可能接受不同人类安排，需要明确归属、隔离编辑与执行结果。AgentScope 2.0.10 已提供日程持久化、定时触发与执行会话，直接复用。

## What Changes

- 在数字员工菜单之后增加日程安排页，基于官方日程页的列表、创建编辑表单和执行历史适配 Nexus 员工与身份。
- 支持五字段 cron、时区、启停、独立或连续上下文、修改删除和执行记录。
- 当前人类只管理自己的安排；后端从可信身份确定创建人，不接受请求体指定归属。
- 日程与执行状态只取 AgentScope；安排者映射为独立的 AgentScope 用户，Nexus 不保存日程副本。

## Capabilities

### New Capabilities
- `nexus-employee-schedules`: 多人类向数字员工安排日程与执行会话查看。

### Modified Capabilities
无。不修改 Astra 历史契约。

## Impact

涉及 nexus_api 日程适配、Vue 页面、导航与针对性测试；不新增计时器，不修改 AgentScope 内部 schema，不增加工作流编排。首次仅支持官方已有周期日程，不提供一次性提醒或保证错过补跑。
