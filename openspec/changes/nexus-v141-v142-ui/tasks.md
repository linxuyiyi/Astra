# Tasks

## 1. 网关 v1.4.2 设计稿

- [ ] 1.1 创建 `docs/design/nexus-v142-gateway-preview.html`：模拟数据并标注“设计示例”，提供正常/异常/无数据切换、1440×900 与 1920×1080 说明、白灰薄荷绿 Element Plus 风格、侧边菜单与五主入口。完成标准：浏览器直接打开可用，三态可切换。
- [ ] 1.2 在设计稿中展示六项主指标及各自真实窗口、顶部时间范围作用说明、requests/errors 调用趋势、节点默认全部异常优先、事件时间排序、可折叠辅助指标与 null 语义；普通 UI 不出现 P95，并明确延迟均值趋势本版本不提供。完成标准：文案与 `design.md` 指标口径一致。
- [ ] 1.3 保存设计评审说明到 `docs/design/nexus-v142-gateway-preview.md`，列出待 Codex 确认项。完成标准：确认项可直接勾选。

## 2. 导航与配置中心（v1.4.1）

- [ ] 2.1 `App.vue` 改为五个 `router-link` 直达入口，移除下拉菜单与 Worker 独立入口；保持选中高亮。验证：五入口均可点击，`/settings/*` 高亮配置中心。
- [ ] 2.2 `router.ts` 建立 `/settings` 父布局与 `employees/workers/workers/:id/categories/agentscope` 子路由；根路径重定向 `/chat`；旧路径重定向。验证：`/`、`/employees`、`/team`、`/workers`、`/workers/:id`、`/interventions` 均到达预期页面。
- [ ] 2.3 `SettingsView.vue` 改为左侧菜单 + 右侧 `RouterView`，与网关布局一致；子页复用现有员工、Worker、分类组件。验证：子页切换不丢配置中心高亮。
- [ ] 2.4 新增 AgentScope 配置页：显示版本/配置/可达状态；可选 `ui_url` 外部入口；无 UI 时明确说明，不伪造 Skill/MCP 管理。验证：不可达、未配置、已配置三种状态文案正确。
- [ ] 2.5 任务大厅增加“任务列表/待处理”视图切换，`/interventions` 重定向到待处理视图；待处理操作保留。验证：旧链接和页面内切换均可操作。

## 3. 统一数字员工聊天（v1.4.1）

- [ ] 3.1 新建 `DigitalEmployeeChatView` 与联系人/群组两个适配器；共用左栏、消息区、输入附件和右侧 Tab。验证：两种会话在同一页面切换，不加载第二套聊天页。
- [ ] 3.2 左栏实现“全部｜联系人｜群组”筛选、忽略大小写的包含式搜索，匹配姓名、职责、群名、成员姓名。验证：搜索成员姓名能命中群组；清空恢复。
- [ ] 3.3 会话状态按联系人/群组隔离：草稿、附件、消息、流式文本、notice、AbortController、generation token。验证：A/B 连续切换不串消息、不串附件、不自动发送。
- [ ] 3.4 联系人适配器对接 `conversations`、私聊发送与 `/stream`；群组适配器对接 `channels`、timeline、成员流与 @ 触发。验证：附件、流式回复、历史消息、群成员维护正常。
- [ ] 3.5 右侧 Tab：联系人任务/资料/记忆，群组任务/成员/资料；群组资料使用已有名称与目标，不展示个人记忆。验证：切换对象 Tab 正确。
- [ ] 3.6 任务关联：`GET /tasks` 增加 `source_session_id`、`source_conversation_id` 精确筛选；联系人按会话查询，群组按 `source_channel_id` 查询；无关联显示“暂无关联任务”。验证：成员其它任务不混入。
- [ ] 3.7 群组创建/编辑成员与联系人禁用状态保留。验证：创建群组、调整成员、禁用联系人不可发送但历史可读。

## 4. 文案与协作状态（v1.4.1）

- [ ] 4.1 删除任务大厅、Worker、员工管理、待处理、图书馆、配置中心、网关页头的解释性副标题；保留错误、校验、部署/重启进度、人工确认与空状态。验证：逐页检查 `AstraPageHeader description` 与紧邻标题的说明段落。
- [ ] 4.2 删除聊天“唯一私聊”“不会代写同事回复”等机制说明；新统一聊天页不得重新引入。
- [ ] 4.3 `deliver_peer_hint` 返回结构化轻量结果；聊天工具区对 `contact_employee` 显示“已联系 XX/联系失败：原因”，其它工具保留受控详情。验证：工具长文本不再作为人类消息气泡。
- [ ] 4.4 精简 `nexus-api` 员工系统指令中的过度解释，保留“调用 contact_employee、不代写”的行为约束。验证：系统指令仍包含行为约束。
- [ ] 4.5 员工真实回复、错误原因、字段级帮助、群组目标不被文案清理删除。验证：回归检查真实消息与错误提示。

## 5. 边审边改修复

- [ ] 5.1 `selectTarget` 的异步 `prepareContact` 增加每目标 Promise 与选择 generation 校验；切换、回切、卸载后的迟到回包不得开启旧流或新建定时器。验证：A 慢/B 快/A 回切只保留当前订阅。
- [ ] 5.2 编辑当前群组时保留草稿与附件，先停止旧流，再替换 target/adapter 并重新订阅；不得 `delete sessions` 丢状态。验证：编辑群组后草稿仍在、旧成员流已取消。
- [ ] 5.3 发送结果与流式重载按消息 ID 合并；群组 timeline 重载加序号防旧响应覆盖新响应；发送迟到只写原会话，清草稿/附件前比对发送时快照。验证：同 ID 不重复、旧 timeline 不回退、切回新编辑不被误清。
- [ ] 5.4 `senderLabel` 沿用 speaker 身份判断，群组其他人不统一显示为“你”。验证：群组多发言人身份正确。
- [ ] 5.5 非 `contact_employee` 工具默认折叠，只显示名称与状态；参数/结果放可展开详情，故障排查仍可见。验证：工具原始 JSON 不占聊天气泡。
- [ ] 5.6 群组成员流使用 `after` 游标与事件 ID 去重；timeline 延迟重载按序号丢弃旧结果；测试事件回放、延迟 timeline、快速 A/B/A。无法解决的接口限制写入验证记录。
- [ ] 5.7 修正网关设计稿：缩放 wrapper 占位、1440/1920 完整居中、趋势图实际绘制、删除首页机制讲解、时间提示一行且明确 stats 窗口不影响独立 trend 窗口、删除假的“健康判断”指标。

## 6. 验证与提交

- [ ] 6.1 `cd web/nexus && npm run type-check && npm run build`；运行现有前端测试。
- [ ] 6.2 `cd services/task-platform && .venv/bin/pytest tests/test_api.py tests/test_gateway_observability.py tests/test_gateway_health_summary.py` 与 `.venv/bin/ruff check app tests`。
- [ ] 6.3 `PYTHONPATH=services/nexus-api services/nexus-api/.venv/bin/pytest services/nexus-api/tests`（至少运行受影响 API 测试）。
- [ ] 6.4 真实浏览器验证两尺寸：1440×900、1920×1080；覆盖五入口、旧链接、聊天快速切换、联系人/群组发送与附件、任务关联、配置中心、AgentScope 状态、网关设计稿三态。
- [ ] 6.5 证据写入 `.astra/qa-v141-v142/`（截图、命令输出、未验证项）。完成标准：无证据不声称通过。
- [ ] 6.6 检查 `git diff` 无无关修改后，v1.4.1 独立提交。

## 7. 网关 v1.4.2

- [ ] 7.1 按确认稿改造 `GatewayView` 与 `GatewayOverview` 布局：六主指标、时间范围、趋势、节点异常优先、事件下钻、折叠明细。
- [ ] 7.2 请求数/TTFT 平均/输出速度平均用 `/api/gateway/stats` 所选窗口；失败率、QPS、当前并发用 `/health-summary` 原窗口；趋势用 `/trends` 的 requests/errors，普通 UI 不展示 P95；null 不用 0。
- [ ] 7.3 统一其它网关页面的表格、筛选与提示视觉，保留全部现有操作与错误反馈。
- [ ] 7.4 运行 type-check/build、网关相关 pytest、真实浏览器两尺寸三状态验证，证据写入 `.astra/qa-v141-v142/`。
