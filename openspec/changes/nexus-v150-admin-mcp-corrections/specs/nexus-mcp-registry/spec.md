## ADDED Requirements

### Requirement: 平台 MCP 唯一登记

系统 SHALL 在平台 AgentScope owner 下按稳定运行名登记 Nexus MCP，与员工绑定独立。

#### Scenario: 尚无员工绑定

- **WHEN** 管理员创建、更新或手动同步 MCP
- **THEN** AgentScope 平台库写入该运行名的唯一记录，页面报告登记结果或可重试失败

#### Scenario: 日程挂载和删除

- **WHEN** 日程会话准备已绑定的 MCP，或管理员删除已解除绑定的 MCP
- **THEN** 日程使用同一平台库记录且保持会话隔离，删除撤销平台库对应记录
