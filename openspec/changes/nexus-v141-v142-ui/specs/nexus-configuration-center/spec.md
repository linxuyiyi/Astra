## ADDED Requirements

### Requirement: 配置中心使用嵌套路由布局

配置中心 SHALL 采用左侧分组菜单加右侧内容的父子路由布局，并包含数字员工管理、Agent Worker、任务分类、AgentScope 四个入口。

#### Scenario: 直接进入配置中心
- **WHEN** 用户访问 `/settings`
- **THEN** 默认进入数字员工管理，并显示配置中心左侧菜单

#### Scenario: 子页面切换
- **WHEN** 用户点击左侧菜单的任一配置项
- **THEN** 右侧内容切换到对应页面，顶部“配置中心”保持高亮

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

### Requirement: AgentScope 入口承载官方管理 UI

配置中心的 AgentScope 子页 SHALL 内嵌官方 AgentScope v2.0.10 Web UI，并在默认部署中可直接进入管理界面；MUST NOT 只用状态、说明文字或外部链接占位。官方 UI SHALL 通过同源 `/agentscope-ui/` 提供，API 通过 `/agentscope-api/` 代理到 AgentScope，`X-User-ID` 使用当前部署的 AgentScope 用户。Skill/MCP/Knowledge/Session 仍以 AgentScope 为唯一事实源，Nexus MUST NOT 新建平行 CRUD 数据。

#### Scenario: 默认进入官方 UI
- **WHEN** 用户打开配置中心的 AgentScope 子页
- **THEN** 页面在内容卡内加载官方管理界面，无需手工填写 server_url 或用户名

#### Scenario: 官方 UI 深链接与静态资源
- **WHEN** 用户刷新 `/agentscope-ui/skill` 或直接打开该深链接
- **THEN** 官方 SPA 正常加载，静态资源和 API 请求均可用

#### Scenario: 原生 Hub 能力
- **WHEN** AgentScope 2.0.10 注册了 Skill/MCP Hub
- **THEN** 官方 UI 可浏览真实 Hub 与资源；没有注册的能力不显示伪造数据

#### Scenario: AgentScope 不可达
- **WHEN** AgentScope 未连接或不可达
- **THEN** 页面保留真实错误与状态，不伪造 Agent/Skill/MCP 数据

#### Scenario: 不新建管理数据系统
- **WHEN** 用户在官方 UI 创建或删除 Agent/Skill/MCP/Knowledge
- **THEN** 变更直接写入 AgentScope，Nexus 不保存第二套管理数据库
