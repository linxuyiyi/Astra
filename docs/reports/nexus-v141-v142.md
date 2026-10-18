# Nexus v1.4.1 / v1.4.2 验收报告

## 提交链

- 基线：`77f18dceb`
- v1.4.1 首个提交：`0a6ac3c69`（导航、统一聊天、配置中心、文案与任务筛选）
- v1.4.1 布局/流式/官方 UI 修复：`eb22e13de`（顶部五入口、满高工作台、官方 AgentScope UI、原生 Hub、快速切换修复）
- v1.4.2：本次网关提交（六主指标、requests/errors 趋势、节点/事件/折叠明细、设计稿）

## 原因

v1.4.0 的底部下拉导航割裂主入口，私聊/群聊两套页面，配置入口分散，网关用 P95 冒充平均口径；AgentScope 缺少真正可用的管理 UI。v1.4.1/v1.4.2 在不重建业务状态机的前提下收口导航、聊天、配置和网关展示。

## 主要改动

### v1.4.1

- 顶部五入口：数字员工、任务大厅、数字图书馆、API 网关、配置中心；无底部 dock、无下拉。
- `.app` 使用 `100dvh` + flex，聊天三栏、任务/Worker/网关/设置页框撑满视口；窄屏整页滚动。
- 删除重复大标题；`AstraPageHeader` 的 `hide-title` 保留 actions/default 插槽与 `aria-label`。
- 左侧会话栏搜索旁常驻“＋ 新建群组”；群组编辑仍在当前群组操作区。
- 统一 `DigitalEmployeeChatView`：联系人/群组共用列表、消息、输入附件、右侧 Tab；两个适配器对接原 API/Session/流。
- 会话隔离：草稿、附件、消息、流式前缀、notice、AbortController、generation token；群组成员流 `after` 游标、私聊游标跨 follow 保留。
- 右侧任务：联系人按 `source_session_id`/`source_conversation_id`，群组按 `source_channel_id` 精确筛选。
- `contact_employee` 结构化结果；聊天只显示轻量状态，原始结果折叠。
- 配置中心/Gateway 右侧单张 `ElCard.content-shell`；子页去掉包裹卡，内部用 toolbar/分区/分隔线/表格。
- 官方 AgentScope v2.0.10 Web UI 嵌入：`/agentscope-ui/` 静态产物、`/agentscope-api/` 同源代理、`X-User-ID` 来自 `/api/v1/runtime`；Skill/MCP Hub 使用原生 `ClawSkillHub`/`GitHubMCPHub`。

### v1.4.2

- 六主指标：请求数、失败率、QPS、TTFT 平均、输出速度平均、当前并发；各自标注真实窗口。
- 失败率近 5 分钟、QPS 近 1 分钟、并发实时；请求数/TTFT/TPS 使用 `/stats` 所选窗口。
- 趋势使用 `/trends` requests/errors；普通 UI 不展示 P95；延迟均值趋势作为版本限制记录。
- 节点默认全部、异常优先；事件时间倒序并可下钻日志；辅助指标折叠，连接/TTFB 使用平均值。
- null 区分无请求/无采样/接口失败/无权限；加载中不填 0。
- 节点详情主区平均值，p50/p95 收高级详情。
- 设计稿 `docs/design/nexus-v142-gateway-preview.html` 同步顶部五入口、无大标题、单内容卡。

## 官方 AgentScope UI

- 上游：`agentscope-ai/agentscope` v2.0.10，commit `e6c014f0be49b2a6f6f62a9453082a0b975484e1`。
- 参考快照 `src` 与官方 tag 对比无差异；本地额外文件仅为 `dist`/`vite.local.config.ts`。
- 构建脚本 `ops/deploy/build_agentscope_web_ui.sh` 从官方 tag 克隆、校验 commit、`pnpm install --frozen-lockfile`、应用 router basename/API URL helper/上下文种子三处补丁后构建。
- 产物 `web/nexus/public/agentscope-ui/` 约 15 MB，主要是官方动态语法高亮/Mermaid 等按需 chunk；保留官方行为，不裁剪。
- 元数据与许可证：`web/agentscope-ui/UPSTREAM.json`、`web/agentscope-ui/LICENSE`。
- API URL helper 覆盖 central client、workspace XHR 上传、knowledge 上传/下载；实测 workspace SKILL.md 上传 201 并回读，知识文档路径返回 AgentScope JSON（真实 KB 上传需 embedding 凭据，隔离服务未配置）。
- 原生写入回读：隔离用户 Agent 重命名后官方 UI 显示，再恢复；MCP 重命名/禁用后 API/UI 回读，再恢复。
- 隔离服务：端口 18001、Redis DB 9、临时 workspace/Qdrant/blob；health `skill_hubs=ok`、`mcp_hubs=ok`；真实 ClawHub/GitHub Registry 列表、测试 Skill/MCP 安装。未触碰原 18000/18120 数据。
- 部署：Vite preview 与 nginx 均配置 `/agentscope-api/` 代理和 `/agentscope-ui/` 深链接回退；保留 `X-User-ID`。

## 验证

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 前端类型检查/构建 | 通过（chunk size 警告） | `.astra/qa-v141-v142/frontend-typecheck-build.txt` |
| 前端单测 | 11 passed | `frontend-test.txt` |
| v1.4.1 提交树类型检查/构建 | 通过 | `v141fix-build.txt`（temp worktree） |
| v1.4.1 提交树浏览器 | 全通过，0 console/page error | `qa-v141fix-commit.output.txt` |
| v1.4.2 浏览器 | 全通过；403/500 为异常态故意触发 | `qa-v142.output.txt` |
| 官方 UI 浏览器 | 全通过；0 console/page error | `qa-agentscope-ui.output.txt` |
| 设计稿浏览器 | 14 项通过 | `qa-design.output.txt` |
| Nexus API 全量 | 81 passed, 4 skipped（live AgentScope） | `nexus-api-pytest.txt` |
| task-platform API+网关 | 109 passed, 3 skipped（真实 Redis） | `task-platform-gateway-pytest.txt` |
| Ruff | All checks passed | `task-platform-ruff.txt` |
| OpenSpec | Change 有效 | `openspec-validate.txt` |
| 隔离 AgentScope health | `skill_hubs=ok`, `mcp_hubs=ok` | `agentscope-health.json` |
| 官方 UI 构建 | 固定 commit + frozen lock + 元数据 | `agentscope-ui-build.log` |

### 布局断言

- 1440×900 / 1920×1080 聊天三栏 bottom ≤ viewHeight-32，输入框完整可见。
- 任务大厅、Worker、网关页框 bottom ≤ viewHeight-32；无横向溢出；无底部 dock。
- 网关动作工具栏位于 content-shell 卡头；Settings/Gateway 单一卡壳。
- 官方 UI 在配置中心卡内加载，1440/1920 截图可见。

### 截图

`chat-group-1440.png`、`chat-1920.png`、`chat-1440.png`、`gateway-overview-1440.png`、`gateway-overview-1920.png`、`agentscope-ui-1440.png`、`agentscope-ui-1920.png`、`design-normal-1440.png`、`design-abnormal-1440.png`、`design-empty-1440.png`、`design-normal-1920.png`、`design-normal-855.png`。

## 未验证 / 限制

- 生产 UI 的聊天/网关浏览器回归使用 mock 后端；真实后端由 pytest 覆盖，真实 Nexus API + AgentScope 多员工聊天未做全链路联调。
- 官方 UI 使用隔离的真实 AgentScope 2.0.10 与真实 Hub，但未在共享生产 Redis 上创建/删除资源。
- 真实知识文档上传需要 embedding 凭据；隔离服务未配置，因此只验证同源路径到达 AgentScope 并返回 JSON。
- 官方 UI 产物约 15 MB，构建有 chunk size 警告；未做裁剪/多版本兼容。
- `test_live_agentscope.py` 4 项、`test_gateway_data_plane.py` 3 项因缺少 live/Redis 环境跳过。
