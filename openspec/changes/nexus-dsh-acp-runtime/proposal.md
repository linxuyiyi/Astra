## Why

Nexus v1.4.8 的任务执行与 OpenCode 原生协议耦合，尚不能安全使用 DSH。依照已锁定的 `docs/architecture/nexus-dsh-acp-adaptation.md`，通过真实 ACP 兼容性证据逐步增加 DSH 引擎，并复用已有业务状态、Worker 通道与管理页面。

## What Changes

- 首先提供固定 DSH/ACP 版本、独立持久目录、Nexus Gateway 模型路由的真实探针；未通过门禁禁止正式切流。
- Worker 增加受监督的 ACP 进程、Session 恢复、提交去重、停止与权限审批；连接丢失和未知受理结果不得触发重跑。
- Runtime 保存最小引擎绑定，通过薄驱动隔离原生协议，保留 Task 人工确认、Git worktree 与现有 Worker 分配规则。
- DSH 在现有任务路由使用通用工作台，展示真实消息/工具/权限与 Git Diff，按任务和绑定隔离；OpenCode 继续使用原生工作台。
- 按需读取 DSH 原生轨迹供复盘使用，保留 candidate 人工审核。

## Capabilities

### New Capabilities

- `nexus-dsh-acp-runtime`: DSH 安装、兼容性门禁、Worker Session 生命周期、受理隔离与恢复、工作台和证据读取。

### Modified Capabilities

无。`openspec/specs/` 中现有 capability 为 Astra 历史契约，不修改。

## Impact

涉及 `services/nexus-api/app/runtime`、`worker_agent`、Worker Gateway、模型与新增 Alembic revision、API 与 Vue 工作台，以及固定版本依赖和 Linux 实证。AgentScope 聊天与记忆不变；不复制 DSH Web、不新增编排器、不自动跨引擎回退、不修改已发布 init、不在本次直接切换正式默认引擎。
