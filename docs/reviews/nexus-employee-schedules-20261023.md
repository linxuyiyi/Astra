# 日程安排功能与表格整改验收

日期：2026-10-23。分支 `codex/employee-schedules`，基线 `a3f429e7c`，工作树 `/Users/niuniu/.codex/worktrees/employee-schedules/Astra`。原始 `v1.4.8` 工作区及其未跟踪内容未改动，未提交、推送或部署生产。

## 原因与修改

原来只有官方日程机制，没有 Nexus 日程入口。首版适配虽使用 `el-table`，但没有完整复用管理列表骨架；在用户指出后，已按 `page-layout.css` 的“管理页面统一 UI 规范 v1.1”重做。

现为统一的卡片标题/刷新/新建 → 员工和状态筛选、搜索/重置 → 表格 → 右对齐 10/20/50 条分页。操作复用 RowMenu，状态复用 SemanticTag，列宽/对齐沿用相邻管理页，主表和执行记录均含加载、空态和失败重试。未新增表格库或页面级 `.el-table` 样式覆盖。

菜单顺序为数字员工 → 日程安排 → 任务大厅。官方日程配置、计时器、Session、inbox/wakeup 与消息存储由 AgentScope 负责。Nexus 仅适配身份、员工和网关路由，不新增日程表和调度器。

## 验证证据

| 验证层 | 结果与边界 |
| --- | --- |
| Nexus 与部署模板回归 | `tests/nexus` + `test_deploy_nginx_config.py`：133 passed，4 skipped（既有真实服务测试需显式启用） |
| Agent Service | `services/agent-service/tests`：13 passed，1 skipped（独立 MySQL 测试库未提供） |
| 前端现有测试 | `npm test`：30 passed |
| 类型和构建 | `npm run type-check`/`npm run build` 通过；保留既有大 chunk 警告 |
| 代码与契约检查 | Ruff、`git diff --check`、OpenSpec strict 校验通过 |
| 独立官方 SQL/HTTP 测试 | 真实官方 AsyncSQLAlchemyStorage + schedule router/访问服务/SessionService：A/B 同员工独立日程，跨人读写拒绝；stateful 复用、stateless 新建不同 workspace；暂停、删除及重启持久化 |
| 真实页面操作 | 隔离页面 `127.0.0.1:18231/schedules`：创建、暂停、恢复、编辑、搜索无结果、重置、状态筛选、每页 10 条、执行历史/内容、离线失败、恢复重试均已观察到正确可见结果 |
| 删除路径 | 页面确认框已打开并取消；真实删除与会话清理由 HTTP 测试覆盖，未通过页面最终删除演示数据 |
| 执行链路 | 独立 Agent Service + 官方 SQL + 独立 Redis `16393` + 本地 OpenAI 协议模型桩：调用官方触发函数，实际 inbox/wakeup dispatcher 推理，回复 `SCHEDULE_NATIVE_REPLY_OK` 落盘并可由页面查看 |

浏览器和上述本地链路使用测试员工、测试 UID、测试路由和本地模型桩，不调用真实供应商。预览服务没有启用真实周期计时器；官方 timer reconcile 的作业注册/移除由自动化测试验证，没有把手动调用官方触发函数冒充墙钟周期验收。

页面证据：

- [日程表格截图](../reports/evidence/employee-schedules-20261023/table.png)、[可访问性状态](../reports/evidence/employee-schedules-20261023/table-ax.txt)。截图为当前约 695px 的内嵌浏览器窄视口，表格保持列宽并可横向滚动。
- [执行记录截图](../reports/evidence/employee-schedules-20261023/execution.png)、[可访问性状态](../reports/evidence/employee-schedules-20261023/execution-ax.txt)。最终版本执行状态也复用了 SemanticTag 中性色。

## OpenSpec verify

使用 `openspec-verify-change` 对 `nexus-employee-schedules` 的完整性、正确性与一致性逐项核对。

| Requirement / Scenario | 实现与证据 |
| --- | --- |
| 独立入口 / 打开日程 | App.vue、router.ts、SchedulesView.vue；浏览器可见菜单顺序、统一管理列表骨架、查询/分页/空态/失败重试 |
| 安排者隔离 / 两人同员工 | schedules.py 派生稳定上游用户，官方访问策略只共享 READ 资源；test_schedules 与真实 SQL 测试验证隔离及 workspace 不同 |
| 安排者隔离 / 伪造归属 | require_owner 沿用可信头优先级；写模型拒绝未知字段；代理拒绝专用命名空间及大小写变体；Vite/Nginx 均经过统一代理 |
| 官方生命周期 / 创建编辑 | 适配调用官方 `/schedule/` POST/PATCH，模型从员工路由注入；浏览器已创建/修改，测试覆盖字段及返回事实 |
| 官方生命周期 / 无效与离线 | 官方 cron/timezone 校验、员工有效性与 HTTP 502；页面离线失败和恢复重试已验证 |
| 官方生命周期 / 删除 | 官方 SessionService 真正删除记录及关联会话，B 无法删除 A；真实 SQL/HTTP 测试覆盖 |

无未实现的本 change Requirement。所有工作项均有对应代码和验证；不修改 Astra 历史 capability。change 保留便于继续评审，未归档。

## 后续分析：对话创建日程

已补充 [数字员工对话创建日程：可行性与实现分析](../architecture/employee-schedules-conversational.md)。结论是：AgentScope 2.0.10 已自带 `ScheduleCreate/List/View/Delete`，当前被 `agent_service.py` 主动移除；直接放开会把日程写到平台用户 `nexus` 下，人类命名空间看不到，且平台列表会跨人可见。建议只在私聊、非日程执行会话保留官方工具，并按当前人类的 `nexus-schedule-<sha256(uid)>` 重建四个官方工具（或重定向 `_user_id`）；MCP 可行但不是关键，当前不值得为此新增进程。归属与重定向已用官方 SQLite 存储做 21 项检查，见 [spike 证据](../reports/evidence/employee-schedules-20261023/conversational-schedule-spike.json)。

该能力属于行为变更，与现有 change 的 Non-Goals（“本次不重新开放员工自主安排”）冲突，实施前需新建 OpenSpec change。开放前还需决定：默认不启用/人类确认、是否开放删除、一次性提醒语义、配额与最小间隔、日程执行工作区是否继承 Skill/MCP/知识库。

## 风险与未验证

- 尚未上线；真实供应商、生产 MySQL 8.0.36、真实 Nginx 重载、长时间周期运行和多副本单 scheduler owner 未验收。
- 日程工作区独立于私聊，本次未自动复制额外 Skill/MCP/知识库绑定；依赖这些资源的安排尚未完成能力继承验收。
- 模型路由更换不会改写既有官方日程的模型快照；禁用/删除员工也不跨安排者自动级联日程。
- 官方列表目前无分页；Nexus 在服务器读取当前用户记录后筛选、分页，浏览器仅收到一页。大规模上游查询性能未验收。
- 正式环境需认证代理覆盖身份头、隔离 Agent Service 直连，并发布本次 Agent Service 访问策略与统一代理配置。

完整设计与部署边界见 [数字员工日程安排](../architecture/employee-schedules.md)。
