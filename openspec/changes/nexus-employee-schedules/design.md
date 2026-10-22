## Context

已核对官方 v2.0.10 / e6c014f 的 schedule router、SchedulerManager、访问策略和 examples/web_ui/frontend/src/pages/schedule。官方定时器通过 HintBlock + inbox + wakeup 执行，源状态全部保留在 Agent Service。现有平台把 Schedule 工具从员工工具集中移除；本次不重新开放员工自主安排。

## Goals / Non-Goals

目标：独立菜单、按人隔离的周期安排与执行内容。非目标：任务编排、多人共享编辑、一次性提醒、重复实现计时器或日历 cron 展开。

## Decisions

- 使用官方 ResourceAccessPolicyBase 扩展点向专用人类命名空间只读共享平台 Agent、网关 credential 与知识库。人类 UID 经 SHA-256 映射为稳定上游用户；日程和会话继续由官方按用户隔离。无需 Nexus 表或迁移。
- Nexus HTTP 适配层从现有 require_owner 派生上游用户，按已登记员工校验 Agent 并从员工路由注入模型。客户端不能覆盖用户、Agent 配置或凭据。官方通用代理固定平台服务身份，不能冒充人类日程命名空间。
- 页面依据官方日程列表、表单、详情与历史结构适配为 Vue/Element Plus，保留字段与语义，复用 admin-card/admin-page 工具栏、RowMenu、SemanticTag 与右对齐 10/20/50 条分页，使用后端员工/状态/关键词筛选和统一错误处理；没有嵌入整个含模型凭据编辑的官方控制台。标明来源，默认 Asia/Shanghai，支持每天、工作日、每周、每月及自定义 cron。
- 上游列表暂无分页；适配层分页后只向浏览器返回一页。执行内容读取前先校验该会话属于当前人的该日程。

## Risks / Trade-offs

- 日程使用创建时的路由模型快照；员工改路由不会自动改写官方已有日程配置，需重新创建日程。
- 启停遵循官方异步 reconcile，最多约 60 秒；多副本部署必须只有一个 scheduler owner。
- 禁用员工不自动级联暂停已有官方日程，操作员应先暂停安排；此边界在页面说明。
- 人类身份沿用当前 UID/可信代理约定，正式部署需由认证代理覆盖身份头并隔离 Agent Service 直连。
- 官方上游列表当前一次读取用户日程，平台侧分页控制浏览器渲染量；不承诺大规模上游查询性能。

- 官方调度新建独立工作区，当前不复制私聊工作区的 Skill/MCP/知识库绑定；本次验收覆盖基础 Agent 与模型回复，任务执行等能力继承需单独接入和验收。
