# 数字员工日程安排

菜单位于“数字员工”之后，入口 `/schedules`。当前人类可创建、编辑、暂停、恢复、删除自己的周期日程，并查看官方执行会话和文本结果。同一员工可以接受多个人类的安排，日程与上下文分别隔离。

## 官方来源与适配

基线为 AgentScope `v2.0.10`，commit `e6c014f0be49b2a6f6f62a9453082a0b975484e1`。

- 官方页面来源：`examples/web_ui/frontend/src/pages/schedule/` 的列表、创建表单、详情与历史结构。
- 现有技术栈为 Vue 3 + Element Plus；本页按官方字段与语义移植，未复制 React 构建产物，也未嵌入含模型/凭据编辑的完整控制台。
- 有意差异：使用仓库的 `admin-card` → 筛选工具栏 → `el-table` → 右对齐 `el-pagination`；复用 RowMenu 和 SemanticTag。支持每页 10/20/50 条、员工/状态筛选、关键词搜索、加载、空态和失败重试。主表与执行记录表使用同一规范，无页面级 `.el-table` 覆盖。
- 仅提供官方支持的周期 Cron，不把按年月日的 Cron 冒充一次性提醒；首次未加入月历展开。
- 模型由后端从员工网关路由确定，表单不允许填写厂商、credential 或归属人。列表不返回模型配置；执行列表不返回 Agent state。

## 状态与权限事实源

日程配置、启停、执行会话及消息全部存在 AgentScope SQL 存储。Nexus 无日程表、计时器或平行状态机。

Nexus 沿用 `require_owner` 的现有身份约定，将可信 UID 的 SHA-256 映射为稳定的 `nexus-schedule-<hash>` 上游用户。UID 不进入请求体；查询参数和浏览器 `X-User-ID` 不参与归属判定。

Agent Service 使用官方 `ResourceAccessPolicyBase` 扩展点，只向该命名空间共享平台 Agent、credential、知识库的 READ 权限，不授予 EDIT；日程与 Session 本身不共享。读取执行内容时，Nexus 先验证该会话属于当前人的指定日程。

控制台 `/agentscope-api/` 拒绝冒充日程命名空间（包括大写变体）。Vite 与 Nginx 均先经过统一 Nexus 服务，再由该服务代理 AgentScope。正式部署仍需认证代理覆盖人类身份头，并限制 Agent Service 直连。

## 执行与边界

执行完全复用官方 `SchedulerManager`：保存日程 → 通知 timer owner reconcile → 绑定独立 workspace/Session → HintBlock inbox → wakeup dispatcher → Agent 推理。不同人的日程互不复用上下文；连续上下文只在同一个日程内复用。

- 启停遵循官方 reconcile，可能约 60 秒后生效；一个部署只能有一个 timer owner。
- 已有日程保存创建时的模型配置快照。员工改路由后需重新创建日程。
- 禁用/删除员工前应先暂停/删除关联日程，当前不跨安排者自动级联。
- 官方新建的日程工作区独立于私聊；本次未自动复制私聊的 Skill/MCP/知识库绑定。日程可使用基础 Agent 指令、官方默认工作区能力与网关模型；依赖额外员工资源的安排需后续单独接入和验收。
- 连续上下文的会话数不等于执行次数；页面不推测执行成功、失败或下次运行时间。执行内容从官方消息读取，仅展示文本块，过滤内部 HintBlock/思考块。
- 上游官方列表尚未提供分页；Nexus 在读取当前用户上游记录后筛选、分页，浏览器只收到一页。当前不承诺大规模上游查询性能。

## 对话创建（MCP）

私聊中的员工可以通过内置 `nexus-schedule` MCP 创建、列表、查看当前人类的日程；官方 `ScheduleCreate` 等 Agent 工具仍保持移除。MCP 只安装在私聊 Session，归属由 Nexus 按 `agentscope_session_id` 解析，创建默认不启用；团队、邮箱、渠道不提供。实现和部署见 [数字员工日程 MCP](employee-schedules-mcp.md)。

MCP 走官方 REST 创建，官方 `source` 为 `USER`、没有 `source_session_id`；这是与官方工具重定向方案的差异。页面、执行记录和隔离仍沿用原设计。

## 部署

无需数据库 migration。更新 Agent Service 的 `agent_service.py`、`schedule_access.py` 并重启，以加载官方资源访问策略；更新 Nexus 和前端构建；加载新的 Nginx 模板使 `/agentscope-api/` 经过 Nexus。Nexus 与 Agent Service 的 `NEXUS_AGENTSCOPE_USER_ID` 必须一致。

运行针对性验证：

```bash
cd services/nexus-api
PYTHONPATH="$PWD" .venv/bin/pytest tests/nexus/test_schedules.py tests/nexus/test_agentscope_proxy.py tests/platform/test_deploy_nginx_config.py
cd ../agent-service
PYTHONPATH="$PWD" .venv-agentscope/bin/python -m pytest tests/test_schedule_access.py
```
