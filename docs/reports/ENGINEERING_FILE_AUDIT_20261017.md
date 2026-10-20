# 工程目录整改后的文件必要性审计

日期：2026-10-17。分支：`cursor/nexus-v1-0-1-phase2-f6f2`。基线：`c046abeda9cb4ade5d891df51e22948fba68f996`。

结论：目录迁移已经落地，但版本兼容、构建路径、部署入口和旧文档尚未收敛。按本次用户指示，**1.2.27 不再作为公司兼容目标**；应退役整条兼容链，同时明确公司统一使用的 OpenCode 与 Workspace UI 版本。

本次只新增本报告和逐文件分类清单，未删除或修改业务代码、用户未跟踪文件、数据与环境配置。

## 覆盖范围与证据强度

- 基线受 Git 管理文件：**6,274 个，146.51 MiB**；每个路径均有分类，见 [逐文件清单](ENGINEERING_FILE_AUDIT_20261017.files.jsonl)。
- `web/workspace-ui/upstream/packages/`：3,319 个文件；版本化 Workspace 静态资源：2,285 个文件。两类合计占受管文件约 89.3%。
- 三个服务：212 个文件；`ops/`：24 个；`tools/`：11 个。
- 本地还存在约 103,710 个未跟踪文件，多数来自旧目录的依赖、构建输出和证据。它们只做目录及角色检查，不逐项读取或删除。
- 人工核读业务入口、导入关系、版本门禁、构建配置、部署脚本和关键文档；第一方前端另外做入口可达性扫描。上游文件按包依赖和文件角色归类，**没有把 6,274 个文件逐行人工审查，也没有证明保留包内的每个文件都是最小依赖**。
- 清单字段：`path` 为文件路径，`decision` 为处理建议，`basis` 对应下文段落编号，`evidence` 区分源码核读、静态引用和模块归类。清单是审计记录，不能直接作为批量删除脚本。

## A. 可清理范围

### A1：退役 1.2.27，涉及 421 个专属文件

| 范围 | 文件数 | 建议 |
|---|---:|---|
| `web/nexus/public/opencode-workspace/1.2.27/` | 415 | 删除旧版发布资源，45.78 MiB |
| `web/workspace-ui/versions/1.2.27/` | 5 | 删除旧版独立适配源码与配置 |
| `.github/workflows/workspace-ui-1.2.27.yml` | 1 | 删除旧版专属构建和推送流水线 |

必须配套收敛，不能只删静态目录：

- `services/task-platform/app/config/settings.py:97,101,235-252`：旧版本白名单、历史 UI 默认版本及对应校验。
- `services/task-platform/app/runtime/opencode_server.py:102-111`：低于最低版本仍可通过白名单放行的分支。
- `services/task-platform/app/api/workspace_routes.py:89`：没有记录实际版本时回落到历史默认版本。
- `services/task-platform/app/runtime/lifecycle.py:443-447`：新 Session 的健康响应没有版本时，也把历史默认值写进快照；这会把未知版本记成已知版本。
- `ops/dev/astra_service.py:191-227`：启动仍要求历史默认版本的 UI 文件存在。
- `test_opencode_server.py:35-45`、`test_session_workspace.py:185-204`、`test_service_gateway.py:159-220`、`test_serve_identity.py:31`：兼容行为断言或旧版正向 fixture。
- `docs/deploy/REMOTE_EMPLOYEES.md:26`：仍要求保留旧 UI 供历史 Session 使用，与本次指示不一致。

`test_remote_provisioning.py:454` 中的 1.2.27 是错误安装版本的负面样例，不代表支持旧版；不应机械删除所有包含该字符串的测试。

历史快照里已记录的真实 OpenCode 版本不能改写成新版本。缺失版本的数据若要修复，应使用可核实的一次性 backfill；无法核实的旧会话应明确显示版本未知或界面已退役。

### A2：统一版本后，1.18.31 也可退役

| 发布目录 | 文件数 | 大小 |
|---|---:|---:|
| `1.2.27` | 415 | 45.78 MiB |
| `1.18.31` | 935 | 32.41 MiB |
| `1.18.33` | 935 | 32.41 MiB |

1.18.31 与 1.18.33 共有 882 个同路径文件，其中 881 个字节完全相同；其余不同名称资源没有因此被认定为重复文件。

公司统一版本后，退役前两版静态资源可减少 **1,350 个文件、78.19 MiB**。取消 1.18.31 支持是进一步的收敛建议，本次没有执行。

现场 `opencode --version` 为 **1.18.35**；仓库 `UPSTREAM.json` 与远程部署默认版本为 **1.18.33**。当前不存在 1.18.35 UI 包，启动校验会明确拒绝。因此不能只升级 OpenCode 可执行文件：统一版本必须同时覆盖上游源码、UI 产物、安装脚本、运行门禁和实际页面验证。本报告没有把 1.18.33 宣称为应采用的最新版本。

### A3：7 个无现用入口的文件

以下 6 个第一方前端文件从 `main.ts` 不可达，仓库搜索也没有发现动态注册入口：

- `web/nexus/src/styles/base.css`
- `web/nexus/src/components/forms/FormField.vue`
- `web/nexus/src/components/forms/FormLabel.vue`（只被上面的不可达 FormField 引用）
- `web/nexus/src/components/runtime/TrendChart.vue`
- `web/nexus/src/components/astra/ErrorDetail.vue`
- `web/nexus/src/components/astra/AstraLogo.vue`

另一个是 `web/workspace-ui/patches/install-korean-ime-fix.sh`：无现用引用，实际内容是下载第三方 OpenCode fork、reset、构建并安装 TUI，与内嵌 Workspace 构建无关。

以上可作为小范围删除项；实施后跑前端类型检查和构建。

### A4：9 个背面角色图片无当前调用

`web/nexus/public/assets/office/characters/character-*-back.png` 共 9 个。`employeeCharacterSource()` 虽然支持 `back`，目前仅有的两个调用方 `EmployeeAvatar.vue` 和 `EmployeeConfig.vue` 都使用默认 `front`。

可以删除背面图片，并同步收敛前端 helper 的参数和资源 manifest。保留 9 个正面图片与来源信息：当前 Worker 头像和角色配置仍使用它们，不能因数字办公室页面退休就删掉整组资源。

## B. 目录整改遗留问题

### B1：必要文件需要修正

| 问题 | 证据 | 影响与建议 |
|---|---|---|
| Workspace 构建输出路径错误 | `web/workspace-ui/vite.config.ts:16`、`tools/e2e/workspace_ui.mjs:71` | cwd 是 `web/workspace-ui`，`../web/nexus/...` 实际解析成 `web/web/nexus/...`；应指向 `../nexus/...`。现有提交产物掩盖了重新构建问题 |
| OpenSpec 仍指向旧业务目录 | `openspec/config.yaml:15` | 注入“新代码放在 nexus/”，与 AGENTS、README 的 `services/*`、`web/nexus` 冲突，需改当前目录约定 |
| 安装器版本规则与 UI 门禁不一致 | `ops/deploy/install_nexus.sh:13,157-158`、`ops/dev/astra_service.py:176-187` | 安装器允许任何 ≥1.18.25 的版本，启动器却需要精确版本 UI；统一受支持版本及产物 |
| 环境文件事实不一致 | `README.md:45,65`、`ops/dev/load_env.sh:7-12`、`astra_service.py:217-218` | 文档称只有 `ops/env/.env`，实现还支持根 `.env` 与 `ENV_FILE`；选定唯一约定并同步 |
| 部署文档配置复制位置错误 | `ops/deploy/DEPLOYMENT_GUIDE.md:140`、`install_nexus.sh:47-48` | 文档示例写到 Agent Service `.env`，安装器读取 `ops/deploy/.env` 或 `ops/env/.env` |
| 开发文档引用旧路径 | `docs/architecture/OPENSPEC_SUPERPOWERS.md:93,101,151-153` | 仍引用 `frontend/node_modules`、已删除的 `scripts/verify.py`、旧 bootstrap 测试位置；应改真实路径 |
| Agent Service CI 未执行测试 | `.github/workflows/ci.yml:51-54` | 当前只安装及 import，未覆盖已有 `services/agent-service/tests/` |

### B2：部署存在两套服务定义和重复启动

`ops/deploy/systemd/` 的 **11 个静态 unit** 没有被安装器读取；`install_nexus.sh:254-272` 自己动态生成另一套 unit。它们不是两份都必须保留的运行事实源，应选择一套作为部署入口，再退休另一套。

另有重复启动风险：

- 安装器 `:296` 先运行 `astra_service.py dev restart`，该 supervisor 已纳管 Task MCP 和 Web。
- 安装器 `:299/302` 再启动 Task MCP，`:319-320` 再启动 Web。
- 证据：`astra_service.py:392-397,457` 及核心服务列表。

这可能造成 8202、5174 端口争抢和管理归属不一致。属于源码已确认的重复启动路径；本轮没有运行安装器来复现冲突。

## C. 应保留或先解耦的第一方文件

### C1：Alembic 迁移

保留 `services/task-platform/alembic/` 和 `alembic.ini`。目前有 **37 个版本文件**，其中包含分支合并修订。虽然名称带 Astra 历史版本，但它们仍组成新库建表和现有库升级链，不能当历史垃圾删除。本轮未执行数据库迁移。

### C2：平台助手与复盘仍耦合

`services/task-platform/app/assistant/` 的 17 个文件仍有真实调用：

- `app/main.py:15-16,44-59,83`：导入、启动、停止和挂载助手路由。
- `app/api/routes.py:15`：使用 `assistant.review`。
- `app/runtime/celery_app.py:5,32,78-80`：定期执行任务复盘。
- `ops/dev/astra_service.py:65`、`ops/dev/preflight.py:361`：助手 Server 的外部进程入口。
- 根 `opencode.json`、`.opencode/agents/platform-assistant.md` 和经验复盘 skill 也有现用关联。

前端没有助手页面不等于这些文件可以删除。如需退休旧助手，应先保留并解耦任务复盘依赖，再收敛 API、进程、配置与测试；不是本次取消 1.2.27 的必需范围。

### C3：CLI 是孤立实现与残留配置，需一起判断

`app/executors/opencode_cli.py`、`opencode_remote.py`、`runtime/semantic_events.py` 没有进入当前 Server 正式链路，主要被相互导入和测试直接使用。`runtime/session_connection.py` 使用本地/远程 Server。

但 `AGENTS.md` 仍明确保留独立 CLI fallback，`settings.py:231-232` 和 `ops/dev/preflight.py:313-319` 也仍接受该选项。不能借取消 1.2.27 把此独立契约一起删除。后续应核实 fallback 的实际入口，收敛失效配置，或通过单独契约变更整体退休。

### C4：前端及资源

`web/nexus/src` 共 77 个文件，静态导入图中从 `main.ts` 可达 71 个，其余 6 个见 A3。可达只证明文件有依赖入口，不保证每个导出或每段代码都必要。

`web/nexus` 的入口、router、API、页面、状态管理、构建及类型配置保留。Worker 配置仍引用角色、区域、任务映射相关文件，不能按旧目录名 `features/workspace` 直接删除。

### C5：测试与验证脚本

保留现用回归测试、真实环境 opt-in 验证和 Workspace/OpenSpec 构建检查。测试不是生产运行文件，但对安全清理仍有用途。未进入 CI 的 E2E 不等于无用，也不能宣称当前 CI 执行了它。

### C6：运维入口

`ops/dev/` 9 个文件形成 shell 入口→环境加载→Python supervisor/controller→进程管理的实际调用链，`preflight.py` 也被启动调用。`tools/llm_wiki/` 是知识库部署运维入口。保留，修正 B1/B2 的冲突。

### C7/C8：项目规则与服务模块

保留 AGENTS、当前说明文档、三服务源码、Task/Worker/Runtime/网关/图书馆及依赖配置。对服务文件的保留建议以当前包角色和主要调用链为依据，不等于逐个函数已完成死代码分析。Python 包的空 `__init__.py` 不能因为静态导入图没有显式引用就删除。

## D. Workspace 上游可进一步缩减

### D1：必须保留的构建主体

保留 `src/adapter.tsx`、入口、构建配置、`UPSTREAM.json`、锁文件、`upstream/LICENSE`，以及 `app`、`ui`、`sdk/js`、`schema`、`session-ui` 包主体。

`app/vendor/opencode-ai-client-1.17.13-v2.tgz` 被 `app/package.json:57` 和 `session-ui/package.json:41` 显式依赖。其名称中的版本号不是 1.2.27 兼容证据，不能按旧版本名称删除。

内嵌适配器的会话管理限制仍用于避免用户绕过平台新建、关闭、归档 Session；这些改动不是旧版本兼容补丁。

### D2：core 服务端依赖可以裁剪，不能直接删目录

`core` 共 493 个文件，UI 当前只引用 `src/util/{array,binary,encode,path,retry}.ts`，五个文件均无进一步导入。例：`app/src/app.tsx:23`、`app/src/components/prompt-input/submit.ts:3-4`、`session-ui/src/components/session-turn.tsx:12-13`。

可保留这五份原始 util 和裁剪后的 `core/package.json`，移除无关 Server 实现，并清退仅由它拉入的：

- `effect-drizzle-sqlite`：26 个文件。
- `effect-sqlite-node`：4 个文件。
- `llm`：152 个文件。
- `plugin`：42 个文件。
- `http-recorder`：24 个文件。

后五包合计 248 个文件。按上述保留边界，**735 个文件属于后续可裁剪候选**；必须同步 `package.json`、`pnpm-workspace.yaml`、`UPSTREAM.json`、锁文件、catalog、服务端专属 patches，完成干净安装和 Workspace 构建后才能确认最终删除集合。

19 个 `.patch` 均被 manifest 显式声明，不能按文件名直接删除。UI 需要的 patches 保留；AI SDK、npm、原生数据库等服务端依赖相关 patches 随实际依赖闭包一起裁剪。

### D3：上游开发资料

保留包中的 tests、E2E、README、AGENTS 等按上游开发资料分类。可从生产发布包排除；没有据此声称这些文件全部是运行或当前构建所必需，也没有在本轮验证其全部测试。

### D4：当前发行资源

1.18.33 的 935 个文件与现行 `UPSTREAM.json` 对应，当前先保留。若统一升级为其他版本，应构建并验证新资源后整体替换，避免逐个手工删生成 chunk、字体或动态资源。

## E. 历史资料与本地残留

### E1/E2：归档，不混作当前使用说明

历史产品需求、原型、Astra 设计与验收记录可归档，不需要进入生产运行包。原始需求 README 明确保留原始资料；`openspec/specs/` 是 AGENTS 指定保留的历史契约，不修改或当死代码删除。

`docs/product/USER_GUIDE.md` 仍是 Astra V0.4，使用 5173 并引用已不存在的 `scripts/setup-windows.ps1`；`implementation-brief.md`、旧 design 和 `runtime-final-state.md` 应明确历史身份，避免当作 Nexus 当前操作规范。

`tools/e2e/opencode_server_poc.py` 是早期 HTTP/SSE 验证脚本，无当前 CI/启动引用，列为归档候选。

### E3：开发工作流

`.opencode/`、`opencode.json`、OpenSpec schema/commands/skills/bundle 是 AGENTS 要求保留的开发能力。它们不全是生产必需文件；应通过发布清单区分开发工具与实际复盘运行依赖，不应统一删除。

### 本地旧目录

根 `apps/`、`backend/`、`frontend/`、`nexus/`、`workspace-ui/` 已没有受管的当前业务源码，但磁盘目录尚存：

| 类型 | 内容 | 处理建议 |
|---|---|---|
| 可重建依赖与缓存 | 旧 `node_modules`、dist、Python 缓存、测试输出 | 核实无进程使用后清理；旧路径不再被当前 .gitignore 完整覆盖，导致大量未跟踪噪声 |
| 需要保护的本地状态 | `backend/data`、备份、`.astra-tasks`、嵌套 Git 工作区、旧配置和环境 | 先核实归属、进程、备份及迁移，不能整目录删除 |
| 验证资料 | `artifacts/`、`evidence/`、未跟踪 QA 报告及 handoff | 用户现存资料，评估保留价值后统一到 `docs/reports/`；本轮不删除 |

只含缓存的空业务目录不代表当前仍维护第二套产品。也不能因为它未被 Git 跟踪，就认定其中数据库、工作区和凭证可丢弃。

## 验证结果与剩余风险

已执行：

1. 当前分支、提交、Git 状态与全部受管文件清单核对。
2. 前端静态导入图：77 个源文件、71 个可达、6 个不可达，并搜索复核动态注册。
3. 三版 Workspace 文件数及大小统计；1.18.31/1.18.33 同路径文件 SHA-256 比较。
4. 使用项目 Python 环境实际调用 `ensure_workspace_ui_bundle('1.18.35')`，确认当前版本因缺少精确 UI 资源被拒绝；没有启动或停止服务。
5. 解析当前 Workspace build outDir，确认错误落点是 `web/web/nexus/public/...`。
6. 针对性测试 **4 passed**：版本门禁、历史会话默认 UI、已记录版本的 UI、缺失 UI 不回落 SPA。它们证明现状仍依赖旧兼容行为，不代表取消兼容已经完成。

测试命令：

```bash
cd services/task-platform
.venv/bin/pytest -q \
  tests/test_opencode_server.py::test_version_check_is_enforced_by_default_and_can_be_disabled \
  tests/test_session_workspace.py::test_workspace_uses_default_version_for_historical_session \
  tests/test_session_workspace.py::test_workspace_uses_runtime_session_opencode_version \
  tests/test_session_workspace.py::test_missing_workspace_bundle_is_not_served_as_the_spa
```

未执行：源码清理、OpenCode 升级、数据库迁移、Workspace 干净安装/构建、前端构建、浏览器 E2E、远程部署与安装器端口冲突复现。本轮没有修改业务代码，所以没有用无关全量测试替代上述未验证项。

后续实施建议先完成 1.2.27 整链退役和版本资源闭合，再做孤儿文件删除、部署入口收敛和上游依赖裁剪。取消兼容涉及既有行为与配置契约，应按 AGENTS 使用一个 OpenSpec change 的 tasks.md 实施；本报告是审计依据，不是第二份实施计划。
