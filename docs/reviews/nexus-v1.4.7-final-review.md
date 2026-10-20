# Nexus v1.4.7 二次复审与整改报告

- 复审日期：2026-10-20
- 分支：`codex/nexus-v1.4.7`（HEAD `b17914cca`，未在 master 开发）
- 复审方式：按《Nexus v1.4.7 问题整改需求与验收清单》19 项，逐项核对 Gemini 首轮代码与实际运行逻辑；以测试与运行证据判定，不采信完成报告。
- 说明：管理页面统一 UI 规范（DeepSeek 前序完成）本轮只做回归，不重复整改。

## 一、结论摘要

| 分类 | 数量 | 说明 |
|---|---|---|
| A 已正确实现 | 15 | 保留，补回归验证 |
| B 部分实现 | 3 | 12 工具/MCP 展示、17 MCP 维护入口、19 技能与 MCP 绑定 |
| C 实现错误 | 1 | 14 对话重复的“内容去重”修复本身违反需求（已整改） |
| 本轮新发现并修复 | 3 | 14 去重误杀、19 技能运行时不生效、09 透明路由健康判定用错模型 |

核心根因：

1. **对话重复**：Gemini 在前端/群时间线加了“相同发送人 + 相同文本 + 时间窗”的内容去重，把“连续发两条相同文字”折叠成一条——这不是修复，是掩盖 id 不一致。真正根因（Nexus message id 未透传给 AgentScope）已由 Gemini 修好，因此内容去重必须删除。
2. **员工技能运行时不生效**：`skills` 只存进 Nexus SQLite，创建 AgentScope 会话时从未调用 `POST /workspace/skill/from-library` 安装；`capability` 又用技能库记录 id 去比对工作区显示名，导致已装也报“未发现”。
3. **透明路由的健康判定错误**：`health_summary` 用客户端请求模型 `route.model` 判断节点是否支持，`GLM-5.2 → GLM-5.3` 会被判成“不可服务”，而数据面实际按 `target_model` 选节点。
4. **统计窗口**：调用趋势原先固定 1h，Gemini 只删了卡片里的窗口切换，趋势没有跟随全局窗口。

## 二、19 项对照表

| 编号 | Gemini 实现情况 | 实际问题 | 本次修改 | 验证结果 | 状态 |
|---|---|---|---|---|---|
| 01 指标采集/计算 | 已实现 | `_frame_has_content` 已覆盖 reasoning/tool_calls；TPS 仅流式成功且 >1 token 才计算 | 保留；补跑单测 | `pytest ... -k "tps or frame_has_content"` 4 passed | 通过 |
| 02 统计窗口统一 | 部分实现 | 只删了趋势卡片内的切换，趋势仍固定 `1h`，与顶部窗口割裂 | 趋势接口改为 `hour/today/7d/30d`，前端趋势跟随 `statsWindow`，删除 `trendWindow` | `test_gateway_health_summary.py` 35 passed；type-check 通过 | 通过（见风险：滚动窗口与自然日边界不同） |
| 03 卡片对齐/分隔线 | 已实现 | 节点表/最近事件对齐与分隔线 | 保留 | 代码核对 | 通过（未浏览器验收） |
| 04 页面滚动异常 | 部分实现 | 配置中心/网关的卡片高度链缺 `min-height:0`，长列表把卡片撑到内容高度后被 `.page` 裁掉，卡片 body 没有可滚动区域（数字员工管理复现：body clientHeight=1838、scrollHeight=1838，无法滚动） | 补齐 `.settings-workspace/.settings-content/.settings-aside` 与 `.gateway-workspace/.gateway-content/.gateway-aside` 的 `min-height:0`，卡片高度被视口约束、body 成为唯一滚动容器 | headless Chrome 结构复现：修复前 `canScroll:false`，修复后 card=649、body client=584/scroll=1838、`scrollTop` 可到 1254；网关壳、顶层 `admin-card` 同样验证通过 | 通过 |
| 05 冗余文案 | 已实现 | 统计窗口说明、延迟趋势说明 | 保留删除结果 | 代码核对 | 通过 |
| 06 调用日志内容 | 部分实现 | 展开详情已结构化，但查询区仍有 4 个低频条件 | 保留结构化详情；删除 Key/资源池/状态码/错误码与「更多筛选」 | 代码核对 + build | 通过 |
| 07 上游节点页 | 已实现 | 列拆分、状态分列 | 保留 | 代码核对 | 通过 |
| 08 资源池/路由拆分 | 部分实现 | 已拆成两个路由页，但仍遗留未挂路由的合并页 `GatewayPoolsRoutes.vue` | 删除该死页与仅它使用的 `AstraFilterPanel.vue` 及对应 CSS | 无引用；build 通过 | 通过 |
| 09 透明模型路由 | 部分实现 | 转发已实现且有测试，但健康汇总按请求模型判断节点能力 | 新增控制面保存/修改/清除测试；`health_summary` 改用 `target_model` 判断节点支持 | `test_transparent_model_routing_a_to_b`、`test_route_target_model_can_be_saved_and_cleared`、`test_transparent_route_uses_target_model_for_node_support` 通过 | 通过（见风险：流式 SSE 未回写 model） |
| 10 配置中心布局 | 已实现 | 统一卡片标题/刷新/新增 | 保留 | type-check/build | 通过 |
| 11 员工表格表头 | 已实现 | 原生 el-table，含“操作”表头 | 保留 | 代码核对 | 通过 |
| 12 工具名称辨识 | 部分实现 | 内置工具名已友好化；MCP 工具仍不在选择目录，名称/描述为硬编码 | 保留现状，报告列为缺口 | 代码核对 | 部分通过 |
| 13 去掉常驻详情 | 已实现 | 改为“查看”抽屉 | 保留 | 代码核对 | 通过 |
| 14 消息重复 | 实现错误 | 内容指纹去重会折叠用户连发的相同消息（违反验收 4） | 删除前端 30s 内容去重与群时间线指纹去重，只按 id 合并；新增“两条相同消息都保留”测试 | 前端 17 passed；`test_groups.py`/`test_im.py` 24 passed；AgentScope `ChatRequest` 契约校验保留客户端 id | 通过（无真实网关联调，见风险） |
| 15 OpenAI 默认模型 | 已实现 | 模型选项仅来自网关路由，无写死项 | 保留 | 代码核对 | 通过 |
| 16 LLM Wiki 远端 | 已实现 | URL 仅允许 127.0.0.1 | 保留 Gemini 的 http(s)+任意 host 校验；健康探针走真实 HTTP | `test_llm_wiki.py` 通过（含远端/HTTPS 用例） | 通过（配置为环境变量，需重启生效） |
| 17 MCP 维护入口 | 部分实现 | 页面只内嵌官方 AgentScope UI；完成报告声称的“快速指引卡片”在代码中不存在 | 保留官方 UI 作为注册/编辑/删除/测试入口，报告如实记录 | 代码核对 | 部分通过 |
| 18 数据来源/归属 | 已核验 | 无代码变更，边界在 AGENTS/架构文档中 | 保留 | 文档核对 | 通过 |
| 19 Skill/MCP 运行时 | 部分实现 | 技能从未安装进会话；capability 用 id 比名称 | 新增 `POST /workspace/skill/from-library` 同步（邮箱/私聊/群会话/rebind/IM），PATCH 回同步全部已绑定会话；capability 先做 id→name 解析 | 新增 `test_employee_skills.py` 7 passed；nexus-api 91 passed | 部分通过（技能已生效；MCP 员工级绑定仍未实现） |

## 二·补、复审后追加修复（2026-10-20）

**现象**：`el-card` 内容超长时上下滑动失效，例如「数字员工管理」。

**根因**：设置页/网关页的高度链是 `.page → .settings-page(ElContainer) → .settings-workspace(ElContainer) → .settings-content(ElMain) → el-card → el-card__body`。`.page > *` 有 `min-height:0`，但 `.settings-workspace`、`.settings-content` 仍是默认 `min-height:auto`，卡片被内容撑到 1903px，外层 `.page` 又 `overflow:hidden`，于是整块被裁掉、body 没有滚动条。headless Chrome 复现：`body.clientHeight = scrollHeight = 1838`，`canScroll:false`。

**修改**：
- `web/nexus/src/views/SettingsView.vue`：`.settings-workspace`、`.settings-content`、`.settings-aside` 增加 `min-height: 0`。
- `web/nexus/src/views/gateway/GatewayView.vue`：`.gateway-workspace`、`.gateway-content`、`.gateway-aside`、`.gateway-page > .gateway-workspace` 增加 `min-height: 0`。

**验证**（headless Chrome，1440×900，30 行数据）：
- 修复前：`.settings-shell` 高 1903px、body `clientHeight=scrollHeight=1838`、`scrollTop` 恒为 0。
- 修复后：`.settings-shell` 高 649px、body `clientHeight=584 / scrollHeight=1838`、`scrollTop` 可滚到 1254，表头与卡片标题保持固定。
- 网关壳同样从“卡片被撑高”变为 card=649、可滚动；顶层 `admin-card`（任务大厅/图书馆）验证为 card=649、`canScroll:true`。

## 二·补二、网关运行总览重设计（2026-10-20）

**现象**：页面由「状态条 + 6 张 KPI 卡 + 整行趋势 + 节点表/事件两栏 + 折叠指标」堆叠。正常状态下状态条没有容器，只剩一行文字和一个悬空按钮；6 张 KPI 卡标签长度不一（「首 Token 延迟（TTFT）」换行），数值基线错位；「最近事件」一次加载 40 条，和 3 行节点表并排时把整行撑到 2000px，节点表被拉出大片空白；趋势独占整行，首屏信息密度失衡。

**调整**（`web/nexus/src/views/gateway/GatewayOverview.vue`）：

1. 状态条改为按状态着色的卡片（正常/降级/不可用/未知），左侧状态胶囊、中间影响范围与原因、右侧「查看异常」。
2. 6 个 KPI 合并为一条带分隔线的指标带：标签单行省略、数值统一 26px/32px 基线、窗口标签固定在底部；6/3/2 列自适应，多行时补行分隔线。
3. 「节点与资源池 + 最近事件」并排：事件列表限高 316px 内部滚动，两张卡片等高；表格列宽收紧以适配分栏。
4. 「调用趋势」独占整行，趋势摘要移入卡片标题副行，分组/序列选择器留在右侧。
5. 网关壳默认会去掉内层卡片边框与左右内边距，总览页面内恢复 1px 边框、12px 圆角与 16px 内边距，让分区块成为可辨识的卡片。
6. 「查看异常」除过滤外，还会滚动定位到节点卡片。

**验证**：headless Chrome 按真实 DOM/CSS 渲染 1440 与 1280 两档（截图见 `artifacts/ui-check/overview-final-1440.png`、`overview-final-1280.png`），`npm run type-check`、`npm run build` 通过。

### KPI 口径统一（2026-10-20 追加）

原 6 个指标混用了三种时间维度：请求数/TTFT/输出速度跟随全局窗口，失败率固定「近 5 分钟」、QPS 固定「近 1 分钟」、并发实时。页面选「本小时/今日」时，QPS 仍显示 1 分钟值，和窗口对不上。

按 RED + LLM + 饱和度重新定义，并统一时间维度：

| 指标 | 维度 | 口径 |
|---|---|---|
| 请求数 | 所选窗口 | 窗口内已落库调用数 |
| 请求失败率 | 所选窗口 | errors / (success + errors)，与趋势、日志同源 |
| 平均 QPS | 所选窗口 | 窗口内请求数 ÷ 窗口秒数 |
| 首 Token 延迟 P95（TTFT） | 所选窗口 | 流式成功请求的 P95（平均值放进提示） |
| 输出速度 | 所选窗口 | 流式成功请求的 Token/s 平均 |
| 当前并发 | 实时 | 运行态瞬时值，不受窗口影响 |

- 时延用 P95 而非平均，符合网关 SLO 的常规做法；吞吐用平均。
- 只有「当前并发」保留实时维度，并在卡片上标注「实时」。
- 失败率不再取健康摘要的 5 分钟值，避免同一排数字出现两套时间口径。

验证：`npm run type-check`、`npm run build` 通过；1440/1280 渲染确认 6 格标签不截断。

### 上游节点详情页重设计（2026-10-20 追加）

**原问题**：详情页在网关壳里又套了一层 `ElContainer/ElHeader/ElMain`，自己滚动，和壳的 body 形成双滚动；壳标题「上游节点」与页面标题重复；「概况/用量/运维」三个分段下又各自重复 `<h3>用量</h3>` 之类的小节标题；内层卡片被壳去掉边框后成了散块；「概况」只有节点信息和最近探测两张卡，页面大半空白。

**新结构**（`web/nexus/src/views/gateway/GatewayNodeDetailView.vue`）：

1. 身份卡：返回节点列表、节点名、节点 ID、上游地址，右侧「编辑 / 更多」。
2. 服务状态卡：按「可以接请求 / 降级 / 不能接 / 未知」着色，展示结论与健康、调度、配置、资源池、最近探测。
3. 分段导航：概况 / 用量 / 运维；切到用量时右侧出现统计窗口，不再在小节标题里重复窗口控件。
4. 概况 = 节点信息 + 最近一次探测 + 探测历史（从运维移入，填满页面）。
5. 用量 = 指标卡 + Token 趋势；运维 = 限制、健康状态、调用日志、配置；日志刷新按钮移入日志卡片头。
6. 删除页面自身的高度与滚动声明，统一由网关壳 body 滚动；恢复内层卡片 1px 边框、12px 圆角与 16px 内边距。

**验证**：`npm run type-check`、`npm run build` 通过；headless Chrome 1440 渲染核对概况/用量两页（`artifacts/ui-check/node-detail-summary.png`、`node-detail-usage.png`）。

### 表单解释文案改为字段提示（2026-10-20 追加）

**问题**：数字员工创建/编辑抽屉里，职责说明、工作指令、模型路由、知识、技能、工具六个字段下方各跟一行灰色解释；其它表单也有同类平铺说明（网关节点、模型路由、任务分类、任务创建、知识详情、任务详情）。

**调整**：统一改为字段标签后的信息图标（`components/forms/FieldHelp.vue`，悬停/聚焦显示），不再占用表单纵向空间：

| 位置 | 字段 / 区块 |
|---|---|
| `components/EmployeeDrawer.vue` | 职责说明、工作指令、模型路由、知识、技能、工具 |
| `components/task/TaskCreateDrawer.vue` | 责任员工（并删除「Worker 与责任员工」整段说明） |
| `views/settings/CategoryEditorDrawer.vue` | 排序 |
| `views/gateway/GatewayNodeDrawer.vue` | Base URL、上游凭据、探测项目、Token 额度、调度状态（并删除表单顶部生命周期说明） |
| `views/gateway/GatewayRoutes.vue` | 目标模型 |
| `views/gateway/GatewayNodeDetailView.vue` | Token 额度说明 |
| `views/library/KnowledgeDetailDialog.vue` | 来源证据、目标目录、关联任务 |
| `views/TaskDetailView.vue` | 关联经验 |

保留：条件性错误/警告文案、一次性 Key 的「关闭后无法再次查看」提示、知识编辑器顶部随状态变化的保存语义说明——这些是操作结果或状态，不是字段解释。

**验证**：`npm run type-check`、`npm run build` 通过。

## 三、本轮实际修改

### 后端（task-platform）

- `app/gateway/health_summary.py`
  - 趋势窗口：`TrendWindow = hour/today/7d/30d`，`TREND_WINDOWS` 固定桶宽（300s/3600s/21600s/86400s），`build_trends` 与服务层统一使用。
  - 透明路由：`RouteFact.target_model` + `route_model()`；节点支持、可调度、弱化原因全部按实际模型判断；对外 `impact.model` 仍展示请求模型。
- `app/api/gateway_routes.py`：`/trends` 接受四档窗口，默认 `today`。
- `tests/test_gateway_health_summary.py`：四档窗口参数化测试；透明路由可服务测试；P95 测试窗口更新。
- `tests/test_gateway_control.py`：`target_model` 创建→修改→显式清除的控制面测试；`create_route` 支持额外字段。
- `app/config/settings.py`（Gemini 原改动，保留）：LLM Wiki URL 放开 http(s)/远端 host。
- `app/gateway/server.py`、`observability.py`、`service.py`、`schemas.py`、`models.py`（Gemini 原改动，保留）：透明路由转发、`actual_model` 落库、TTFT/TPS 口径。

### 后端（nexus-api）

- `nexus_api/agentscope.py`：新增 `add_workspace_skills_from_library()`（`POST /workspace/skill/from-library`，解析 `failed`）。
- `nexus_api/main.py`：新增 `sync_employee_skills()`；在邮箱、群成员、私聊首次、rebind、IM 私聊、群记录会话创建后安装技能；员工 PATCH 技能变化后对邮箱 + 私聊 + 群成员会话全部回同步（使用 `session_ids()`，本轮修正了只覆盖私聊的缺口）。
- `nexus_api/capability.py`：`GET /skill` 的 id→name 映射后再比对工作区；任务技能兼容 id/名称两种别名。
- `nexus_api/groups.py`：删除群时间线“发送人+文本+分钟”指纹去重，只按 message id 去重。
- `tests/test_employee_skills.py`（新增 7 个用例）、`tests/test_groups.py`（新增 2 个用例）。

### 前端

- `features/chat/sessionState.ts`：删除内容/时间窗去重，只按 id 合并。
- `tests/chat-session-state.test.ts`：替换 Gemini 的“内容相同即去重”测试为“两条相同消息都保留”“同 id 覆盖”。
- `api/gateway.ts`、`views/gateway/GatewayOverview.vue`：趋势窗口类型与请求改为全局 `statsWindow`，删除 `trendWindow` 及相关提示/样式。
- 删除 `views/gateway/GatewayPoolsRoutes.vue`、`components/astra/AstraFilterPanel.vue` 及 `styles/page-layout.css` 中仅它们使用的 `.astra-filter*` 规则。
- 前序 UI 规范改造（数字员工、Worker、任务、分类、网关六页、图书馆、待处理、任务大厅）保留，本轮未重做。

## 四、验证

### 运行的命令与结果

```bash
# task-platform：网关 + LLM Wiki（本轮改动集中区）
cd services/task-platform
.venv/bin/pytest tests/test_gateway_data_plane.py tests/test_gateway_control.py \
  tests/test_gateway_health_summary.py tests/test_gateway_observability.py tests/test_llm_wiki.py -q
# => 172 passed, 4 skipped

.venv/bin/ruff check app tests
# => All checks passed!

# nexus-api
cd /Users/niuniu/程序/Astra
PYTHONPATH=services/nexus-api services/nexus-api/.venv/bin/pytest services/nexus-api/tests -q
# => 91 passed, 4 skipped
services/nexus-api/.venv/bin/ruff check services/nexus-api/nexus_api services/nexus-api/tests
# => All checks passed!

# web
cd web/nexus
npm test          # => 17 passed
npm run type-check  # => 0 errors
npm run build       # => built successfully
```

### 关键场景证据

| 场景 | 证据 |
|---|---|
| GLM-5.2 客户端请求实际以 GLM-5.3 转发 | `test_transparent_model_routing_a_to_b`：断言上游请求体 `model == GLM-5.3`、响应体 `model == GLM-5.2`、日志 `model/actual_model` 正确 |
| 透明路由配置可保存/修改/清除 | `test_route_target_model_can_be_saved_and_cleared` |
| 透明路由节点能力判定 | `test_transparent_route_uses_target_model_for_node_support` |
| 指标口径 | `test_output_tps_calculation_rules`（流式成功/单 token/中断/非流式）、`test_frame_has_content_supports_reasoning_and_tool_calls` |
| 统计窗口 | 四档窗口参数化测试（桶宽 300/3600/21600/86400，桶数 12/24/28/30） |
| 消息 id 契约 | 用 AgentScope 2.0.10 的 `ChatRequest` 校验 Nexus 载荷，`input.id` 原样保留；`upsert_message` 按 id 覆盖 |
| 两条相同消息 | 前端 `mergeMessages keeps two identical messages...`；后端 `test_timeline_keeps_two_identical_messages_sent_in_the_same_minute` |
| 技能安装 | `test_employee_skills.py`：from-library 的 skill_ids、filter 任务技能、PATCH 回同步、失败不 500、capability id→name |

### 全量测试中的既有失败（与本次改动无关）

非网关测试文件存在 10 个失败文件（channel/opencode/worker 相关）。在 `HEAD b17914cca` 的独立 worktree 上复跑同样文件，失败数量与用例完全一致（如 `test_channel_switch` 1 failed、`test_employee_serve` 4 failed、`test_phase4_channel` 2 failed），确认是分支既有/环境问题，不是本轮引入。全量串跑还会因沙箱 `Bad system call` 中断，故以分组测试为准。

### 环境限制（未真实联调）

- 本机 AgentScope 8000 可达，但技能库为空（`GET /skill` 返回 `[]`），无法真实验证“安装技能后对话生效”；`/workspace/skill/from-library` 按 2.0.10 源码 + MockTransport 验证。
- 网关数据面 18110 未运行，无法做真实 `GLM-5.2 → GLM-5.3` 调用与页面视觉验收；透明路由以数据面测试（MockUpstream 捕获真实请求体）为准。
- 为避免污染用户数据，未向运行中的会话发送真实消息；消息去重以契约校验 + 前端/后端测试为准。

## 五、数据库与配置

- 迁移 `services/task-platform/alembic/versions/0049_gateway_target_model.py`：为 `gateway_routes.target_model`、`gateway_requests.actual_model` 加列。
- 本地 `services/task-platform/data/nexus.db` 已是 `0049 (head)`，两列存在。
- 该迁移文件目前是**未跟踪文件**，提交时必须 `git add`，部署时需执行 `alembic upgrade head`。
- 本轮没有新增迁移，没有修改历史数据。

## 六、未解决风险

1. **流式响应的 model 字段**：请求体已重写为实际模型，但上游 SSE chunk 里的 `"model"` 仍是实际模型；非流式会回写为请求模型。需要时可在 SSE 层做分帧改写。
2. **趋势窗口边界**：趋势采用滚动窗口（近 24h/7d/30d），统计卡片按自然日/自然小时计算，同一档位边界不完全一致（选择器已统一）。
3. **MCP 员工级绑定**：MCP 的注册/编辑/停用/连接测试由内嵌 AgentScope 官方 UI 提供；Nexus 侧未提供员工选择 MCP 工具并装入各会话的能力，MCP 工具也不会出现在员工工具选择器。
4. **技能安装成本与依赖**：`from-library` 逐会话安装，会话多时 PATCH 变慢；技能必须仍能从原 hub 下载，否则返回 failed。
5. **运行中的 Nexus API 需要重启**才会加载本轮 nexus-api 改动；当前进程只包含 Gemini 首轮代码。
6. **视觉验收缺口**：网关总览/节点页/调用日志仅代码与构建验证，没有浏览器截图（网关服务未启动）。

## 七、Git 状态

- 分支：`codex/nexus-v1.4.7`
- HEAD：`b17914cca fix(worker): harden maintenance recovery and deployment checks`
- 未提交改动：88 项（Gemini 首轮后端/前端改动 + 本轮整改）；未 commit、未 checkout、未合并 master、未清理数据库。
- 未跟踪但必须纳入提交：`services/task-platform/alembic/versions/0049_gateway_target_model.py`、`services/nexus-api/tests/test_employee_skills.py`、`web/nexus/src/views/gateway/GatewayPools.vue`、`web/nexus/src/views/gateway/GatewayRoutes.vue`、`web/nexus/src/views/settings/SettingsHeaderActions.vue`、本报告。
