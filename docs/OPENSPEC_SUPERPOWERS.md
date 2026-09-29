# Astra：项目级 OpenSpec + Superpowers

**唯一入口**：`AGENTS.md` 管通用开发底线；`openspec/config.yaml` 管 Spec 产物约束；已有能力以 `openspec/specs/` 为事实源。不要把同一要求分别复制到命令、Skill 和 Agent 提示词中。

## 如何选择

| 任务 | 做法 |
| --- | --- |
| 文案、局部 UI、明确 Bug、纯重构、测试与文档 | 直接定位代码、最小修改、针对性验证；不建 change |
| 行为契约、API、数据模型、权限、Runtime 状态或跨模块设计变化 | 先查既有 spec，用 `/opsx-propose`；必要时用分步命令 |
| 复杂排障或实施 | 按需显式调用 `/sp-debug`、`/sp-plan`、`/sp-execute`、`/sp-review`、`/sp-verify`，不重复维护任务清单 |

OpenSpec 回答“做什么、如何验收”；Superpowers 提供按需的分析/实现方法。**一个 change 只有一个 `tasks.md`。** 现有项目 `spec-driven` schema 的 design 是必需产物；无需 Spec 的小改动直接走轻量路径，不为它们伪造 proposal。

## 项目级文件

```text
AGENTS.md                         通用开发约束（常驻）
opencode.json                     仅注入 AGENTS.md
openspec/config.yaml              OpenSpec 默认 schema、背景与产物规则
openspec/specs/                   当前行为契约
openspec/changes/                 进行中的变更及归档
openspec/schemas/spec-driven/     本项目已提交的 workflow 与模板
.opencode/commands/               已提交的 /opsx-* 和 /sp-* 命令
.opencode/skills/                 已提交的 OpenSpec、Superpowers 等 Skill
.opencode/tools/openspec.mjs       已提交的自包含 CLI
.opencode/plugins/superpowers.js  V1/V2 适配器及可选 bootstrap
```

无需安装全局 Skill 或 CLI；工程以仓库内版本为准，不盲目用上游新命令覆盖本地适配。当前可用命令：`/opsx-explore`、`/opsx-propose`、`/opsx-new`、`/opsx-continue`、`/opsx-ff`、`/opsx-apply`、`/opsx-verify`、`/opsx-sync`、`/opsx-archive`。

默认**不自动注入**整篇 `using-superpowers` bootstrap，减少每次会话的提示词成本；Skill 仍可通过原生命令显式调用。需要恢复上游自动 Skill 检查行为时，在 OpenCode Server 启动环境设置 `ASTRA_SUPERPOWERS_BOOTSTRAP=1` 并重启。平台助手运行角色始终跳过开发 bootstrap，子代理也跳过。请勿通过仅修改 `AGENTS.md` 误以为能禁用插件注入。

## 自检与维护

```bash
# 从仓库根目录执行
node .opencode/tools/openspec.mjs templates
opencode debug skill
opencode debug config
```

升级 OpenSpec 前，在有依赖的维护环境验证版本和本地 Schema，再运行 `node scripts/build_openspec_bundle.mjs` 重建已提交 bundle；升级 Superpowers 需同步验证项目插件与 OpenCode V1（公司版）/V2 兼容，不依赖生产机联网安装。

生产平台助手和员工任务可能加载不同工作区的配置。排查某个 Session 的 Skill / MCP 不生效时，应检查该 Session 实际工作目录和 Server 配置，不能把 Astra 根目录的能力视为所有员工自动继承。
