## Context

v1.4.0 前端已经有员工目录、私聊、团队交流、任务、Worker、图书馆、网关与任务分类页面。员工、Session、Skill、Knowledge 与记忆的唯一事实源仍是 AgentScope；任务仍由 task-platform 管理。本次只调整前端信息架构与两处最小服务端返回，不重建会话或任务模型。

## Goals / Non-Goals

**Goals:**

- 五个底部主入口直达，配置管理收口到 `/settings/*`，旧路径保持可达。
- 用单个 `DigitalEmployeeChatView` 统一联系人/群组，共用列表、消息、输入附件与右侧 Tab；两个适配器分别对接原私聊和群组 API、Session 与事件流。
- 会话切换时取消旧流、隔离草稿/附件/流式状态，防止异步回包串屏。
- 联系人任务按当前会话筛选，群组任务按 `source_channel_id` 筛选。
- 去掉解释性页头与机制话术，保留错误、校验、部署进度、人工确认和真实回复。
- 交付网关 v1.4.2 可审阅设计稿；生产布局等设计确认后再改。

**Non-Goals:**

- 不合并联系人/群组的服务端会话模型，不更换 AgentScope Session。
- 不新增 Skill/MCP/Session/Memory 后端，不伪造 AgentScope 管理能力。
- 不改任务状态机、Worker 调度、网关代理/健康/路由/限流/熔断/重试/鉴权/日志审计与数据库契约。
- 本次不实施网关生产布局改动。

## Decisions

### 导航与路由

- `App.vue` 改为五个 `router-link`，不使用 `el-dropdown`。高亮通过 `route.path` 前缀匹配。
- 根路径 `/` 重定向到 `/chat`。
- `/settings` 为父布局（`ElContainer + ElAside + ElMenu + RouterView`），与 `GatewayView.vue` 同款结构。
- 旧路径使用重定向：`/employees → /settings/employees`、`/workers → /settings/workers`、`/workers/:id → /settings/workers/:id`、`/interventions → /tasks?view=interventions`、`/team → /chat`（带 `group` 参数时保留）。
- 任务大厅内部用 `view=list|interventions` 查询参数切换“任务列表/待处理”，复用现有待处理视图逻辑，不再占底部入口。

### 统一聊天

- `DigitalEmployeeChatView` 只做容器与交互编排；`features/chat` 下提供：
  - 目标模型：`contact` 与 `group` 两类 session key。
  - `PrivateChatAdapter`：封装 `createConversation`、`getConversation`、`sendConversation`、`/stream`。
  - `GroupChatAdapter`：封装 `listChannels`、`channelTimeline`、`sendChannel`、频道事件流与成员流。
- 两个适配器暴露统一接口：`load()`、`send()`、`follow()`、`taskQuery()`。视图不直接拼两套 API。
- 会话状态按 `contact:<employeeId>` / `group:<channelId>` 隔离：`draft`、`attachments`、`messages`、`liveLines`、`notice`、`abortController`、`generation token`。
- 切换会话立即 abort 旧流并递增 token；所有异步回调在写入状态前校验 session key 与 token。
- 私聊适配器在选中联系人时 `createConversation`（后端保证一人一条私聊）；群组适配器按频道加载。
- 联系人任务：调用 `/api/tasks?source_session_id=<agentscope session>`；会话尚未建立 Session 时按 `source_conversation_id=<conversation id>` 查询。群组：`source_channel_id=<channel id>`。
- 右侧 Tab：联系人 `任务/资料/记忆`；群组 `任务/成员/资料`。资料来自已有 `Channel.name/description` 与成员数据，不编造记忆。
- 未读与最近消息：仅在已有可靠字段时展示；本次不伪造未读数，群组按创建时间、联系人按名称稳定排序。

### 任务查询契约

- 在 `GET /tasks` 增加只读可选参数 `source_session_id`、`source_conversation_id`，与已有 `source_channel_id` 同级做精确过滤。
- `TaskRead` 已包含这两个字段，无需数据迁移；不改变任务创建、Worker 占用和状态机。
- 不使用 responsible_employee_id 作为联系人会话任务的替代口径。

### 文案与工具状态

- 删除 `AstraPageHeader description` 中属于页头解释的文案；保留字段级 `description`、空状态、错误、校验与操作确认。
- 网关父页去掉 `sectionLine` 动态长说明，保留标题、菜单、内容与错误反馈。
- `contact_employee` 返回结构化轻量 JSON：成功 `{"ok": true, "peer": "...", "status": "delivered"}`；失败 `{"ok": false, "peer": "...", "error": "..."}`。Agent 仍可读取结果推理。
- 聊天工具区只在 `tool.name === "contact_employee"` 时解析结构化结果并显示“已联系 XX”/“联系失败：原因”；其它工具保留受控详情。不对任意模型正文做关键词过滤。

### AgentScope 入口

- 现有部署没有可验证的 AgentScope 图形管理 UI，Nexus 不内嵌、不复制 Skill/MCP/Session/Memory 管理。
- 配置中心提供 `/settings/agentscope`：展示 AgentScope 版本、配置与可达状态；若部署设置可选 `NEXUS_AGENTSCOPE_UI_URL`，显示“打开 AgentScope 管理界面”外部入口；否则明确说明当前部署未提供图形管理入口。
- `/api/v1/runtime` 增加可选 `ui_url` 字段用于该入口，不改变已有字段。

### 网关 v1.4.2 指标口径（设计稿）

顶部时间控件 SHALL 明确标注“仅用于请求数、TTFT 平均值、输出速度平均值和趋势”。窗口取 `GET /api/gateway/stats?scope=overview&window=<hour|today|7d|30d>`，按配置时区的自然小时/自然日计算，`window_start/window_end` 由后端返回。作用范围固定为 `overview`。

| 主指标 | 接口与真实字段 | 展示窗口 |
| --- | --- | --- |
| 请求数 | `/stats` `requests` | 顶部所选窗口（本小时/今日/近 7 天/近 30 天） |
| 请求失败率 | `/health-summary` `metrics.error_rate`，样本 `error_samples`、失败数 `error_failures` | 近 5 分钟；样本不足时标注“未达健康判断样本”，不改成其它窗口 |
| QPS | `/health-summary` `metrics.qps`（`qps_requests / 60`） | 近 1 分钟 |
| TTFT 平均 | `/stats` `ttft_ms.avg` | 顶部所选窗口；接口不暴露该延迟的独立分母，UI 不伪造样本数 |
| 输出速度平均 | `/stats` `output_tps.avg` | 顶部所选窗口 |
| 当前并发数 | `/health-summary` `metrics.active` | 实时，不受顶部窗口影响 |

节点表来自 `GET /api/gateway/node-metrics`，默认全部显示、异常优先；不展示 `ttft_p95_ms`。健康摘要继续驱动原健康判断，P95 字段只用于后端判断，不在普通 UI 展示。

趋势使用 `GET /api/gateway/trends?window=1h|15m&group_by=none|model|node` 的 `requests` 与 `errors`，保留时间窗口与模型/节点分组。普通 UI MUST NOT 绘制 TTFT/输出速度 P95 趋势。当前 `/trends` 不提供延迟平均值；本版本明确记录“延迟均值趋势暂不提供”，不新增后端字段、不把缺失伪装成无数据，也不使用 P95 替代。延迟平均值只在主指标按所选统计窗口展示。

辅助明细：重试率、上游 429、流式中断率、Token 吞吐量、Token 用量缺失率取 `/health-summary` 的近 5 分钟字段；连接耗时、TTFB 取 `/stats` 的 `connect_ms.avg`、`ttfb_ms.avg`，标注所选窗口与“平均”。

事件来自 `GET /api/gateway/ops-events`，按 `created_at` 倒序展示；点击事件带 `node_id/model/pool_id/key_id/from/to` 打开 `/gateway/logs`。

null 语义：`null` 只能显示“无请求/无采样”，请求失败显示“加载失败”，401/403 显示“无权限”；不得用 0 填充。底层健康判断完整保留。

### 设计预览

- `docs/design/nexus-v142-gateway-preview.html` 为独立静态模拟稿，显式标记“设计示例，模拟数据”。
- 提供正常/异常/无数据三态切换与 1440×900、1920×1080 尺寸预览说明，沿用白灰薄荷绿与 Element Plus 视觉。

## Risks / Trade-offs

- [快速切换仍可能有迟到回包] → session key + generation token 双重校验；abort 旧流；发送结果只写入发起会话。
- [任务来源字段历史不一致] → 只做精确匹配，不做员工级兜底；无匹配显示“暂无关联任务”。
- [群组多成员流式排序] → 以 timeline 为准，流式文本只作为临时展示，REPLY_END 后重载 timeline。
- [删除副标题误伤业务信息] → 只改页头与机制说明，保留字段级帮助、错误、空状态和真实回复。
- [AgentScope 无原生 UI] → 只做状态与可选外部入口，不伪造管理页面。
- [网关指标口径混淆] → 主指标窗口与健康窗口分开标注；P95 一律写 P95；null 不用 0。
- [指标窗口混用] → 每张卡标注真实窗口，顶部控件只声明对 request/TTFT/TPS/趋势生效；健康判断保留原 5 分钟口径。

## Migration Plan

- 前端路由与页面改动随发布生效；旧链接由重定向兼容，无需数据迁移。
- 任务查询新增可选参数，向后兼容。
- `contact_employee` 返回结构调整只影响工具结果展示；Agent 仍收到 JSON 字符串。
- 回滚按提交回退到 v1.4.0 基线；数据库无变更。

## Open Questions

- AgentScope 部署是否实际提供可访问的管理 UI。若提供，需确认鉴权与反向代理方式后再接入；当前按“无内置 UI”处理。
- 趋势平均值是否允许扩展 `/trends` 契约。当前保持 P95 并明确标注。
