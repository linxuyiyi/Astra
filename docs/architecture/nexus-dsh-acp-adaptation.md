# Nexus v1.4.8 × DSH ACP 适配设计基线

> 状态：设计已锁定、实施未完成。基线：`v1.4.8`；本分支专用于 DSH 执行引擎适配。不得据此声称 DSH 已稳定上线。

## 决策

1. Nexus 保留 Task、AgentWorker、Worker Gateway、RuntimeSession 索引、Git worktree、网关、人工确认及经验复盘。AgentScope 2.0.10 保留数字员工聊天、Agent、Session、记忆等事实源。只把代码任务执行引擎从 OpenCode 扩展为 DSH。
2. 正式接口优先采用 DeepSeek Harness 官方 ACP（stdio JSON-RPC），Worker 使用官方 `agent-client-protocol` Python 客户端，启动固定版本 `dsh --profile acp`。Python SDK 不作为受管停止/授权主接口。
3. 保留 Worker Gateway WebSocket 数据/命令通道；DSH 子进程只监听 Worker 本机 stdio，不开放公网 HTTP。不 fork DSH。
4. `worker_channel=worker` 表示已有传输通道；`runtime_engine=dsh|opencode` 表示执行引擎。二者不可混用。迁移期间 OpenCode 可运行，严禁同一执行会话跨引擎热切换、失败后自动切引擎重试。
5. Nexus 不复制 DSH 消息全文、不另建 Run 状态机；DSH 原生 Session/持久轨迹是执行事实源，RuntimeSession 只保存绑定和结果索引。
6. 模型必须经过现有 Nexus Gateway `/v1`；DSH Profile 与 Gateway 模型别名能否兼容必须先实测，不允许偷偷直连厂商。
7. DSH 当前候选版本为 `@deepseek-ai/dsh@0.2.1-rc.2`（开发预览而非已验收生产稳定版），版本、ACP 客户端、插件和 Profile 必须统一固定、预检兼容，不追随 latest。

## 页面复用与前端边界（必须实施，不是后续美化）

### 既有资源复用核对

- Nexus Vue 3 + Element Plus 入口 `services/nexus-api/web`、导航/星系首页/任务大厅/任务详情/Worker 管理/配置中心继续作为唯一管理界面；不新增第二套 DSH 任务管理站点。
- `services/nexus-api/chat-island` 继续为 AgentScope 数字员工单聊/群聊服务，不将数字员工对话与执行引擎 Session 混为一谈。
- 已有 Workspace 文件浏览、Git 变更/Diff、附件、操作授权、任务状态、人工确认 UI 尽量保持既有路由、组件和交互，后台用 `runtime_engine` 分支调用真正支持的能力。
- `web/workspace-ui` 实际为基于 `@opencode-ai/app`/SolidJS 的 OpenCode 前端包，依赖其原生 API/SSE/消息结构，**不能简单替换 API URL 或当作 DSH 前端**。迁移期间保留给 OpenCode 原生任务，不在其内部硬塞 ACP。
- DSH 有官方完整 `dsh --profile web` Web UI，包含代码/对话、Session、文件/工具等丰富操作；但它是与 `--profile acp` 不同的应用组合，官方 ACP 明确不暴露 DSH 专属计划、标题、待办、终端等完整 UI 数据。官方 Web UI **不能被视为已经可以绑定同一活跃 ACP Session 的通用页面组件**。

### 定稿：Nexus 通用任务工作台（统一路由，不重建整个页面）

1. 原任务详情页面增加/保持 **执行过程**、**文件变更**、**验证与结果**、**人工交互** 视图，顶部只展示真实引擎版本、Worker、Session、健康/连接状态。
2. 定义后端 ViewModel 而非伪造 OpenCode message：`TaskWorkbenchView`（业务 Task、Worker、会话绑定、能力表），`RuntimeEvent`（seq/session/turn/kind/phase/timestamp/payload）、`InteractionRequest`（approval/clarification、归属、超时、答案）、`WorkspaceChange`（沿用现有 Git diff）。
3. DSH 通过 ACP 适配 **assistant 消息、generic tool lifecycle、reasoning（按权限显示）、配置变更、上下文用量、permission**；缺失的 DSH 专属 UI 结构不得模拟为“原生已支持”。如果终端/计划/待办需要展示，需另做官方 Session 日志/插件只读投影，经版本化契约和一致性测试验证后启用。
4. OpenCode 仍复用现有原生工作台；DSH 任务默认走 Nexus 通用工作台。路由直接按 `runtime_engine` 选择视图，不允许前端以事件内容猜引擎，也不让 DSH 适配层暴露 OpenCode 专属接口伪兼容。
5. Nexus Workbench UI 必须支持同一员工多个任务并行时，按 `task_id + binding_id + native_session_id` 隔离消息、权限审批、停止及变更。切换任务、刷新、断线恢复后不串消息。
6. 任务确认完成与 Session 停止分离；不可在 UI 的“回复完成”事件出现时自动变更业务任务为已完成。

### DSH 原生 Web UI 的复用边界

- DSH Web 属于**原生人工使用的独立页面**。默认不 iframe 内嵌、不开放远程随机端口、不共享 ACP 子进程写入中的 `DSH_HOME`、不自动把 DSH token URL 发给浏览器。
- 原生 Web 若需在 Nexus 中作为“高级工作台”呈现，先验证一个**受支持的同进程 Host/统一 Session 所有权**方案：同一 DSH Context 内是否能装配 Web+ACP、是否能关联同一工作区和活动 Session、是否存在重复写入或权限/鉴权边界冲突。
- 只有在受管身份代理、按任务 URL 绑定、HTTPS/WebSocket/Host-Origin 检查、Session 唯一写入者、关闭恢复与端到端测试全部通过时，才能增加 `打开原生 DSH 工作台` 入口。不得以两个独立 DSH Host 共享一个 `DSH_HOME` 来实现“复用”，亦不得宣称 iframe 等于集成。
- 该高级原生入口**不作为正式 DSH 任务执行的依赖**，因而不会阻断 ACP 适配项目的稳定发布。DSH 原生 HTML/CSS 不复制到 Nexus，不 fork/同步其内部 UI 包。

### 前端验收门槛

- 任务大厅 → DSH 任务 → 工作台 → 工具/消息流 → 文件 Diff → 人工审批 → 停止/继续 → 人工确认，必须在现有 Nexus 导航下闭环。
- 同一员工至少两个并行任务：切换后消息/工具/审批隔离；断线刷新继续原生 Session；未知状态不展示为成功；重复事件幂等。
- 不能支持的原生 DSH 计划、终端等能力明确标注缺失，不用假日志/伪终端补齐。OpenCode 任务的旧 Workspace UI 不受破坏。
- 验证桌面 2560×1440 下组件尺寸、左右布局、滚动容器、全屏/宽屏响应；不另引入一套设计系统。
- DSH Web/ACP 官方文档：<https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/web-app/README.md> 、<https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/acp/acp/README.md> 。

## 代码切点（已核对 v1.4.8）

| 文件 | 本次改动 |
|---|---|
| `services/nexus-api/app/runtime/lifecycle.py` | 将 OpenCode 原生操作从 Task 业务流移入执行驱动；保留已有业务确认、占用/状态协调 |
| `app/runtime/session_connection.py` | 保留现有 Git worktree；按引擎选择执行驱动 |
| `app/runtime/session_state.py` | DSH 原生事件到结果状态独立投影，不复用 OpenCode message/finish 推断 |
| `app/executors/opencode_prompt.py` | 业务提示词和原生交互能力解耦；不能要求 DSH 使用 OpenCode question Tool |
| `app/models.py` | 通过**新增** Alembic migration 保存 engine/native-session/持久目录/版本等最小绑定，不修改已发布 `0001_init` |
| `app/runtime/worker_files.py`、`workspace_changes.py` | 复用 Worker 文件、Git diff、提交历史与路径约束 |
| `docs/architecture/task-experience-review.md` | 保留 MAP/REDUCE 与 candidate 人工审核，按引擎接入原生只读证据来源 |

## 运行时契约

- 极薄的 `RuntimeDriver`：`prepare`、`create_or_resume_session`、`submit`、`cancel`、`reply_permission`、`snapshot`、`read_trace_page`、`dispose`。
- Worker 端 `DshAcpDriver` + `AcpProcessSupervisor`：第一版每个活跃 RuntimeSession 配置一个独立可监督的 ACP 子进程；并发由现有 Worker Pool 控制。
- 每个 DSH Session 保持原 Worker、工作区、`DSH_HOME`、Profile hash、原生 Session ID、DSH/ACP 版本指纹。Worker 重启后先对账原生日志/进程再恢复；磁盘丢失或无法恢复时标记异常，禁止自动新建相同任务并重跑。
- 提交命令包含 `submission_id`、`binding_id`、`attempt/generation` 和 fencing token。只有唯一已受理提交可执行；超时或掉线先查受理证据，结果未知不得重复提交。
- `session/cancel` → 等待确认 → 超时仅终止归属此 Session 的进程组；未确认停止前不得把同一任务派给另一 Worker。
- 人工工具审批走 ACP `session/request_permission`，关联 task/session/tool_call；普通业务澄清走已有 Task MCP 的 WAITING 机制，回复后继续同一个 DSH Session。危险操作不默认放行。
- 会话轮次终止/idle ≠ 任务完成；保留现有人工验收确认。断线状态不能被投影成 SUCCEEDED。
- ACP `session/update` 是在线事件，不是完整历史证据。复盘必须有原生持久轨迹的按需分页读取能力；如 DSH 没有稳定公开读接口，使用只读 observer 插件或固定版本解析器，不 fork DSH，且必须验明证据完整性。

## 唯一实施清单

### P0 —— 必须优先跑真实兼容性探针
- 对照固定版本官方 tag 验证 ACP 的 init、新建/恢复/取消/授权、连续双轮、MCP、日志、断线和长 Session。
- 在目标 Mac、公司 Linux ARM64/glibc 2.28、Windows x64 做运行时加载与前置依赖检查；公司 Python 若旧版，ACP 客户端运行于 Worker 独立 Python 3.10+ venv。
- 经 Nexus Gateway 真实验证 DSH Profile 对 DeepSeek/GLM/Qwen 已配置模型路由的兼容性（流式、工具、reasoning、token 统计、限流），拒绝默认直连或自动覆盖人工模型选择。
- P0 未通过不得进行正式切流；不得以 mock 测试替代 DSH 实机测试。

### P1 —— Worker 增加 DSH 引擎
- 建立离线安装包/固定版本依赖锁、Profile 与 Skill/MCP 装配 hash、部署检测/健康检查。
- 实现 ACP 进程管理、受理/事件/取消/审批/断线恢复/隔离，复用 Worker Gateway。
- 安全前置：独立工作目录、系统权限、网络白名单和 MCP 工具白名单，记录无敏感字段的运行审计。

### P2 —— Nexus Runtime 与 UI
- 增加最小 Alembic migration 和 `runtime_engine` 选择；将 OpenCode 操作收敛到已有驱动实现，业务任务状态不另起一套。
- 任务详情复用统一工作台；按引擎只显示真正可用的操作、事件、权限；保留 Git worktree、diff、任务关闭/恢复。
- 经验复盘增加 DSH 原生轨迹阅读适配，所有证据可回溯、candidate 仍需人工确认。

### P3 —— 实证验收与切换
- 同模型、同网关、同任务比较 DSH/OpenCode 代码正确性、测试通过率、人工修复工作量、token/吞吐。
- 建议至少 100 个任务、72 小时包含重启、断线、取消、超时、权限等待的故障注入/长任务验收；这些是拟定门禁，不是已跑结果。
- 首先灰度 DSH Worker，满足门禁后仅将**新任务**默认引擎切至 DSH；老 OpenCode Session 自然完成。最后单独变更清理 OpenCode 专属遗留。
- 任何阶段失败可停止 DSH 新派单并回到 OpenCode；不得自动抹除或重写 DSH Session 数据。

## 开发约定与资料

- 必读本仓 `AGENTS.md`；跨模块契约先按仓库已配置的 OpenSpec 约定处理，在当前 change 的 `tasks.md` 维护唯一实施计划。
- 不在 main/master 或原 `v1.4.8` 上开发，保护已有工作树及未提交代码；每阶段独立提交并有可核对实测证据。
- 官方 DSH ACP：<https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/acp/acp/README.md>
- DSH 发布记录：<https://github.com/deepseek-ai/deepseek-harness/releases>
- 官方 ACP Python 客户端：<https://github.com/agentclientprotocol/python-sdk>
- 原复盘基线：`docs/architecture/task-experience-review.md`
