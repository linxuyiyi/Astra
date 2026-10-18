# API 网关 v1.4.2 设计评审说明

- 设计稿：`docs/design/nexus-v142-gateway-preview.html`
- 状态：Codex 已确认以下口径，生产实现按此推进。
- 数据声明：设计稿全部为模拟数据，不连接真实接口。

## 已确认口径

- 顶部保留五个主入口；网关侧边菜单保持：监控（运行总览 / 调用日志 / 审计）、配置（上游节点 / 资源池与路由 / API Key）。
- 顶部时间控件只作用于请求数、TTFT 平均值、输出速度平均值和趋势；页面必须明示。
- 六项主指标各自标注真实窗口：
  - 请求数：`/api/gateway/stats` 所选窗口。
  - 请求失败率：`/api/gateway/health-summary` 近 5 分钟 `metrics.error_rate`，保留原健康判断口径。
  - QPS：`/api/gateway/health-summary` 近 1 分钟 `metrics.qps`。
  - TTFT 平均值：`/api/gateway/stats` `ttft_ms.avg`，所选窗口。
  - 输出速度平均值：`/api/gateway/stats` `output_tps.avg`，所选窗口。
  - 当前并发数：`/api/gateway/health-summary` `metrics.active`，实时值。
- 调用趋势使用 `/api/gateway/trends` 的 `requests` 与 `errors`，保留 1h/15m 与按模型/节点分组。
- 普通 UI 不展示 P95。底层 P95 健康判断完整保留。
- 延迟均值趋势当前接口不支持，作为本版本限制明确展示；不新增后端契约，不用 P95 替代，不伪装成无数据。
- 节点默认全部显示，异常优先；“只看异常”可切换。
- 事件按时间排序，点击带对象与时间范围下钻调用日志。
- 辅助指标可折叠：重试率、上游 429、流式中断率、Token 吞吐量、用量缺失率使用近 5 分钟健康窗口；连接耗时、TTFB 使用 `/stats` 所选窗口平均值。
- `null` 不使用 0 代替；区分无请求、无采样、接口失败、无权限。

## 仍需部署侧确认（不阻塞前端实现）

- `NEXUS_AGENTSCOPE_UI_URL` 是否配置真实可访问的 AgentScope 管理入口。未配置时，配置中心只展示状态与说明，不伪造管理页面。
- 趋势平均值若后续确需，需要后端为 `TrendPoint` 增加平均字段；本版本不做。
