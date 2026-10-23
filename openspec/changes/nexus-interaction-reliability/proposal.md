## Why

真实交互报告暴露交接回复不可见、并发消息拒绝不明确、正文误触发同事、工具失败误报及任务分类不可发现。逐项复现后修复机制问题，对尚无证据的模型/部署问题补验证，不能把既有产品限制直接当缺陷。

## What Changes

- 私聊明确拒绝忙碌消息，保留用户可重试内容，不引入第二套会话队列。
- 同事支持精确姓名、ID、唯一岗位解析；来源明确的回复同时进入原生视图与发送方下一轮上下文，仍读取 AgentScope。
- 跳过代码、引用和日志中的 mention，保留有界自然同事交流；记录追加参与者与流事件来源。
- 群预算区分实际总消耗与生成限额，避免重复计算历史使初始参与者饥饿；保留时间、轮次与单次输出上限。
- 修正工具失败状态；提供已启用任务分类及任务类型枚举；邮箱不挂无授权的 Task MCP。
- 验证 SQLite/MySQL 并发、原始协议输出及真实 Worker/MCP 的环境条件，按证据修复。

## Capabilities

### New Capabilities
- `nexus-interaction-reliability`: 消息拒绝、交接可见性、路由、预算、工具展示及分类发现的可验证交互契约。

### Modified Capabilities
无。现存 openspec/specs 是历史 Astra 契约；相关 Nexus capability 仍位于 changes。

## Impact

涉及 nexus_api、Task MCP、Agent Service 配置与 chat-island。复用现有权限和 AgentScope Session，不新增审批、私聊多会话、Agent 工作流或对话存储。保留原报告，新增复核矩阵与真实验证证据。
