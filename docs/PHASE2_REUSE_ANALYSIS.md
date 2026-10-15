# Nexus v1.0.1 第二阶段历史代码复用分析

分析日期：2026-10-15。当前第一阶段未验收，本文件只记录源码分析与迁移契约，不代表第二阶段已开始实施或通过测试。

## 1. 基线与范围

- Nexus：`/Users/niuniu/程序/Nexus`，开发分支 `codex/nexus-v1.0.1`；第一阶段已有 AgentScope 服务、员工扩展、资源绑定与运行守卫，前端、完整对话、附件和删除仍未交付。
- 历史源：[Astra codex/astra-updates-20261010](https://github.com/linxuyiyi/Astra/tree/codex/astra-updates-20261010)，实际 `git ls-remote` 与本地 remote ref 均为 **05dd73142b294a9cb6b2b14a0750b707422aeec3**。
- 分析使用固定提交的 `git show/ls-tree` 及只读选择性快照 `/tmp/nexus-astra-phase2-05dd731`。快照未导出的文件不能据此判为源分支缺失；已另行核实完整 workspace-ui、复盘 Skill、助手 agent 和 MCP 启动配置存在。
- UI 参考：`/Users/niuniu/Downloads/Nexus_v1.0.1_五页面原型_Worker环境运维完整版.html`，保留五页面布局与成熟业务页面。原型中的模拟数据、定时假进度不迁移。
- 本轮未运行历史工程测试、SSH、部署或真实任务。下面的测试仅指可复用的测试源码，不是通过记录。

目标链路：AgentScope 员工 → Task Skill/MCP → Task Service → Worker Pool → OpenCode；关键任务事件可靠唤醒责任员工，由员工查询当前事实后决定后续动作。

## 2. 数据与职责契约

| 对象 | 唯一职责与事实源 | 最小迁移关系 |
| --- | --- | --- |
| Employee | 长期 AgentScope 员工身份、职责、能力和启停 | 保留第一阶段 Employee 与原生 Agent 绑定；不承载 SSH、Serve 进程或执行占用 |
| Task | 工程需求、分类、验收标准与业务闭环 | `responsible_employee_id`、`source_conversation_id`、`worker_id`；保留历史 Task Service 和状态机 |
| Worker | OpenCode 执行资源及环境运维 | 从旧 DigitalEmployee 的执行字段/环境/占用/心跳适配；与 Employee 身份分离 |
| ExecutionEnvironment | 本地或 Linux 远程配置 | 旧 `employee_id` 改 Worker FK；保留每 Worker 一个环境的唯一关系、不可变执行快照和密码不回显策略 |
| RuntimeSession | Task 的执行绑定、终态和用量索引 | 旧执行员工 FK 改 Worker FK；责任员工通过 Task 获取；保留 `superseded_by` 隔离旧轮次 |
| Event | Task 业务事件与执行关联 | 旧 `Event.employee_id` 由旧 Task 执行员工填入，改为 Worker 关系；员工唤醒目标另从 Task 的责任员工获取 |
| OpenCode Session/Workspace | 工程执行、消息、权限、问题、文件和变更事实源 | 继续通过 Server/Session/Event 与原生查询；不恢复 Run 表或消息副本；Workspace 是路径与 worktree，不虚构已有 Workspace 表 |
| AgentScope Conversation Session | 员工交流、推理与记忆事实源 | 与 OpenCode RuntimeSession 分开；以可信服务端绑定解析 Employee/owner/来源会话 |
| 任务总结/经验 | 真实执行证据和人工审核发布 | `source_employee_id` 来自 Task 责任员工，不能写 Worker ID；执行证据引用真实 RuntimeSession |

旧 `task_reassign` 的环境快照与占用重置对应 Worker 重分配，不能混同责任员工转交。新库不迁移旧业务数据或整条历史 Alembic 链；提取当前有效模型与约束即可。

## 3. 复用清单

“直接复用”允许包路径、配置命名和测试 import 调整；“小范围适配”保留业务算法与主要交互，修改边界绑定；“需要实现”仅补源码确实缺少的能力。

| 模块 | 分类 | 固定提交中的实现与迁移说明 |
| --- | --- | --- |
| Task 状态规则 | 直接复用 | `backend/app/state_machine.py`、`runtime/session_state.py`、`runtime/task_status.json`；实际状态为 PENDING/RUNNING/WAITING/SUCCEEDED/FAILED/CANCELED，无 PAUSED |
| Task Service/API | 小范围适配 | `services.py:TaskService`、`api/routes.py`；保留创建/转换/事件/筛选/确认/删除，`(source, external_task_id)` 已有幂等约束；拆分责任员工与执行 Worker，并增加 owner/来源会话校验。历史没有 Task PATCH/PUT 编辑 API，用户所需修改能力须补薄服务入口与合法状态校验 |
| 执行生命周期 | 小范围适配 | `runtime/lifecycle.py:RuntimeService` 的 start/send/retry_submission/stop/refresh/confirm_completion 是主链路；保留人工问题/权限、取消、未知提交恢复及旧轮次保护 |
| OpenCode 连接 | 直接复用与关系适配 | `executors/opencode_server.py:OpenCodeServerClient`、`runtime/session_connection.py`；实际 Session、prompt_async、events、question、permission、abort。不要另迁一个重复执行循环；大体量 `_execute` 不是现行 Task 主链路 |
| 故障与恢复 | 小范围适配 | `runtime/recovery.py` 的 SSE 观察、绝对状态重读、退避和恢复；保留现有 Celery 包装，不把 Scout 或平台助手作为前置 |
| Workspace/worktree | 小范围适配 | `session_connection.py:prepare_workspace`、`api/workspace_routes.py`；复用真实 Git 根/common-dir/注册/符号链接校验、任务配置及只读历史代理；目录命名改 Worker，绑定目录从 DB 取 |
| 任务中心/详情 | 小范围适配 | `frontend/src/views/TaskListView.vue`、`TaskDetailView.vue` 及相关组件/API/store；保留筛选、表格、控制、日志、总结、经验等主要交互，只换架构字段和入口 |
| OpenCode 工作区 UI | 小范围适配 | TaskDetail 的 OpenCodeTaskWorkspace iframe、`workspace-ui/` 全 fork、`scripts/workspace_ui.mjs` 和代理；上游 v1.18.33、commit 51ef4be1d3c122f18fefb510dca8d778571f4f18。保留已有禁用新建/fork/archive 的产品约束；验证与实际 Serve 版本兼容，不删除构建资产 |
| Task MCP 查询/控制基础 | 小范围适配 | `assistant/tool_gateway.py` 的 task_search/task_get；`assistant/operations.py` 已调用 TaskService.create、RuntimeService.stop/start。提取领域资格校验，新增薄 MCP 入口，不复制状态机 |
| 完整 Task MCP/Skill | 需要实现与查询适配 | 历史 Task 控制为 prepare_create/cancel/retry；reassign 未注册，resume 文案不是工具。task_get 已含 review/task_summary，experience_search 已查候选/发布经验，另有 Session 查询、失败摘要和可信复盘提交，应薄适配复用。仅补缺少的编辑/分配/运行控制工具与自主 Task Skill，服务端绑定员工身份，不强制旧 WAITING_CONFIRM 卡片流程 |
| AgentScope 注册与事件唤醒 | 需要实现 | 复用第一阶段资源目录/工具 guard/Session factory；增加 Task MCP 与 Skill 配置、Task 关键事件持久投递、幂等与重启恢复，使用原生消息/唤醒入口，不建事件管理页面 |
| Worker 环境模型/API | 小范围适配 | `models.py:OpenCodeExecutionEnvironment`、schemas、环境 API；已有 LOCAL/LINUX_REMOTE、SSH host/port/user/password、目录、可执行路径/版本、Serve 端口、模型与检测状态；改绑 Worker，保留编辑空密码不覆盖 |
| SSH 运输 | 直接复用 | `runtime/ssh_client.py:SSHSession` 的命令、流式输出、SFTP、正反隧道及关闭。当前仅密码认证和 AutoAddPolicy，不能声称已有密钥认证或严格 host-key 管理 |
| 本地 Serve | 小范围适配 | `executors/opencode_local_server.py:ensure_ready`；保留跨进程端口锁、owner/workspace 与 `/path` 校验、只停止自有进程、超时和日志尾部；owner 改 Worker |
| Linux 远程 Serve | 小范围适配并补缺 | `executors/opencode_remote_server.py`；复用 tunnel/PID/cwd/命令/健康/日志。健康复用分支缺少本地同等级 `/path` 归属核对和启动互斥，需补多 Worker 资源隔离 |
| 一键部署/重启 | 小范围适配 | `runtime/remote_provisioning.py`、`remote_install.sh`、`api/environment_routes.py`；保留短事务 CAS、busy/有效期、总超时、取消、步骤日志、旧 operation 结果隔离、固定版本校验/flock/原子配置合并，改 Worker/Gateway/Library 接线 |
| 环境检测 | 小范围适配 | `runtime/environment.py:ExecutionEnvironmentRuntime.test`；保留 SSH、命令/版本、目录权限、端口检查；区分环境可用与 Serve 已启动 |
| Worker Pool | 需要实现最小适配 | `runtime/dispatcher.py:claim_once` 只有 `current_task_count < 1` 与首个兼容员工选择，不是完整容量池。复用优先级/兼容筛选/原子占用/快照/恢复，补 Worker.capacity 与并发安全计数/释放，不另造调度平台 |
| Worker 日志入口 | 需要实现薄查询 | 已有本地日志、远端 server.log、失败 tail、部署 NDJSON，但没有独立 Worker 日志查询 API；补受控查询入口，复用现有日志来源 |
| Worker 表格/抽屉 | 小范围适配 | `EmployeeListView.vue`、`components/employee/EmployeeDetailDrawer.vue`、`settings/EmployeeConfig.vue`；保留表格/筛选/分页、抽屉表单、检测、部署/取消和未保存保护，移除人格/办公室/自动认领耦合 |
| 网关路由与凭据算法 | 直接复用 | `gateway/scheduler.py`、`security.py`；健康/熔断/模型/并发/节点额度过滤、权重选择、Key 摘要恒时验证与 Fernet 加密 |
| 网关数据面/控制面 | 小范围适配 | `gateway/server.py`、`runtime.py:RedisRuntimeStore`、config/service/observability 与 `api/gateway_routes.py`；保留流式转发、重试、租约、用量、刷新、撤销、节点 drain、统计和完整 CRUD，适配部署/DB/Employee/Worker 调用 |
| 网关页面 | 小范围适配 | `frontend/src/views/gateway/`、`api/gateway.ts`、`utils/gateway.ts` 和共享 Element Plus 组件/tokens；保留总览、节点/池/路由/Key/日志，不重设计 |
| 图书馆资源与检索 | 小范围适配 | `library_service.py:KnowledgeService`、models 的图书馆模型、`llm_wiki.py`；保留目录/权限/CRUD/批处理/发布/收藏/反馈/引用及真实 llm_wiki Source API；业务状态与同步状态分开，不另建重复 RAG |
| 图书馆 MCP | 小范围适配 | `mcp_server.py` 的 search/get/related/feedback 与 stateless Streamable HTTP；`runtime_context.py` 的旧执行 token 解析重接 Employee/Task/Session；related 目前明确 unsupported，不补假图谱 |
| 总结/候选/审核发布 | 小范围适配 | `assistant/review.py`、TaskSummary、ExperienceEvidence、library_service；保留实际 OpenCode 证据验证、精确去重/带理由合并/证据追加、已发布正文保护、审核/驳回/补充；候选仅由可信复盘提交，不新增员工直接发布 |
| 复盘 Skill 与 MCP 资产 | 小范围适配 | `.opencode/skills/nexus-task-experience-review/SKILL.md`、`.opencode/agents/platform-assistant.md`、opencode.json 中 `astra-assistant-mcp` 均实际存在；提取复盘能力，不需迁整个旧平台助手页面 |
| 图书馆页面 | 小范围适配 | `DigitalLibraryView.vue`、`views/library/` 六组件及 API/types；保留资源、证据、审核、同步状态和统计 |
| 旧数据与外围产品 | 不再需要 | 旧业务数据、整条迁移历史、旧 3D/办公室/人格、Scout、旧平台助手确认卡/页面、版本兼容重定向、演示数据和假进度；CLI fallback 不作为正式执行链路 |

## 4. 必须保留的实际边界

1. **业务完成与执行结束分开。** 历史 OpenCode 轮次成功不会自动关闭 Task，当前仅 confirm_completion 完成业务闭环。第二阶段员工可负责跟进与交付；是否允许员工在验证后代替人类完成确认，必须在 Task MCP 契约中明确，不能迁移时悄悄改变。
2. **暂停没有独立历史状态。** 不能照原型新增第二套 PAUSED 状态机；按现有 stop/start/send 的实际语义明确暂停/恢复工具行为，遇到行为冲突再确认。
3. **网关实际协议只有 `/v1/models` 与 `/v1/chat/completions`。** 无 Responses/Anthropic 协议翻译或模型名改写。AgentScope/Worker 使用现有兼容协议；不扩大到新网关产品。
4. **Key Token 配额存在实现缺口。** 小时/日字段与统计存在，handle_chat 当前只执行 Key 并发限制，节点额度规则已有。迁移报告必须区分已保留项与必要补齐项，不声称完整 Key Token 停止策略已存在。
5. **生产网关共享运行态依赖 Redis。** 不用测试 InMemoryRuntimeStore 代替正式租约/撤销/用量。历史 DB 同时支持 SQLite+aiosqlite 与 MySQL+asyncmy，不预设需要 PostgreSQL 重构。
6. **管理入口信任边界须接线。** 旧网关 actor 固定 astra，图书馆默认 admin/信任 X-Astra 头；不能将匿名 UUID 或浏览器可伪造头当正式鉴权。保留受控部署边界和业务权限规则，不顺带开发登录产品。
7. **知识关系不等于原生 KB ID。** 图书馆知识与 AgentScope KB 需最小映射/配置，使员工所选知识与记忆真实可用，不能只改标签或复制一套搜索服务。
8. **原型五页没有网关/图书馆入口。** 旧路由 `/library`、`/gateway/*` 完整保留；第二阶段集成时确定最小入口，不擅加第六/第七个底部菜单或重做页面。

## 5. 可复用验证与后续验收

| 范围 | 历史测试基线 | 证据边界与第二阶段必验 |
| --- | --- | --- |
| Task/Session/恢复 | runtime、task、workspace、review 等 backend 测试及 Task e2e | 保留旧轮次隔离、取消/重试/人工介入/未知提交；新增责任员工与 Worker 分离、MCP 授权/幂等 |
| Serve/运维 | test_employee_serve、test_remote_provisioning、test_session_workspace、remote-provisioning.spec | Serve 为真实 HTTP fixture 子进程而非实际 OpenCode；远端多数 mock，shell 在本地 Bash；迁移后仍须真实 SSH/安装/重启/故障/日志 |
| 网关 | gateway_control/data_plane/config/observability 测试与 gateway e2e | Redis 实测受 ASTRA_TEST_REDIS_URL 条件控制；e2e 多 mock。补真实 Redis、转发、用量、吊销/限流与 AgentScope/Worker 调用 |
| 图书馆/经验 | library/tree/llm_wiki/experience/runtime_context/task_review_api 与 library e2e | 真实检索/MCP、经验候选证据/审核入库及已发布保护；不将 mock UI 或 unsupported related 算完整能力 |
| 员工任务闭环 | 新增真实端到端证据 | 人类需求→员工 Skill/MCP 建任务→Pool 分配→真实 OpenCode 工作→事件唤醒→员工跟进/介入→验证交付/总结候选；任务中心/Worker/员工会话显示一致 |
| 并发与重启 | 新增边界验收 | 多 Task/多 Worker/容量竞争、Worker 故障、Session 恢复、事件重复与重启漏投恢复；不能用计时器或预设回复替代 |

实施顺序遵循用户要求：第一阶段门槛通过 → Task/Employee/Worker 契约 → Task 迁移与 MCP/Skill → AgentScope 真实调用 → Worker/Pool/运维 → 网关/图书馆 → 页面/事件集成 → 真实端到端。每模块经独立审查和实际检查后进入下一项；实际代码由 OpenCode 完成。
