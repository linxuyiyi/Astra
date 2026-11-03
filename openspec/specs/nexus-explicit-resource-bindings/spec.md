# nexus-explicit-resource-bindings

## Purpose

明确 Skill 与 MCP 作为独立资源进行登记、绑定和同步。内网 Task MCP 使用显式员工编号与任务业务参数，不依赖用户角色、员工岗位或会话来源权限；员工保存结果反映全部目标会话的最终同步状态。

## Requirements

### Requirement: 资源独立登记和绑定
系统 SHALL 只展示 Skill 管理、MCP 管理及 AgentScope 资源库中实际登记的资源。员工 SHALL 仅获得明确绑定且启用的资源；Skill 与 MCP 相互独立，名称不赋予特殊能力。

#### Scenario: 未登记资源
- **WHEN** 管理目录没有登记任务管理 Skill 或任务/日程 MCP
- **THEN** 目录不自动生成这些资源，员工保存及聊天不隐式安装它们

#### Scenario: 独立选择
- **WHEN** 员工只绑定 Skill、只绑定 MCP，或分别解绑其中之一
- **THEN** 仅同步选定的资源；绑定或解绑 Skill 不安装或卸载 MCP，反之亦然

#### Scenario: 普通名称
- **WHEN** 员工明确绑定名为 nexus-task 或 nexus-schedule 的有效 MCP
- **THEN** 允许正常绑定与通用同步，不因名称隐藏、拒绝或另走自动挂载路径

### Requirement: 内网 Task MCP 使用简单业务参数
创建工具 SHALL 显式接收 employee_id，直接校验员工存在，不要求用户角色、员工岗位、Skill 绑定、私聊、群讨论或日程上下文。source_* SHALL 是可选的通知信息，不用于权限判断。

#### Scenario: 仅提供员工和任务参数
- **WHEN** 调用方提供存在的员工 ID 和有效任务分类、仓库等业务参数，未提供来源会话或用户身份
- **THEN** 创建成功，任务责任员工为传入 ID，不执行员工 enabled、Skill 或会话权限校验

#### Scenario: 员工不存在
- **WHEN** 调用方提供不存在的员工 ID
- **THEN** 返回员工不存在，不写入任务

#### Scenario: 后续按任务编号处理
- **WHEN** 调用方查询或操作存在的任务 ID
- **THEN** 不要求原调用者身份或来源会话；任务状态、执行绑定和请求/回合有效性仍按既有业务规则处理

### Requirement: 员工同步结果反映最终状态
系统 SHALL 在全部资源同步完成后返回并保存员工同步状态；任一目标会话失败或待处理不得显示已同步，也不推进生效版本。

#### Scenario: 部分同步失败
- **WHEN** 邮箱资源正常而员工其他会话同步失败或仍在运行
- **THEN** 员工列表与保存响应一致显示失败或待处理，保留上次生效版本

#### Scenario: 连续保存成功
- **WHEN** 同一员工多次保存相同有效配置且全部会话核对成功
- **THEN** 清除旧异常，配置与实际挂载一致，员工及会话历史保留
