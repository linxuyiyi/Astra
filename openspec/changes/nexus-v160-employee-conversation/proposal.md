## Why

当前数字员工页由 Vue 同时管理会话、事件、Markdown、输入及滚动。仓库还发布一套历史 React 聊天岛。v1.6.0 需要让私聊与群聊共享可分段阅读、能说明真实工作状态的对话体验。

## What Changes

- 在现有员工页装配统一的 React 对话区，保留员工目录、群管理与资料入口。
- 以轻量状态、可展开的工具详情和有来源的任务入口组织消息。
- 私聊与群聊按需读取历史，隔离切换前的订阅、草稿与发送重试。
- 替换既有 Vue 消息渲染与历史聊天岛，按原有顺序交付静态资源。

## Capabilities

### New Capabilities

- `nexus-employee-conversation`：数字员工对话的工作反馈、交互与历史恢复。

### Modified Capabilities

无。

## Impact

涉及 Nexus 对话接口、chat-island 与 web 的员工聊天入口；沿用现有员工、Session、任务及权限事实源。
