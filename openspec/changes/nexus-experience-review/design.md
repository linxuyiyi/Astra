## 决策

### 触发与事务

只在 `RuntimeService.confirm_completion()` 人工确认路径创建 Job。确认 Event、Job、Task SUCCEEDED 在同一事务提交；`review_key` 由不可变确认 Event ID 计算，重复确认由唯一约束保护。

### 状态与恢复

Job 状态：PENDING / COLLECTING / ANALYZING / VALIDATING / WRITING / SUCCEEDED / NO_FINDINGS / RETRY_WAIT / FAILED / SUPERSEDED / SKIPPED_SOURCE_MISSING / SKIPPED_TOO_LARGE。片段独立记录进度，Map 成功结果持久化，重试不重做已完成片段。

### 轨迹读取

按 Session 分页读取原生消息，标准化为带 trace id 的事件；不复制完整消息库。超过消息数/字节硬上限使用 SKIPPED_TOO_LARGE，不静默截断。

### 分层分析

按因果边界切片，Map 只输出局部 observation；Reduce 去重、关联、冲突检测后输出候选；Harness 分析只读取结构化 observation，不重读全文。上下文预算按 32K 设计，超限自动拆段。

### 证据与写入

模型只能引用已分配的 trace id；服务端校验 Session 归属、message/part 存在、确认水位与摘要。证据不通过则丢弃候选。候选写入统一走 `KnowledgeService.mcp_submit_experience`，origin 为 `system_review`，状态 candidate，verification unverified，不允许自动语义合并或发布。

### 模型

独立 `ReviewLLMClient` 调用现有 Gateway `/v1/chat/completions`，不创建 AgentScope 长期 Agent，不使用员工工具权限。首版只生成候选与改进建议，不自动应用配置。
