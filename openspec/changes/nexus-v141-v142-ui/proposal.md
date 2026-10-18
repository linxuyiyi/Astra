## Why

v1.4.0 的导航被下拉菜单拆散，私聊与团队交流成为两套割裂页面，配置入口散落在任务与 Worker 页面；同时页头、聊天与工具结果中存在解释内部机制的文案。v1.4.2 还需要把 API 网关总览收敛为可审阅的六项主指标，避免用 P95 冒充平均值。

## What Changes

- 顶部主导航固定为五个直达入口：数字员工、任务大厅、数字图书馆、API 网关、配置中心，不再使用下拉菜单。
- 私聊与团队交流合并为 `/chat` 的数字员工统一聊天页：联系人与群组共用列表、消息区、输入附件和右侧 Tab，但保留原有私聊/群组两套 API、Session 与事件流。
- 新增配置中心父布局与嵌套路由：员工管理、Agent Worker、任务分类迁入 `/settings/*`；旧路径重定向到对应新位置。
- 任务大厅内置任务列表与待处理入口；原 `/interventions` 直达链接仍可用。
- 清理全站解释性页头副标题和聊天机制说明；保留错误、校验、部署/重启进度、人工确认与空状态。
- `contact_employee` 的工具结果与人类可见消息分层：聊天只显示“已联系 XX / 联系失败：原因”等轻量状态，不把工具长文本当作人类消息。
- 联系人右侧任务按当前会话严格筛选；为任务列表增加最小 `source_session_id`、`source_conversation_id` 查询筛选，不改任务状态机。
- API 网关总览重新组织为：轻状态与时间范围、六项主指标、调用趋势、节点（异常优先）、事件时间排序下钻、可折叠明细。主指标保留各接口真实窗口；趋势使用 `/trends` 的 requests/errors，普通 UI 不展示 P95。缺失值区分无请求、无采样、接口失败与无权限，不以 0 代替 null。
- AgentScope 子页改为真实官方 Web UI（v2.0.10），通过 `/agentscope-ui/` 静态产物与 `/agentscope-api/` 同源代理接入；启用原生 Skill/MCP Hub。
- 新增 `docs/design/nexus-v142-gateway-preview.html` 设计预览（模拟数据、正常/异常/无数据切换、1440×900 与 1920×1080）。Codex 已按同一口径确认推进，生产布局改动按本 change 实施。

## Capabilities

### New Capabilities

- `nexus-navigation`: 五入口直达、路由高亮、旧路径兼容与根路径默认页。
- `digital-employee-chat`: 联系人/群组统一聊天、搜索筛选、会话隔离、流式订阅、右侧信息与当前会话任务关联。
- `nexus-configuration-center`: 配置中心父子路由及员工、Worker、任务分类、AgentScope 入口归口；右侧单一内容卡工作台。
- `agentscope-management-ui`: 官方 AgentScope Web UI 的同源嵌入、API 代理、原生 Skill/MCP Hub 与隔离验证。
- `nexus-user-facing-copy`: 页头与聊天文案精简、必要提示保留、工具结果与人类消息分离。
- `gateway-overview-ui`: 网关总览六主指标、窗口口径、趋势与节点/事件布局、null 语义与响应式预览约束。

### Modified Capabilities

- 无。Nexus 不修改 `openspec/specs/` 中的 Astra 历史契约。

## Impact

- 前端：`web/nexus/src/App.vue`、`router.ts`、聊天/任务/配置/网关视图与共用组件。
- 任务 API：`services/task-platform/app/api/routes.py` 的 `/tasks` 增加两个只读筛选参数，响应字段已存在。
- 员工对话：`services/nexus-api/nexus_api/main.py` 的系统指令措辞精简；`services/agent-service/agent_service.py` 的 `deliver_peer_hint` 返回结构化轻量结果。
- 文档与证据：OpenSpec change、网关设计预览、`.astra/qa-v141-v142` 验证记录。
- 不改变：AgentScope 事实源、Session 模型、任务状态机、Worker 调度、网关代理/健康/路由/限流/熔断/重试/鉴权/日志审计与数据库/API 契约。
