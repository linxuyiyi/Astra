## Context

AgentScope 2.0.10 scheduler 创建隔离 Session 后写 inbox/wakeup。Nexus 的通用资源同步未被调用；现有工具策略和 Task MCP 仅识别 Nexus 聊天绑定。官方 `basic` 组始终激活，因此空 activated_groups 本身不能作为无能力的判断标准。

## Goals / Non-Goals

目标：每次日程推理前收敛员工绑定，复用通用同步器及官方工作区安装接口，验证实际工具调用和失败行为。
非目标：复制 scheduler、Session、记忆或模型配置；修改 Library 注册凭据；自动重放线上业务。

## Decisions

- 在 Agent Service 现有 ChatService 适配层于运行锁获取前调用 Nexus 内部准备接口；准备失败传入工具装配阶段抛错，由官方运行层持久化错误事件。中断跳过准备及工具装配策略。保持官方调度和状态处理。
- Nexus 用 Agent Service 从官方存储读取的 ScheduleOrigin、用户、Agent 和日程记录校验来源，不建立新的会话归属表。资源与安装状态沿用现有员工目录及 resource_sync。
- 日程工作区请求使用日程身份，资源目录请求使用平台身份。库安装通过仅服务间可调用的 Agent Service 适配端点复用官方安装函数，只调整库读取的 owner，不共享用户 Session。
- 官方 LocalWorkspaceManager 的 PER_SESSION 仅隔离缓存 ID，物理目录仍按员工共用；日程使用轻量 manager 子类，按安排者、员工、持久化 workspace_id 派生目录和缓存键。普通聊天目录不变；旧日程共用目录不复制到新隔离目录。
- 工具策略每次按官方来源与当前员工权限检查；日程 Task MCP 在网关及每个工具调用时重验来源。任务 actor 使用明确的日程身份，不猜测不可逆哈希对应的人类 UID。
- 日程实际工具组装配后激活仍被授权的组；不激活已移除的调度/任务编排工具。绑定变更使用现有同步记录收回资源。

## Risks / Trade-offs

- 官方私有装配/路由接口依赖固定 2.0.10 → 使用该版本真实 SQL、scheduler 和工具调用回归；不改 site-packages。
- 准备与推理存在间隔 → 同一会话的准备与运行共用资源互斥，运行中的外部准备请求返回 pending 并阻止推理；工具执行仍重验权限。
- 线上真实模型、MySQL 和长周期运行 → 与本地桩验证分开报告；生产部署需同时更新 Nexus、Agent Service、Task MCP。
- 现有卡住执行不会自动重放 → 修复作用于下一次运行，历史执行的恢复需单独按业务确认。
