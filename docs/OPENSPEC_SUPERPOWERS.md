# 项目内 OpenSpec + Superpowers 集成

Nexus 把 OpenSpec 与 Superpowers 的开发能力直接放进仓库，**clone 后启动 OpenCode 即可使用**，
不依赖任何全局安装、`~/.config`、`~/.opencode` 或 Home 目录配置。

## 迁移来源

本仓库从空业务仓库建设，工程规范复制自原工程 Astra 的成熟研发流程，并做最小适配：

- 保留：通用 `skills/`、`commands/`、`openspec/schemas/`、OpenSpec CLI bundle 与相关脚本/测试。
- 替换：项目背景改为 Nexus（模块化单体，AgentScope 独立集成），去除旧业务、旧架构与旧平台助手内容。
- 未迁移：Astra 的业务源码、旧 Spec、历史数据与旧助手专用配置。

原工程仅作为规范来源，不参与构建，也不写入本仓库。

## 目录与事实源

```text
Nexus/
├── opencode.json                         # 项目配置：instructions 指向 AGENTS.md，model 声明开发模型
├── AGENTS.md                             # 项目级开发硬规则（唯一）
├── .opencode/
│   ├── commands/                         # /opsx-* 与 /sp-* 命令
│   ├── agents/code-reviewer.md           # 只读代码评审子代理
│   ├── skills/                           # 24 个项目级 skill（OpenSpec 9 + Superpowers 15）
│   ├── plugins/superpowers.js            # skill 注册 + 可选 bootstrap 注入（纯 JS）
│   └── tools/openspec.mjs                # 自包含 OpenSpec CLI bundle（可提交产物）
├── openspec/
│   ├── config.yaml                       # 产物规则唯一事实源（context + rules）
│   ├── schemas/spec-driven/              # 本项目固定 schema 与模板
│   ├── specs/                            # 归档后的当前规范
│   └── changes/                          # 进行中变更 + archive/
├── scripts/check_openspec_config.mjs     # 校验 config.yaml 未退化为死配置
└── scripts/build_openspec_bundle.mjs     # 仅维护用：重新生成 bundle
```

仓库即唯一事实源。改动只需改仓库内文件，无需在开发机做任何安装或链接。

## OpenCode 如何加载

OpenCode 原生识别项目级目录，无需在 `opencode.json` 里逐个登记：

- 项目 skill：`.opencode/skills/<name>/SKILL.md`
- 项目命令：`.opencode/commands/<name>.md`
- 项目 agent：`.opencode/agents/<name>.md`
- 项目插件：`.opencode/plugins/*.js`（自动发现）

`opencode.json` 只声明三件事：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "instructions": ["AGENTS.md"],
  "model": "deepseek/deepseek-flash"
}
```

它不声明任何 MCP、全局 token 或硬编码 key。修改这些文件后需要重启 OpenCode（配置只在启动时加载）。

## OpenSpec

- 命令：`/opsx-explore`、`/opsx-propose`、`/opsx-new`、`/opsx-continue`、`/opsx-ff`、`/opsx-apply`、`/opsx-verify`、`/opsx-sync`、`/opsx-archive`。
- skill：`.opencode/skills/openspec-*`。
- CLI：`.opencode/tools/openspec.mjs`。命令与 skill 内统一调用
  `node .opencode/tools/openspec.mjs <command>`，不依赖 PATH 上的 `openspec`。
- schema 由 `openspec/schemas/` 提供（CLI 的项目级优先级最高，`templates --json` 会显示 `Source: project`）。
- 工作流：`需求/变更提出 → Spec → 设计/任务拆解 → 实现 → 验证 → 归档`。

### config.yaml 的真实能力边界

产物规则集中在 `openspec/config.yaml`，`openspec/AGENTS.md` 已删除（避免被 `instructions` 重复注入）。

内置 CLI 的 `ProjectConfigSchema` **只解析三个字段**：

| 字段 | 作用 | 校验 |
| --- | --- | --- |
| `schema` | 使用的 workflow schema | 必填，非空字符串 |
| `context` | 注入每个 artifact 的项目背景，上限 50KB | 可选 |
| `rules` | 按 artifact ID 附加的规则数组 | 可选，键必须是合法 artifact ID |

**其他顶层键会被静默丢弃。** 非法 `rules` 键只打印一行 warning 后忽略。
`spec-driven` 的合法 artifact ID 为 `proposal`、`specs`、`design`、`tasks`。
`scripts/check_openspec_config.mjs` 会驱动真实 CLI 检出这两类问题：它用每次唯一的临时 change
做探针（生成的目录名带 pid 与时间戳，不会误删已有用户目录），并在 `finally` 中清理。

### design.md 是必需产物

`schema.yaml` 中 `tasks.requires: [specs, design]`，因此 `design.md` 总是需要生成；
没有真实设计决策时保持精简即可。

### 重新生成 CLI bundle（维护用）

升级 OpenSpec 后，在一台装有 `esbuild` 的机器上执行：

```bash
node scripts/build_openspec_bundle.mjs
```

脚本会把 OpenSpec 打包成单个自包含的 `.mjs`，并内联两处运行时 `require`
（`package.json` 版本、`cross-spawn`），同时屏蔽 CLI 的重复自动执行。
OpenSpec 来源优先级：`NEXUS_OPENSPEC_PACKAGE` → 本地 `node_modules` → 全局 npm root。
普通开发者不需要运行此脚本，直接使用已提交的 bundle；**不要手动改动 bundle 内容及其内部生成变量**。

## Superpowers

- skill：`.opencode/skills/` 下的 15 个 Superpowers skill（`brainstorming`、`writing-plans`、
  `executing-plans`、`subagent-driven-development`、`test-driven-development`、
  `systematic-debugging`、`verification-before-completion`、`requesting-code-review` 等）。
- 命令：`/sp-brainstorm`、`/sp-plan`、`/sp-execute`、`/sp-debug`、`/sp-review`、`/sp-verify`、`/sp-finish`。
- agent：`code-reviewer`（`mode: subagent`，只读），由 `/sp-review` 使用。

### bootstrap 为按需启用

插件 `.opencode/plugins/superpowers.js` 做两件独立的事：

1. **注册 skill** —— 始终执行（V1 由 OpenCode 原生项目发现，V2 由 `ctx.skill.transform()`）。
2. **注入 `using-superpowers` bootstrap** —— 需显式开启。

开启方式：启动 OpenCode 前设置 `NEXUS_SUPERPOWERS_BOOTSTRAP=1`。

默认关闭的原因：bootstrap 会在每轮对话注入约 3KB 通用提示词，并要求"任何响应前先调用 skill"，
使简单改动也被迫走完整的头脑风暴 → 计划 → 子代理流程；与 OpenSpec 的 change 流程重复。
关闭只影响常驻提示词，**skill 仍然注册且可通过原生 `skill` 工具显式调用**，`/sp-*` 命令也不受影响。

注入还需同时满足：工作目录等于仓库根目录，且会话不是子代理会话（基于 session 的 `parentID` 判定，
不依赖模型自觉遵守提示词）。非仓库根目录或子代理会话均不注入。V2 下若运行上下文未提供 `ctx.directory`
（无法确认工作目录），保守不注入；skill 注册不受影响。

## 与 OpenSpec 的一体化

- `AGENTS.md`、`openspec/config.yaml` 和 OpenSpec 命令/skill 说明以 change 的 `tasks.md` 为唯一实施清单。
- `writing-plans`、`executing-plans`、`subagent-driven-development`、`brainstorming` 的入口都带一段 OpenSpec 适配：
  已有合法 change 产物时不再另写计划，checklist 式 `tasks.md` 不匹配只接受 `### Task N` 的
  `task-brief` / `task-start` / SDD ledger 脚本。
- OpenSpec/Superpowers 的命名（`openspec-*`、`opsx-*`、`sp-*`、`code-reviewer`）与项目内既有资源无冲突。

## 验证（干净环境）

以下方式可在不依赖全局安装的情况下复现：

```bash
# 1) 屏蔽外部 skill 后，仍能列出项目内 24 个 skill
OPENCODE_DISABLE_EXTERNAL_SKILLS=1 OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1 opencode debug skill

# 2) 仓库内 bundle 使用项目级 schema
PATH=/usr/bin:/bin node .opencode/tools/openspec.mjs templates --json   # Source: project

# 3) 项目配置与插件回归检查
node scripts/check_openspec_config.mjs
node tests/opencode/test-superpowers-bootstrap.mjs

# 4) 命令 / agent 被发现
opencode agent list
```

## 已知边界与风险

- 根目录 `.opencode/` 与 `opencode.json` 会被**同一 git worktree 内**的所有 OpenCode 进程继承。
- bootstrap 的自动注入只在仓库根、且设置了 `NEXUS_SUPERPOWERS_BOOTSTRAP=1` 时生效；其余会话只注册 skill。
- `opsx-archive` 命令内部使用 `mkdir -p` / `mv`，依赖 OpenCode 的 bash 环境；核心 CLI 全部跨平台（仅依赖 `node`）。
- bundle 是从 `@fission-ai/openspec` 生成的可提交产物，升级时需按上文重新生成；
  升级后必须重跑 `scripts/check_openspec_config.mjs`，因为新版可能新增或改名 config 字段。
