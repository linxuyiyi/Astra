# 项目内 OpenSpec + Superpowers 集成

Nexus v1.4.7 继续使用仓库里已经提交的 OpenSpec 与 Superpowers，**clone 后启动 OpenCode 即可使用**，
不依赖任何全局安装、`~/.config`、`~/.opencode` 或 Home 目录配置。
没有在仓库外再找一套 Superpowers，也没有补写缺失的 skill。

## Nexus OpenSpec / Superpowers 盘点（只记录仓库里真实存在的文件）

2026-10-13 在仓库根目录 `.opencode/` 核对（当时分支记录为 Nexus v1.0.1）。Phase 2 的实现在 `services/task-platform` 与 `web/nexus`，不在名为 `nexus/` 的目录。结果如下。

Superpowers skill（15 个，均在 `.opencode/skills/<name>/SKILL.md`）：

`brainstorming`、`diagnosing-superpowers`、`dispatching-parallel-agents`、`executing-plans`、`finishing-a-development-branch`、`receiving-code-review`、`requesting-code-review`、`subagent-driven-development`、`systematic-debugging`、`test-driven-development`、`using-git-worktrees`、`using-superpowers`、`verification-before-completion`、`writing-plans`、`writing-skills`。

OpenSpec skill（9 个）：`openspec-apply-change`、`openspec-archive-change`、`openspec-continue-change`、`openspec-explore`、`openspec-ff-change`、`openspec-new-change`、`openspec-propose`、`openspec-sync-specs`、`openspec-verify-change`。

另外还有 1 个 Astra 产品 skill：`nexus-task-experience-review`。它不是 Superpowers，Nexus 产品不使用它。

命令：`/opsx-apply`、`/opsx-archive`、`/opsx-continue`、`/opsx-explore`、`/opsx-ff`、`/opsx-new`、`/opsx-propose`、`/opsx-sync`、`/opsx-verify`，以及 `/sp-brainstorm`、`/sp-debug`、`/sp-execute`、`/sp-finish`、`/sp-plan`、`/sp-review`、`/sp-verify`。

Agent：`code-reviewer` 属于这套开发集成。`platform-assistant` 是 Astra Runtime 代理，不是 Nexus 产品的一部分。

未找到、因此没有写入文档的内容：独立的 Superpowers 包、额外的 skill 目录，或上述名单之外的 `/sp-*` 命令。

## 目录与事实源

```text
Astra/
├── opencode.json                         # 项目配置：instructions 指向 AGENTS.md
├── AGENTS.md                             # 项目级开发硬规则（唯一）
├── .opencode/
│   ├── commands/                         # /opsx-* 与 /sp-* 命令
│   ├── agents/code-reviewer.md           # 只读代码评审子代理
│   ├── agents/platform-assistant.md      # 平台助手（独立 Runtime 标记）
│   ├── skills/                           # 25 个项目级 skill（OpenSpec 9 + Superpowers 15 + Astra 1）
│   ├── plugins/superpowers.js            # skill 注册 + 可选 bootstrap 注入
│   └── tools/openspec.mjs                # 自包含 OpenSpec CLI bundle（可提交产物）
├── openspec/
│   ├── config.yaml                       # 产物规则唯一事实源（context + rules）
│   ├── schemas/spec-driven/              # 本项目固定 schema 与模板
│   ├── specs/                            # 归档后的当前规范
│   └── changes/                          # 进行中变更 + archive/
├── tools/e2e/check_openspec_config.mjs     # 校验 config.yaml 未退化为死配置
└── tools/e2e/build_openspec_bundle.mjs     # 仅维护用：重新生成 bundle
```

仓库即唯一事实源。改动只需改仓库内文件，无需在开发机做任何安装或链接。

## OpenCode 如何加载

OpenCode 原生识别项目级目录，无需在 `opencode.json` 里逐个登记：

- 项目 skill：`.opencode/skills/<name>/SKILL.md`
- 项目命令：`.opencode/commands/<name>.md`
- 项目 agent：`.opencode/agents/<name>.md`
- 项目插件：`.opencode/plugins/*.js`（自动发现，已确认出现在 `opencode debug config` 的 `plugin`）

`opencode.json` 只声明 `instructions` 和 `mcp`，把 `AGENTS.md` 注入上下文。
修改这些文件后需要重启 OpenCode（配置只在启动时加载）。

## OpenSpec

- 命令：`/opsx-explore`、`/opsx-propose`、`/opsx-new`、`/opsx-continue`、`/opsx-ff`、`/opsx-apply`、`/opsx-verify`、`/opsx-sync`、`/opsx-archive`。
- skill：`.opencode/skills/openspec-*`。
- CLI：`.opencode/tools/openspec.mjs`。命令与 skill 内统一调用
  `node .opencode/tools/openspec.mjs <command>`，不依赖 PATH 上的 `openspec`。
- schema 由 `openspec/schemas/` 提供（CLI 的项目级优先级最高，已确认 `Source: project`）。
- 工作流：`需求/变更提出 → Spec → 设计/任务拆解 → 实现 → 验证 → 归档`。

### config.yaml 的真实能力边界

产物规则集中在 `openspec/config.yaml`，`openspec/AGENTS.md` 已删除（曾被 `instructions` 重复注入）。

内置 CLI 的 `ProjectConfigSchema` **只解析三个字段**：

| 字段 | 作用 | 校验 |
| --- | --- | --- |
| `schema` | 使用的 workflow schema | 必填，非空字符串 |
| `context` | 注入每个 artifact 的项目背景，上限 50KB | 可选 |
| `rules` | 按 artifact ID 附加的规则数组 | 可选，键必须是合法 artifact ID |

**其他顶层键会被静默丢弃。** 非法 `rules` 键只打印一行 warning 后忽略。
`spec-driven` 的合法 artifact ID 为 `proposal`、`specs`、`design`、`tasks`。
`tools/e2e/check_openspec_config.mjs` 会驱动真实 CLI 检出这两类问题，已接入 `scripts/verify.py`。

### design.md 是必需产物

`schema.yaml` 中 `tasks.requires: [specs, design]`，因此 `design.md` 总是需要生成；
schema 提示语已同步更正，避免"按需生成"与实际依赖冲突。没有真实设计决策时保持精简即可。

### 重新生成 CLI bundle（维护用）

升级 OpenSpec 后，在一台装有 `esbuild`（`web/nexus/node_modules/.bin` 内已有）的机器上执行：

```bash
node tools/e2e/build_openspec_bundle.mjs
```

脚本会把 OpenSpec 打包成单个自包含 `.mjs`，并内联两处运行时 `require`
（`package.json` 版本、`cross-spawn`），同时屏蔽 CLI 的重复自动执行。
OpenSpec 来源优先级：`ASTRA_OPENSPEC_PACKAGE` → `web/nexus/node_modules` → 全局 npm root。
普通开发者不需要运行此脚本，直接使用已提交的 bundle。

## Superpowers

- skill：`.opencode/skills/` 下的 15 个 Superpowers skill（`brainstorming`、`writing-plans`、
  `executing-plans`、`subagent-driven-development`、`test-driven-development`、
  `systematic-debugging`、`verification-before-completion`、`requesting-code-review` 等）。
- 命令：`/sp-brainstorm`、`/sp-plan`、`/sp-execute`、`/sp-debug`、`/sp-review`、`/sp-verify`、`/sp-finish`。
- agent：`code-reviewer`（`mode: subagent`，只读），由 `/sp-review` 使用。

### bootstrap 为按需启用

插件 `.opencode/plugins/superpowers.js` 做两件独立的事：

1. **注册 skill** —— 始终执行。V1 由 OpenCode 原生项目发现，V2 由 `ctx.skill.transform()`。
2. **注入 `using-superpowers` bootstrap** —— 需显式开启。

开启方式：启动 OpenCode 前设置 `ASTRA_SUPERPOWERS_BOOTSTRAP=1`。

默认关闭的原因：bootstrap 会在每轮对话注入约 3KB 通用提示词，并要求"任何响应前先调用 skill"，
使简单改动也被迫走完整的头脑风暴 → 计划 → 子代理流程；与 OpenSpec 的 change 流程重复。
关闭只影响常驻提示词，**skill 仍然注册且可通过原生 `skill` 工具显式调用**，
`/sp-*` 命令也不受影响。

注入还需同时满足（非仓库根目录、平台助手 Runtime、子代理会话均不注入）；
子代理判定基于 session 的 `parentID`，不依赖模型自觉遵守提示词。

## 与 Astra 既有能力共存

- `platform-assistant` agent 与 `nexus-task-experience-review` skill 已收敛为项目级：分别位于
  `.opencode/agents/platform-assistant.md` 与 `.opencode/skills/nexus-task-experience-review/`。
- OpenSpec/Superpowers 的 skill、命令、agent 命名（`openspec-*`、`opsx-*`、`sp-*`、`code-reviewer`）
  与既有资源无冲突，加载无重复项。

## 验证（干净环境）

以下方式可在不依赖全局安装的情况下复现：

```bash
# 1) 屏蔽全局 Superpowers（~/.claude/skills）后，仍能列出项目内 25 个 skill
OPENCODE_DISABLE_EXTERNAL_SKILLS=1 OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1 opencode debug skill

# 2) 屏蔽全局 openspec 后，仍能用仓库内 bundle 跑完整流程
PATH=/usr/bin:/bin node .opencode/tools/openspec.mjs templates   # Source: project
PATH=/usr/bin:/bin node .opencode/tools/openspec.mjs new change demo
PATH=/usr/bin:/bin node .opencode/tools/openspec.mjs status --change demo --json
PATH=/usr/bin:/bin node .opencode/tools/openspec.mjs validate demo
PATH=/usr/bin:/bin node .opencode/tools/openspec.mjs archive demo --yes

# 3) 项目配置与插件的回归检查（已接入 scripts/verify.py）
node tools/e2e/check_openspec_config.mjs
node tests/opencode/test-superpowers-bootstrap.mjs

# 4) 命令 / agent 被发现
opencode debug config    # 16 个 command、code-reviewer agent、superpowers 插件
opencode agent list
```

## 已知边界与风险

- 根目录 `.opencode/` 与 `opencode.json` 会被**同一 git worktree 内**的所有 OpenCode 进程继承。
  平台助手以仓库根为加载上下文，插件通过 `ASTRA_RUNTIME_ROLE=platform-assistant` 跳过 bootstrap 注入；
  助手仍可见 `/opsx-*`、`/sp-*` 命令与全部 skill，其主要约束是
  `.opencode/agents/platform-assistant.md` 中 `edit`/`write`/`bash`/`webfetch` 的 deny 权限。
- 员工任务工作区是 `services/task-platform/.astra-tasks/<employee>/<task>` 下的独立 git worktree。
  该目录带自己的 `.git`，`discover_workspace_config()` 的向上查找在 git 根停止，
  因此**不会**加载 Astra 根的 `opencode.json`；若 worktree 由包含本集成的提交创建，
  会带上一份 `.opencode/` 副本（其中 skill 与命令可见，但 bootstrap 因工作目录不等于仓库根而不注入）。
  数字员工自己的 OpenCode 配置由其工作区拥有，Astra 从不生成也不设置 `OPENCODE_CONFIG`。
- `openspec/specs/` 下 3 个 spec 由旧版 CLI 归档，缺少新版要求的 `## Purpose` 段，
  `openspec validate --all` 会报 3 项失败。历史内容本身有效，修复时补 `## Purpose` 即可，不要删除 Requirement。
- `opsx-archive` 命令内部使用 `mkdir -p` / `mv`，依赖 OpenCode 的 bash 环境；Windows 下需确保
  bash 语义可用（OpenCode 默认 bash 工具已满足）。核心 CLI 全部跨平台（仅依赖 `node`）。
- bundle 是从 `@fission-ai/openspec` 生成的可提交产物，升级时需按上文重新生成；
  升级后必须重跑 `tools/e2e/check_openspec_config.mjs`，因为新版可能新增或改名 config 字段。