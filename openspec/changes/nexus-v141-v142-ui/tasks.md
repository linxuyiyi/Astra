# Tasks

## 1. 网关 v1.4.2 设计稿

- [x] 1.1 创建 `docs/design/nexus-v142-gateway-preview.html`：模拟数据并标注“设计示例”，提供正常/异常/无数据切换、1440×900 与 1920×1080 说明、白灰薄荷绿 Element Plus 风格、侧边菜单与五主入口。完成标准：浏览器直接打开可用，三态可切换。
- [x] 1.2 在设计稿中展示六项主指标及各自真实窗口、顶部时间范围作用说明、requests/errors 调用趋势、节点默认全部异常优先、事件时间排序、可折叠辅助指标与 null 语义；普通 UI 不出现 P95，并明确延迟均值趋势本版本不提供。完成标准：文案与 `design.md` 指标口径一致。
- [x] 1.3 保存设计评审说明到 `docs/design/nexus-v142-gateway-preview.md`，列出待 Codex 确认项。完成标准：确认项可直接勾选。

## 2. 导航与配置中心（v1.4.1）

- [x] 2.1 `App.vue` 五个 `router-link` 迁到顶部 header，移除下拉菜单、底部 dock 与底栏占位 padding；保持选中高亮，顶部连接状态紧凑但异常可见。验证：五入口均可点击，页面工作区占满剩余高度，`/settings/*` 高亮配置中心。
- [x] 2.2 `router.ts` 建立 `/settings` 父布局与 `employees/workers/workers/:id/categories/agentscope` 子路由；根路径重定向 `/chat`；旧路径重定向。验证：`/`、`/employees`、`/team`、`/workers`、`/workers/:id`、`/interventions` 均到达预期页面。
- [x] 2.3 `SettingsView.vue` / `GatewayView.vue` 改为左侧菜单 + 右侧单张 `ElCard.content-shell` 承载 `RouterView`；子页去掉重复大标题和包裹卡，表格/筛选/操作保留，高度由 `100dvh + flex` 撑满。验证：子页切换不丢高亮，表格/内容底部不悬空，内部不再大卡套小卡。
- [x] 2.4 AgentScope 子页内嵌官方 v2.0.10 Web UI：`/agentscope-ui/` 静态产物、`/agentscope-api/` 同源代理、`X-User-ID` 上下文、深链接回退、新窗口辅助。验证：默认无需手填即可进入，真实 Agent/Skill/MCP 资源可见。
- [x] 2.4a 启用 AgentScope 原生 `ClawSkillHub` 与 `GitHubMCPHub`，在隔离端口/Redis DB 验证 health `skill_hubs/mcp_hubs=ok`，并完成真实 Skill 安装与 MCP 列表/安装。
- [x] 2.4b 记录上游 commit `e6c014f0...`、三处最小 UI 补丁与构建/部署方式；不复制官方业务后端，不新建 Nexus 管理数据库。
- [x] 2.5 任务大厅增加“任务列表/待处理”视图切换，`/interventions` 重定向到待处理视图；待处理操作保留。验证：旧链接和页面内切换均可操作。

## 3. 统一数字员工聊天（v1.4.1）

- [x] 3.1 新建 `DigitalEmployeeChatView` 与联系人/群组两个适配器；共用左栏、消息区、输入附件和右侧 Tab。验证：两种会话在同一页面切换，不加载第二套聊天页。
- [x] 3.2 左栏实现“全部｜联系人｜群组”筛选、忽略大小写的包含式搜索，匹配姓名、职责、群名、成员姓名。验证：搜索成员姓名能命中群组；清空恢复。
- [x] 3.3 会话状态按联系人/群组隔离：草稿、附件、消息、流式文本、notice、AbortController、generation token。验证：A/B 连续切换不串消息、不串附件、不自动发送。
- [x] 3.4 联系人适配器对接 `conversations`、私聊发送与 `/stream`；群组适配器对接 `channels`、timeline、成员流与 @ 触发。验证：附件、流式回复、历史消息、群成员维护正常。
- [x] 3.5 右侧 Tab：联系人任务/资料/记忆，群组任务/成员/资料；群组资料使用已有名称与目标，不展示个人记忆。验证：切换对象 Tab 正确。
- [x] 3.6 任务关联：`GET /tasks` 增加 `source_session_id`、`source_conversation_id` 精确筛选；联系人按会话查询，群组按 `source_channel_id` 查询；无关联显示“暂无关联任务”。验证：成员其它任务不混入。
- [x] 3.7 群组创建/编辑成员与联系人禁用状态保留。验证：创建群组、调整成员、禁用联系人不可发送但历史可读。

## 4. 文案与协作状态（v1.4.1）

- [x] 4.1 删除任务大厅、Worker、员工管理、待处理、图书馆、配置中心、网关页头的解释性副标题；保留错误、校验、部署/重启进度、人工确认与空状态。验证：逐页检查 `AstraPageHeader description` 与紧邻标题的说明段落。
- [x] 4.2 删除聊天“唯一私聊”“不会代写同事回复”等机制说明；新统一聊天页不得重新引入。
- [x] 4.3 `deliver_peer_hint` 返回结构化轻量结果；聊天工具区对 `contact_employee` 显示“已联系 XX/联系失败：原因”，其它工具保留受控详情。验证：工具长文本不再作为人类消息气泡。
- [x] 4.4 精简 `nexus-api` 员工系统指令中的过度解释，保留“调用 contact_employee、不代写”的行为约束。验证：系统指令仍包含行为约束。
- [x] 4.5 员工真实回复、错误原因、字段级帮助、群组目标不被文案清理删除。验证：回归检查真实消息与错误提示。

## 5. 边审边改修复

- [x] 5.0 顶部五入口替换底部 dock；去掉各页重复大标题；新建群组“＋”移到左栏搜索旁；聊天/管理/网关/settings 使用统一定义高度的 flex 布局。验证：1440×900 与 1920×1080 下聊天三栏逐一验证、页面框底部留白 0-48px，输入框可见，body 无横向溢出，无 dock。

- [x] 5.1 `selectTarget` 的异步 `prepareContact` 增加每目标 Promise 与选择 generation 校验；切换、回切、卸载后的迟到回包不得开启旧流或新建定时器。验证：A 慢/B 快/A 回切只保留当前订阅。
- [x] 5.2 编辑当前群组时保留草稿与附件，先停止旧流，再替换 target/adapter 并重新订阅；不得 `delete sessions` 丢状态。验证：编辑群组后草稿仍在、旧成员流已取消。
- [x] 5.3 发送结果与流式重载按消息 ID 合并；群组 timeline 重载加序号防旧响应覆盖新响应；发送迟到只写原会话，清草稿/附件前比对发送时快照。验证：同 ID 不重复、旧 timeline 不回退、切回新编辑不被误清。
- [x] 5.4 `senderLabel` 沿用 speaker 身份判断，群组其他人不统一显示为“你”。验证：群组多发言人身份正确。
- [x] 5.5 非 `contact_employee` 工具默认折叠，只显示名称与状态；参数/结果放可展开详情，故障排查仍可见。验证：工具原始 JSON 不占聊天气泡。
- [x] 5.6 群组成员流使用 `after` 游标与事件 ID 去重；timeline 延迟重载按序号丢弃旧结果；测试事件回放、延迟 timeline、快速 A/B/A。无法解决的接口限制写入验证记录。
- [x] 5.7 修正网关设计稿：缩放 wrapper 占位、1440/1920 完整居中、趋势图实际绘制、删除首页机制讲解、时间提示一行且明确 stats 窗口不影响独立 trend 窗口、删除假的“健康判断”指标。

## 6. 验证与提交

- [x] 6.1 `cd web/nexus && npm run type-check && npm run build`；运行现有前端测试。
- [x] 6.2 `cd services/task-platform && .venv/bin/pytest tests/test_api.py tests/test_gateway_observability.py tests/test_gateway_health_summary.py` 与 `.venv/bin/ruff check app tests`。
- [x] 6.3 `PYTHONPATH=services/nexus-api services/nexus-api/.venv/bin/pytest services/nexus-api/tests`（至少运行受影响 API 测试）。
- [x] 6.4 真实浏览器验证两尺寸：1440×900、1920×1080；覆盖五入口、旧链接、聊天快速切换、联系人/群组发送与附件、任务关联、配置中心、AgentScope 状态、网关设计稿三态。
- [x] 6.5 证据写入 `.astra/qa-v141-v142/`（截图、命令输出、未验证项）。完成标准：无证据不声称通过。
- [x] 6.6 检查 `git diff` 无无关修改后，v1.4.1 独立提交。

## 7. 网关 v1.4.2

- [x] 7.1 按确认稿改造 `GatewayView` 与 `GatewayOverview` 布局：六主指标、时间范围、趋势、节点异常优先、事件下钻、折叠明细。
- [x] 7.2 请求数/TTFT 平均/输出速度平均用 `/api/gateway/stats` 所选窗口；失败率、QPS、当前并发用 `/health-summary` 原窗口；趋势用 `/trends` 的 requests/errors，普通 UI 不展示 P95；null 不用 0。
- [x] 7.3 统一其它网关页面的表格、筛选与提示视觉，保留全部现有操作与错误反馈。
- [x] 7.4 运行 type-check/build、网关相关 pytest、真实浏览器两尺寸三状态验证，证据写入 `.astra/qa-v141-v142/`。
- [x] 7.5 设计稿同步顶部五入口、无重复大标题、左侧菜单 + 右侧单一内容卡工作台。
- [x] 7.6 官方 UI 的 API URL helper 覆盖 central client、workspace 上传、knowledge 上传/下载；实测 workspace SKILL.md 上传回读与知识文档同源路径。
- [x] 7.7 原生 API 写入（Agent 重命名、MCP 重命名/禁用）后 UI/API 回读并恢复；仅使用隔离用户。
- [x] 7.8 官方 UI 构建脚本固定上游 tag/commit，验证 commit、frozen pnpm lock、最小 patch、LICENSE/UPSTREAM 元数据；构建失败不保留旧产物。
- [x] 7.9 GatewayView 动作工具栏移入右侧 content-shell 卡头；Settings/Gateway 单一卡壳，内容区顶对齐。

## 8. 未验证与限制

- [x] `docs/design/nexus-v142-gateway-preview.html` 两尺寸与三态通过真实浏览器检查；生产 UI 的 1440×900 / 1920×1080 通过 mock 后端浏览器检查，不等同于生产验收。
- [x] 只读核查本机 AgentScope 2.0.10 `/health` 为 `ok`；未在共享 Redis/原 Agent Service 上创建 Agent 或执行真实多员工联调。原因：需要隔离 Redis、独立 Agent Service 与模型配置；本轮不修改原数据库、不重启原服务。
- [x] Nexus API 全量单测 `81 passed, 4 skipped`（4 项为需 `NEXUS_LIVE_AGENTSCOPE=1` 的 live 用例）。
- [x] task-platform 网关与任务 API 回归 `109 passed, 3 skipped`（3 项为需 `ASTRA_TEST_REDIS_URL` 的真实 Redis 用例）。
- [x] 延迟均值趋势：现有 `/trends` 不提供均值字段，本版本明确不展示；不新增后端契约。
