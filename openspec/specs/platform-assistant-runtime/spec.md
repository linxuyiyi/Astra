> **已废弃（2026-10-20）**：平台助手及其任务复盘/总结已被数字员工替代并移除，见 `openspec/changes/nexus-remove-platform-assistant/`。本文保留为历史契约，不再代表当前实现。

# platform-assistant-runtime

## Purpose
定义平台助手的运行方式：作为项目级 Agent 使用独立 Runtime，不拥有专属工作区。

## Requirements

### Requirement: 数字助手不拥有独立 Workspace
数字助手 SHALL 作为 Astra 项目级 Agent 运行，系统 MUST NOT 创建、初始化或维护数字助手专属 Workspace（例如 `nexus-assistant/`）。助手 OpenCode Server 的工作目录 MUST 为 Astra 项目根目录，且该目录仅作为能力加载上下文。

#### Scenario: 启动助手不创建 Workspace
- **WHEN** 助手 Runtime 启动
- **THEN** 系统 MUST NOT 创建任何助手专属工作区目录，也 MUST NOT 校验其存在

#### Scenario: 助手加载项目配置
- **WHEN** 助手 OpenCode Server 启动
- **THEN** 其工作目录 MUST 为 Astra 项目根目录，并加载项目根 `opencode.json` 与 `.opencode/`

### Requirement: 数字助手保留独立 Runtime
系统 SHALL 为数字助手维护独立的 OpenCode Runtime（独立监听地址与凭据），该 Runtime MUST 与数字员工 Task Runtime 分离。

#### Scenario: 助手与任务 Runtime 隔离
- **WHEN** 数字员工任务 Runtime 出现异常（卡死、超时、工具异常）
- **THEN** 数字助手 Runtime MUST 仍可正常启动与响应

#### Scenario: 端口独立
- **WHEN** 助手 OpenCode Server 启动
- **THEN** 它 MUST 使用与平台公共 Serve、员工 Serve 不同的端口

### Requirement: 数字助手保留独立 Session
数字助手 SHALL 使用独立的 OpenCode Session，且 MUST NOT 与数字员工 Task Session 混用。

#### Scenario: 助手 Session 身份隔离
- **WHEN** 助手创建 OpenCode Session
- **THEN** Session MUST 带有助手身份标记（`astraRuntime: platform-assistant`），且 MUST NOT 与任务执行 Session 共享

### Requirement: 助手权限边界
数字助手以 Astra 项目为加载上下文，但 MUST NOT 具备修改 Astra 源码的能力。助手 Agent MUST 默认拒绝 `edit`、`write`、`bash`、`webfetch`，平台操作 MUST 通过 `nexus-assistant` MCP 完成。

#### Scenario: 禁止直接修改源码
- **WHEN** 用户请求助手修改 Astra 仓库文件或执行 Shell 命令
- **THEN** 助手 MUST 拒绝，并改为通过平台 MCP 提供可用操作

#### Scenario: 平台操作走 MCP
- **WHEN** 用户请求创建、取消、重试、恢复或重分配任务
- **THEN** 助手 MUST 通过 `nexus_*` MCP 工具执行，且变更类操作 MUST 先 prepare 并经用户确认

### Requirement: 助手启动不生成临时配置
助手启动 SHALL 直接使用 Git 仓库中的项目配置；系统 MUST NOT 生成、复制或运行时拼装助手专属 `opencode.json`，也 MUST NOT 通过 `OPENCODE_CONFIG` 指向助手目录。

#### Scenario: 配置来源唯一
- **WHEN** 助手 Runtime 启动
- **THEN** 其 OpenCode 配置来源 MUST 为 Astra 项目根 `opencode.json`