## Context

员工 C 编辑后报“未发现 nexus-task”。普通资源同步与名称驱动的自动挂载互相装卸，同步快照又在最终同步前写入。用户明确要求 Skill/MCP 独立配置，并要求内网 Task MCP 保持简单，不做用户、员工岗位或会话来源的权限校验。

## Goals / Non-Goals

管理页配置是绑定唯一事实源；AgentScope 工作区是运行资源事实源。Task MCP 显式接收员工 ID，只校验员工存在。无需引入用户角色、能力签名、会话身份解析或额外防御层。不改变 Task、Run、Worker、OpenCode 的生命周期。

## Decisions

- 删除自动技能目录项、任务/日程 MCP 注入、Skill 隐式授予 MCP、MCP 名称特权，统一使用通用资源同步。
- 删除 Task MCP 的私聊、群讨论、日程及员工 Skill 权限前置检查。创建工具显式接收 employee_id，直接查询现有员工表确认存在，不检查 enabled 或技能权限。
- source_* 是可选通知元数据，不作为认证、授权或创建前置条件。查询和后续控制按任务 ID 处理，列表可按员工 ID 筛选。
- 仓库、分类、任务状态、当前执行 Session 和请求/回合标识等业务有效性规则继续走已有 TaskService/RuntimeService。
- 最后保存并返回统一同步结果，失败/待处理保留上次 applied_revision。
- 全新空库直接登记实际 Skill/MCP；不实现旧特殊 ID 转换或数据兼容。

## Risks / Trade-offs

- Task MCP 面向内部可信网络，接口不提供用户或员工访问权限隔离；这是本次明确约定。
- 工具 employee_id 参数需要调用方按新 schema 传入；旧隐式来源鉴权链删除，不提供双轨。
- 公司环境未连接，不能把本地验证描述为公司环境验收。

## 分类派工与 Worker Git 配置（用户追加约定）

- 删除 CodeRepository/GitCredential 独立台账与任务 repository_id。任务页面、API、MCP 均不提供仓库选择和仓库查询。
- Worker 执行环境直接存 repository_url、branch、加密 git_token_ciphertext；写入可选 git_token，读取只返回 has_git_token。
- Dispatcher 仅按现有任务类型/分类、Worker 运行与占用状态选择，不匹配仓库。
- Worker 的 Git 部署和更新继续使用既有 Git 执行逻辑，只从自身配置取值，不验证 Git 与分类的一致性。
- 按用户最新明确要求，仅面向全新空库初始化：当前 init 直接使用新结构，不做旧库升级、数据回填或兼容链。
