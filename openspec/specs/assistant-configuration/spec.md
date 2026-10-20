> **已废弃（2026-10-20）**：平台助手及其任务复盘/总结已被数字员工替代并移除，见 `openspec/changes/nexus-remove-platform-assistant/`。本文保留为历史契约，不再代表当前实现。

# assistant-configuration

## Purpose
定义平台助手的配置范围：业务配置必须保留，Workspace 相关概念一律不得出现。

## Requirements

### Requirement: 助手配置不包含 Workspace 概念
平台助手的配置项、API 与界面 MUST NOT 暴露 Workspace 概念，包括工作空间路径、初始化、上传、Git 与文件管理。

#### Scenario: 配置项无 Workspace
- **WHEN** 读取平台助手配置项
- **THEN** MUST NOT 存在 `assistant_workspace_dir`、`assistant_workspace_path`、`assistant_repo_path` 等字段

#### Scenario: 设置界面无 Workspace
- **WHEN** 用户在设置中查看平台助手
- **THEN** 界面 MUST 仅展示有业务意义的配置，不出现工作空间相关入口

### Requirement: 助手保留业务配置
系统 SHALL 保留数字助手业务必需配置：启用状态、Agent 名称、默认模型、运行地址与凭据、用户身份、请求超时、限流与操作 TTL。

#### Scenario: 助手配置生效
- **WHEN** 配置助手 Agent 或默认模型
- **THEN** 系统 MUST 在启动助手 Runtime 时应用该 Agent 与模型