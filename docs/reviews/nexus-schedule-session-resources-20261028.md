# 日程 Session 资源准备修复与验证

## 原因

基线：`v1.4.8` / `4e2044913`。开始时 tracked 文件无修改，原有 untracked 文件保持原样。本记录对应本地实现验收，未部署。

代码确认 AgentScope 2.0.10 scheduler 直接创建 Session/workspace 并向 inbox 投递 HintBlock，未调用 Nexus 员工资源同步器。Nexus 原有工具策略和 Task MCP 来源验证仅识别已登记的聊天绑定，单独补装文件不足以让日程获得工具权限。

本地安装包还确认：`LocalWorkspaceManager` 的 `PER_SESSION` 只隔离 workspace ID/cache，物理目录仍是 `basedir/agent_id`，不同日程和私聊可能共用磁盘内容。`activated_groups=[]` 本身不能证明没有工具，因为官方 `basic` 组始终可用。

Wetask 具体执行记录、首轮回复及 Library 401 修复时间来自用户提供的排查证据，本次未连接生产重新取证。Library 注册凭据未在本次修改。

## 改动

- Agent Service 对可信日程来源，在官方运行锁取得前向 Nexus 请求资源准备。资源互斥覆盖准备及整次运行，后续唤醒不会中途拆改工作区；中断跳过准备。
- Nexus 从官方存储校验安排者、Agent、Session、ScheduleOrigin 和日程记录，复用通用 Skill/MCP 收敛器。平台目录和隔离工作区使用不同身份，库安装复用官方路由实现，不复制 scheduler、Session 或记忆。
- 工具装配与聊天共用员工权限规则，并激活装配后的工具组。Task MCP 在连接和工具调用时验证官方日程来源与当前员工任务权限；保留执行 Session 来源，查询/操作限制在当前执行 Session，不借用私聊身份。
- 日程 manager 子类按安排者、员工、持久化 workspace_id 派生物理目录及缓存键。普通聊天目录不变，旧共用目录中的文件不会复制进新日程目录。
- 通用同步器将按资源 ID 返回的技能安装失败视为失败；技能改名时也向已有日程安装记录登记旧名回收，下一次准备收回旧名。
- 准备失败由官方装配错误处理持久化 `finished_reason=error` 和 `finished_at`，不调用模型。日志仅记录 Session、异常类型和 HTTP 状态，不记录原始上游响应或凭据。

## 验证

| 检查 | 结果 |
| --- | --- |
| Nexus 全套 `tests/nexus` | 163 passed, 6 skipped |
| Task MCP 身份、任务归属、唤醒、提示、传输安全 | 48 passed |
| Agent Service 全套（含本次真实链路） | 19 passed, 1 skipped |
| Nexus `ruff check app nexus_api tests` | 通过 |
| 新 Agent Service 模块和测试 Ruff | 通过 |
| `git diff --check` | 通过 |
| OpenSpec `nexus-schedule-session-resources --strict` | 通过 |

真实链路使用官方 2.0.10 SQL、scheduler、inbox、Agent、workspace 和工具执行；Nexus 内部请求通过 ASGI transport，MCP 为实际监听本机临时端口的 HTTP 测试服务，模型为确定性桩。覆盖独立/连续上下文、任务管理和平台库 Skill 实际读取、平台库 MCP 及任务工具测试端点调用、继续推理、完成时间、运行锁/资源互斥释放、准备失败不调用模型、后续触发恢复。任务工具测试端点不等于真实 Worker 工程执行验收。

另有测试覆盖跨安排者/伪造员工/服务认证/员工禁用、无私聊资源安装、重复准备不重复上传、绑定收回、技能改名、准备与推理互斥，以及普通聊天和中断不依赖准备成功。

### 平台全量回归限制

组合 `tests/nexus tests/platform` 扩大回归出现多个失败，执行中止，未获得完整全量结果。随后单独运行 `tests/platform -x`，首个失败为：

`test_acceptance_review.py::test_question_to_acceptance_wakes_for_the_new_result` 第 80 行期待“回答问题”，实际为“等待数字员工处理问题或权限请求”。

从原始 HEAD 导出的独立临时源码中重新运行该用例，同样失败。这里只确认此首个失败是基线已有问题，不能据此归因组合运行中的其他未逐一核实失败；本次不修改无关的人机介入文案契约。

## OpenSpec 场景核对

| 场景 | 实现与验证证据 |
| --- | --- |
| 独立日程首次执行 | `prepare_schedule`、官方 scheduler/Agent 真实链路的 stateless 参数 |
| 连续上下文再次执行 | 通用同步器、重复安装/绑定收回/技能改名用例、真实 stateful 连续触发 |
| 伪造或跨安排者调用 | 官方来源接口、内部接口服务认证、员工与 Session 绑定验证及负向用例 |
| 同员工不同安排者工作目录 | 日程 workspace manager；相同 workspace ID 的跨安排者/私聊磁盘隔离测试 |
| 库资源与任务能力 | 官方库安装适配、真实 Skill/MCP 调用、Task 来源鉴权与跨执行任务访问拒绝 |
| 部分安装或服务失败 | 资源 ID 失败用例、真实链路故障注入：无模型调用、官方错误终态及恢复 |
| 普通聊天与中断 | 运行适配跳过分支测试、日程中断跳过准备、普通目录保持测试及 Nexus 回归 |

完整性：6/6 tasks；正确性：3/3 Requirements、7/7 Scenarios 有对应代码/测试；一致性：继续使用官方调度、状态、存储和资源安装，未增加业务状态机或 schema。生产边界如下，不把本地通过视为线上闭环。

## 风险与部署边界

- 需同时更新并重启 Nexus、Agent Service 和 Task MCP；Agent Service 新增的两个模块必须一起发布，双方平台 owner 和服务间密钥需一致。
- 未验证真实 Wetask、供应商模型、生产 MySQL 8.0.36/Redis、Linux 及多副本 timer owner；日程知识库绑定和已有模型快照变更不在本次范围。
- 不自动重放历史卡住的执行；旧共用目录中的日程工作文件不自动迁移。
- 日程任务没有原人类聊天绑定，Worker 求助向人的投递闭环未在本次验收。当前日程任务权限限制在本次执行 Session。
- OpenSpec 尚未归档；Git 合入结果以提交历史为准。
