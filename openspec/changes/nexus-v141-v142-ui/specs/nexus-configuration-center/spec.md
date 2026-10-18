## ADDED Requirements

### Requirement: 配置中心使用嵌套路由布局

配置中心 SHALL 采用左侧分组菜单加右侧内容的父子路由布局，并包含数字员工管理、Agent Worker、任务分类、AgentScope 四个入口。

#### Scenario: 直接进入配置中心
- **WHEN** 用户访问 `/settings`
- **THEN** 默认进入数字员工管理，并显示配置中心左侧菜单

#### Scenario: 子页面切换
- **WHEN** 用户点击左侧菜单的任一配置项
- **THEN** 右侧内容切换到对应页面，底部“配置中心”保持高亮

### Requirement: 员工管理与聊天职责分离

“数字员工”主入口 SHALL 用于聊天与协作；配置中心的“数字员工管理” SHALL 用于创建、编辑、启停和配置员工。

#### Scenario: 从配置中心编辑员工
- **WHEN** 用户在配置中心修改员工并保存
- **THEN** 员工目录更新，统一聊天页使用更新后的目录数据

#### Scenario: 从聊天进入管理
- **WHEN** 用户需要创建或编辑员工
- **THEN** 通过配置中心的员工管理完成，而不是在聊天页内维护员工目录

### Requirement: Worker 保留完整管理能力

配置中心的 Agent Worker SHALL 保留原工作环境、部署、重启、配置、任务关联、暂停与恢复能力，不得退化为只读简表。

#### Scenario: 查看 Worker 详情
- **WHEN** 用户打开某个 Worker
- **THEN** 可以看到任务、用量、工作环境与配置，并可执行原有操作

### Requirement: 任务分类复用现有管理能力

配置中心的任务分类 SHALL 复用现有列表、筛选、编辑抽屉、提示词和启停功能。

#### Scenario: 编辑任务分类
- **WHEN** 用户新增或编辑任务分类并保存
- **THEN** 分类列表与平台缓存刷新，原提示词和启用状态语义保持不变

### Requirement: AgentScope 入口只展示真实能力

AgentScope 入口 SHALL 展示真实可达状态与可用的管理入口；如果服务没有可安全内嵌的管理界面，MUST 明确提供外部入口或说明，不得伪装成已有的 Skill/MCP 管理页面。

#### Scenario: AgentScope 不可达
- **WHEN** AgentScope 未连接或不可达
- **THEN** 配置中心显示真实状态与失败原因，不展示伪造的管理数据

#### Scenario: 没有内置图形界面
- **WHEN** 部署的 AgentScope 没有可安全内嵌的管理界面
- **THEN** 页面提供明确的服务入口或说明，并声明 Nexus 不另建 Skill/MCP/Session 数据库
