## ADDED Requirements

### Requirement: 内置 MCP 使用标准 HTTP 注册

Nexus SHALL 通过 AgentScope 2.0.10 官方 Session 工作区 MCP 接口挂接 Task、Schedule 和 Library MCP；服务地址来自统一部署配置。登记 SHALL 幂等：已有相同 URL 和来源头的配置直接复用，地址变化时替换，不创建重复条目。

#### Scenario: 重复同步复用现有挂接
- **WHEN** 员工保存后再次同步且 MCP 地址、会话和来源头未变化
- **THEN** 不新增同名 MCP，也不删除现有条目

#### Scenario: 地址变化更新挂接
- **WHEN** `NEXUS_TASK_MCP_URL` 或 Task MCP 端口变化后再次同步
- **THEN** 该会话的 `nexus-task` 配置使用新地址，旧条目被替换而不是保留两份

### Requirement: 连接和工具发现不要求业务上下文

MCP 初始化和工具发现 SHALL NOT 要求正在运行的群讨论、有效讨论租约或私聊会话；SHALL NOT 因为没有可执行业务操作而拒绝连接。员工、会话、任务归属、讨论状态/租约和日程来源检查 SHALL 在实际工具调用时执行。

#### Scenario: 没有活跃讨论时工具发现成功
- **WHEN** AgentScope 读取一个群会话的 `/workspace/mcp`，该群当前没有 `running` 讨论
- **THEN** Task MCP 正常完成 `initialize` 和 `tools/list`，返回工具列表

#### Scenario: 缺少业务上下文时工具调用被拒绝
- **WHEN** 调用方直接调用 Task MCP 工具，但没有提供有效的员工/会话或任务来源
- **THEN** 工具返回 `ok=false` 和明确的业务上下文错误，不执行任务操作

#### Scenario: 私聊、群讨论和日程保持各自语义
- **WHEN** 私聊、有效群讨论或官方日程来源调用 Task MCP
- **THEN** 分别按私聊归属、群讨论参与者和租约、日程来源和员工权限校验，不互相借用身份

### Requirement: 内网 MCP 不共享 Bearer key

内网部署 SHALL NOT 生成、注入或校验 MCP 共享 Bearer key。服务默认监听 `127.0.0.1`；分机 AgentScope 可通过统一配置使用实际内网地址。已有环境文件中的旧 MCP key 项 SHALL 在升级时清理且不得导致部署失败。

#### Scenario: 旧环境升级
- **WHEN** 已有 `nexus.env` 含 `NEXUS_MCP_API_KEY`、`NEXUS_TASK_MCP_KEY` 或 `NEXUS_SCHEDULE_MCP_KEY`
- **THEN** 部署器清理这些项，保留其他密钥、数据库和业务配置并继续升级

#### Scenario: 无 key 的标准初始化
- **WHEN** 客户端不带 `Authorization` 请求内置 MCP 的 `initialize` 和 `tools/list`
- **THEN** 请求成功；工具调用仍执行原有业务权限检查

### Requirement: 同步状态反映真实失败环节

任务能力同步 SHALL 区分技能上传、MCP 注册、连接/工具发现和收回失败，并把失败原因写入员工 capability。技能上传失败 SHALL NOT 阻断 MCP 注册；任何挂接失败 SHALL NOT 被吞掉后报告成功。

#### Scenario: 服务不可达
- **WHEN** Task MCP 不可达且 AgentScope 工作区返回不健康或工具为空
- **THEN** 同步结果为 `error`，员工配置页显示“任务 MCP 连接或工具发现失败：…”而不是“已同步”

#### Scenario: 取消再启用
- **WHEN** 员工取消任务技能后再次启用
- **THEN** 取消时收回任务技能和 `nexus-task`，再次启用时重新挂接且不残留邮箱中的任务 MCP

### Requirement: 部署验收使用真实 MCP 与 AgentScope 接口

部署 SHALL 对每个内置 MCP 实际执行 MCP `initialize` 和 `tools/list`，并 SHALL 通过真实 AgentScope `/workspace/mcp` 注册 Task/Schedule、核对连接状态和工具。临时校验 Agent/Session SHALL 在验收后删除；任一必要服务或 MCP 验收失败 SHALL 使部署以明确阶段失败。

#### Scenario: 首次部署验收
- **WHEN** 首次安装完成并启动全部服务
- **THEN** 部署器验证三个 MCP 的初始化和工具列表，经 AgentScope 注册 Task/Schedule 后看到健康工具，并清理临时校验记录

#### Scenario: 验收失败
- **WHEN** 某个 MCP 无法初始化、工具发现为空或 AgentScope 注册后不健康
- **THEN** 部署返回非零并指出失败的是 MCP 初始化/发现还是 AgentScope 注册/连接，不报告整体成功
