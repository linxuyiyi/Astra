## Why

员工 C 保存后出现同步异常，源于普通资源同步与名称驱动的自动挂载逻辑互相装卸，状态又在最终同步完成前写入。用户明确要求 Skill 与 MCP 均为独立配置资源，不存在平台内置、技能隐式赋予 MCP 等规则。

## What Changes

- Skill/MCP 目录只展示实际登记的资源，员工仅获得明确绑定且启用的资源。
- 移除任务、日程的自动挂载、按技能隐式赋予 MCP、按名称放行工具及自动生成技能目录项。
- 所有资源沿用通用同步入口；Task MCP 显式传入员工 ID，只做存在性检查，移除用户/员工/会话权限链。
- 员工保存完成全部同步后写入并返回一致结果；失败/待处理不推进生效版本。
- 已有特殊绑定一次性转换为管理页登记的普通资源，保留员工、原会话和聊天记录。

## Capabilities

### New Capabilities

- `nexus-explicit-resource-bindings`: Skill/MCP 显式登记、独立绑定与统一同步。

### Modified Capabilities

无；不修改 Astra 历史 Spec。当前 Nexus 自动挂载旧行为由本次明确的新契约替代，业务来源隔离继续保留。

## Impact

涉及员工/资源 API、通用同步、Task MCP 参数及调用、Agent Service 绑定资源过滤与相关测试。无新表、不修改 AgentScope 内部 schema、不新增编排或状态机。
