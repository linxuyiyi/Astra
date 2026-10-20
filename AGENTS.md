# Nexus v1.4.7 开发约定

只放所有代码任务都要遵守的规则。OpenSpec 产物规则在 `openspec/config.yaml`，工具与集成细节见 `docs/architecture/OPENSPEC_SUPERPOWERS.md`；按需查阅，不要每轮加载整套教程。

本分支的产品是 **Nexus v1.4.7**。数字员工、私聊、团队和 `contact_employee` 由 `services/nexus-api` 与 `services/agent-service` 承担。任务、Worker、OpenCode、网关和图书馆在 `services/task-platform`，由 `web/nexus` 经 `/api` 调用。

## 项目范围

Astra 负责长期任务的分配、执行、状态跟踪、人工介入和异常恢复。

平台管理 Task、Run、Employee 和 Runtime 状态。
任务具体怎么执行，由对应数字员工自己处理。

不要在 Astra 中增加 Agent 内部步骤、Skill、Tool 或工作流编排能力。

## Runtime

正式运行方式为 **OpenCode Server / Session / Event**。
`opencode run` 只作为独立的 CLI fallback。

修改 Runtime 相关代码前，先理清：

```text
Task → Run → Employee → Worker → OpenCode Session
```

注意：

- Task、Run、Employee、Session 状态要保持一致。
- 不要根据时间戳、日志、事件数量或前端状态反推 Runtime 状态。
- Runtime 展示以 `services/task-platform/app/runtime/presentation.py` 为准。
- 修改生命周期时，要同时考虑失败、取消、超时、重试、重启、心跳丢失和恢复。
- 优先解决状态流转问题，不要靠 fallback 兜底。

## 数据兼容

历史异常数据优先通过 **Alembic migration、backfill 或一次性修复脚本**处理。

不要为了兼容旧数据长期保留：

- 新旧字段同时判断；
- 多层 fallback；
- 重复状态映射；
- 对异常历史数据的猜测处理。

能一次迁移的数据，就不要把兼容逻辑留在长期运行代码里。

## 工作原则

- 使用简体中文交流和编写项目文档；代码标识符、命令和 Conventional Commits 沿用仓库既有习惯。
- 先读真实代码、确认现状与需求，再做最小有效修改；不为臆测的问题增加状态层、抽象层或多套 fallback。
- 同一业务状态与规则只有一个事实源。修改前确认分支与 `git status`，不覆盖他人未提交改动。
- 优先复用现有实现，能收敛就不要再加一套。不顺手修改与当前任务无关的代码。
- 没有验证证据不得声称完成。最终只汇报：原因、改动、验证、风险。

## 按变更规模选择流程

- **直接处理**：文案、局部样式、明确的小 Bug、纯重构、测试与文档，且不改变已约定行为。读相关代码后修改，运行针对性检查；不创建 OpenSpec change。
- **先定契约**：改变已约定的业务行为、既有 Spec，或涉及 API 契约、数据模型、权限、跨模块架构时，先查 `openspec/specs/` 是否已有对应 capability，再用 `/opsx-propose`（或分步命令）。纯重构不虚构业务 Requirement。`openspec/specs/` 里现存文档是 Astra 历史契约，Nexus 不修改它们。
- **一个计划**：用 OpenSpec 时以该 change 的 `tasks.md` 为唯一实施清单。Superpowers 只使用仓库里已经存在的 skill，不生成第二份同内容计划，也不虚构缺失的 skill。
- **完成闭环**：按 Scenario 核实代码与测试；影响 Spec 的变更执行 `/opsx-verify`，需要时 `/opsx-sync` 再 `/opsx-archive`。跑不了的验证如实标为未验证。

## 产品边界

- 对话、员工目录和 Agent 绑定写在 `services/nexus-api`。任务、Worker、网关和图书馆沿用 `services/task-platform/app` 的现有服务，不另写一套状态机。
- 前端是 `web/nexus`：Vue 3 + Element Plus。员工目录是 `services/nexus-api`：Python HTTP 服务，只通过 HTTP 调用 AgentScope，不内嵌、不 fork AgentScope。任务 API 是 `services/task-platform/app`。
- **AgentScope 2.0.10 Agent Service 是 Agent、Session、Skill、Knowledge 与对话记忆的唯一事实源。** Nexus 只保存员工目录、会话绑定和尚未被 Agent Service 接受的用户消息。不要在 Nexus 再实现一套 Agent、Session 或 Memory。
- Nexus 不提供模型、凭证或供应商配置界面。模型配置留在 AgentScope。
- 一位数字员工对应一个独立 Agent。一条私聊或一位员工在某个团队讨论中的上下文对应一条 Session。
- 员工之间的平等交流不使用 AgentScope Agent Team。Agent Team 是 Leader 派工给 Worker，不是对等同事。
- 同事联系走部署在 Agent Service 上的自定义工具 `contact_employee`：向对方 Session 的 inbox 投递 `HintBlock`，再由 MessageBus 的 wakeup 唤醒对方自己推理。Nexus 不编排脚本化的多 Agent 回复。

## 技术边界

- AgentScope 版本以 `services/agent-service/agentscope.version` 为准，当前为 PyPI / GitHub tag `2.0.10`。接口说明以该 tag 的源码为准，见 `docs/architecture/agentscope-2.0.10.md`。
- 正式部署是独立的 AgentScope Agent Service + Redis。`create_app` 的 workspace 隔离使用 `IsolationPolicy.PER_SESSION`，这样同一员工的多条对话不会被 `(user, agent, workspace)` 折成一条 Session。
- 不把历史 Astra 行复制进 Nexus SQLite。任务库仍是 task platform 的 SQLAlchemy 库，新字段用 Alembic 增加。不要为了旧状态长期保留双写和猜测式 fallback。

## 前端表格（Element Plus）

以下是默认做法（SHOULD）；某页确需偏离时，在改动说明里写清原因和影响面。

- 表格统一用 Element Plus 的 `el-table` + `el-table-column`，不引入第二套表格库，也不为单个页面重写一套表格组件。
- 列表页保持同一套形态：分页（`el-pagination`）、加载（`v-loading`）、空态（`el-empty`）、失败可重试；新增页面沿用相邻页面的列宽与对齐。
- 数据量用后端分页或限制返回量控制，不在前端全量渲染几百行。
- 表格数据只取一个接口事实源，不在前端拼多份派生状态或跨接口合并。
- 样式调整走 `web/nexus/src/styles/element-plus-theme.css` 的主题层，不在各页面写零散的 `.el-table` 覆盖。
- 若确要迁移到 Tailwind/shadcn 等新方案：先拿单页试点并记录成本，按页面切片独立提交，不与业务改动混在一起。

## 验证

先跑与本次改动最相关的检查，再根据影响范围扩大。

```bash
# task platform
cd services/task-platform && .venv/bin/pytest tests/<相关测试>
.venv/bin/ruff check app tests

# nexus api
PYTHONPATH=services/nexus-api services/nexus-api/.venv/bin/pytest services/nexus-api/tests

# web
cd web/nexus && npm run type-check && npm run build
```

涉及核心 Runtime 或主要用户流程时，再运行对应 E2E。

完成前检查 `git diff`，确认没有无关修改、调试代码和多余兼容逻辑。

最终说明：

**问题原因 → 修改内容 → 验证结果 → 剩余风险**
