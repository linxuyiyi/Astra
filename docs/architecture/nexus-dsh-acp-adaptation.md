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
- 任务详情仅按引擎显示真正可用的操作；保留 Git worktree、diff、任务关闭/恢复。
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
