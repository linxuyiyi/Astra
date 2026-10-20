> **说明（2026-10-20）**：平台助手已移除；文中助手相关条目仅作历史记录，其余规则仍适用。

# project-capability-registry

## Purpose
定义 Skill、Agent、Command 与 MCP 的唯一定义来源在项目级结构，不保留助手本地副本。

## Requirements

### Requirement: 能力项目级单一定义
Skill、Agent、Command 与 MCP SHALL 以 Astra 项目级结构为唯一定义来源：`.opencode/skills/`、`.opencode/agents/`、`.opencode/commands/` 与根 `opencode.json`。系统 MUST NOT 保留助手本地的这些能力副本。

#### Scenario: 助手 Agent 项目级加载
- **WHEN** 助手 Runtime 创建会话
- **THEN** 助手 Agent SHALL 从 `.opencode/agents/platform-assistant.md` 加载

#### Scenario: 助手 Skill 项目级加载
- **WHEN** 助手在会话中使用任务经验审核能力
- **THEN** 该能力 SHALL 从 `.opencode/skills/nexus-task-experience-review/` 加载

#### Scenario: MCP 项目级加载
- **WHEN** 助手 Runtime 启动
- **THEN** `nexus-assistant` MCP SHALL 由根 `opencode.json` 配置提供

#### Scenario: 无重复能力
- **WHEN** 检查助手相关能力来源
- **THEN** MUST 不存在独立的 Skill、MCP、Command、Agent 或 `opencode.json` 副本

### Requirement: 能力定义项目级、授权 Agent 级
系统 SHALL 在项目级定义能力，并通过 Agent 级权限控制可用范围；MUST NOT 通过为不同 Agent 复制能力定义来实现隔离。

#### Scenario: 按 Agent 授权
- **WHEN** 平台助手与数字员工执行 Agent 使用同一项目级能力集
- **THEN** 各自可使用的能力 MUST 由 Agent 级权限决定，而非各自维护一份能力定义