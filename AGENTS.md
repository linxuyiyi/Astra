# Astra 开发约定

本文件只放所有代码任务都要遵守的规则。OpenSpec 产物规则在 `openspec/config.yaml`；按需查阅 `docs/OPENSPEC_SUPERPOWERS.md`，不要在每轮对话加载整套开发教程。

## 工作原则

- 使用简体中文交流和编写项目文档；代码标识符、命令和 Conventional Commits 保持项目原有习惯。
- 先理解真实代码、需求和已有实现，再做最小有效修改；不为可能发生的问题增加架构、状态层或多套 fallback。
- 同一业务状态与规则只有一个事实源。保留已有改动，修改前确认仓库、分支和 `git status`；不直接在 `master` 开发，不无依据执行破坏性 Git 命令。
- 没有验证证据，不得声称完成。最终只汇报：原因、改动、验证、风险。

## 按变更规模选择流程

- **直接处理**：文案、局部样式、明确的小 Bug、纯重构、测试和文档，且不改变已约定行为。阅读相关代码，修改后运行针对性检查；不创建 OpenSpec change。
- **先定契约**：变更已约定的关键业务行为或既有 Spec，或涉及 API 契约、数据库模型、Runtime 状态/恢复、权限及跨模块架构时，先检查 `openspec/specs/` 的既有能力，再使用项目级 `/opsx-propose`（或分步命令）。Spec 只写可验证的需求和场景，不写实现方案。
- **一个计划**：使用 OpenSpec 时，以该 change 的 `tasks.md` 为唯一实施清单。Superpowers 的分析、调试、TDD、评审 Skill 按任务需要调用；不要再生成第二份同内容计划，不要求每个小任务都走头脑风暴或子代理流程。
- **完成闭环**：按场景核实代码和测试；影响 Spec 的变更执行 `/opsx-verify`，按需 sync，再 archive。不能实际运行的验证明确标为未验证。

## 不可混淆的架构边界

- Astra 管理 Task 的业务闭环、员工分配、运行观察、人工介入与异常恢复；任务内部的 Agent、Skill、Tool 及执行步骤由数字员工负责，平台不重复编排。
- **Task 是业务闭环；OpenCode Session 是执行事实源；RuntimeSession 只存绑定、终态和用量索引。** 不要重新引入 Run 状态机、消息副本或靠前端状态/时间戳推断 Runtime 终态。
- 正式 Runtime 使用 OpenCode Server / Session / Event；CLI fallback 仅用于明确的隔离测试或应急。修改生命周期须考虑取消、重试、人工确认、超时、断线和恢复，以及旧轮次结果不得覆盖新轮次。
- 平台助手使用独立 Runtime、不持有员工 Workspace；员工任务使用独立工作区。不要把平台助手和数字员工的权限、提示词、配置混用。
- 历史数据问题优先一次性迁移、回填或修复，不把旧字段兼容和猜测式 fallback 留在长期代码里。

## 技术边界与验证

- `backend/`：FastAPI、SQLAlchemy async、Celery、Alembic；修改后在此目录执行相关 `ruff`、`pytest` 或迁移检查。数据库事务只用于短状态更新，不在行锁内做 OpenCode / SSH / 网络 I/O。
- `frontend/`：Vue 3 + Element Plus。优先原生布局和表单组件，同类控件统一尺寸；主题色以 `frontend/src/styles/tokens.css` 与 `element-plus.css` 为准，不在页面重复定义主题。
- `workspace-ui/`：OpenCode 前端 fork。修改前确认 `UPSTREAM.json` 与公司版本兼容；不因修改管理台而顺手重建它。
- 先跑影响范围内的测试；核心 Runtime 或主要交互再考虑 `python scripts/verify.py` 和对应 E2E。生产构建产物可复用，但不能替代所改源码的测试。

项目工具、命令、离线构建、真实 OpenCode 测试与部署细节见 `README.md` 和 `docs/OPENSPEC_SUPERPOWERS.md`。
