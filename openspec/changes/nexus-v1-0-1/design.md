## Context

新仓库位于 `/Users/niuniu/程序/Nexus`，分支 `codex/nexus-v1.0.1`。Astra 只提供研发规范；用户原始需求及完整原型保存在 `docs/reference/`。原生接口研究见 `docs/AGENTSCOPE_RESEARCH.md`，UI 细节见 `docs/UI_ACCEPTANCE.md`。

AgentScope Python 固定 `2.0.10`，tag commit `e6c014f0be49b2a6f6f62a9453082a0b975484e1`。官网文档版本与发布版本有漂移，以该版本源码和真实服务 `/openapi.json` 为契约。已在隔离 Python 3.12 环境安装正式包，验证官方 ASGI 生命周期、Agent/Session CRUD 和 SQLite 重开恢复；尚不代表真实进程重启或模型推理通过。

## Goals / Non-Goals

**Goals:** 三个页面保留原型布局，所有操作真实连接服务；一个员工对应一个长期原生 Agent；私聊和基础团队会话可恢复；人类 @ 指定长期员工真实回复；匿名会话与附件隔离；证据驱动验收。按用户 2026-10-15 更新，自主多员工职责协作延后第三阶段。

**Non-Goals:** 旧业务兼容、旧数据迁移、第四个产品页面、模型配置页面、复杂协作编排，以及 proposal 列出的版本外能力。

## Decisions

### 1. 两个进程，模块化业务单体

`frontend/` 使用 Vue 3、TypeScript、Element Plus、Vite；`backend/` 使用 FastAPI、SQLAlchemy async、SQLite。`agent-service/` 仅使用官方 `agentscope.app.create_app` 和公开扩展，作为独立进程部署。Nexus 后端通过 HTTP 调用官方路由，不导入其私有执行服务，不复制官方核心源码。

最小验证部署使用官方 `AsyncSQLAlchemyStorage` + `aiosqlite`、单进程 `InMemoryMessageBus`、持久化工作区；通过公开 `WorkspaceManagerBase` + `LocalWorkspace` 按原生持久 workspace ID 分目录（官方 `LocalWorkspaceManager` 2.0.10 实际按 Agent 分目录，仅设置 PER_SESSION 不足以隔离文件）。无跨进程事件总线要求时不引入 Redis、Celery 或新微服务。服务仅监听回环地址，前端经 Nexus 访问。生产多副本不在本版本验收范围。

### 2. 明确数据事实源

| 数据 | 事实源 |
| --- | --- |
| Agent 姓名、实际执行 prompt、原生配置 | AgentScope AgentRecord |
| 员工领域、职责/指令编辑字段、头像、enabled、资源默认关联、删除标记 | Nexus Employee 扩展；保存时编译执行 prompt 并真实更新 AgentScope |
| 模型凭据与 Session 模型绑定 | AgentScope；服务部署配置提供默认绑定，Nexus 页面不显示模型选项 |
| 活动会话消息、AgentState、运行状态、原生记忆 | AgentScope |
| Conversation owner、员工/Session 关联、讨论、成员、投递 envelope、附件归属 | Nexus SQLite |
| 删除后的只读历史 | 删除前一次性归档的原生消息快照，永久不可执行，不形成第二套活动会话 |

绑定必须使用 AgentRecord **顶层 `id`**。实测它与 `data.id` 不同；`POST /agent/` 返回值指向顶层记录。`GET /sessions/` 的元素是 `{session, status, is_running, team}`。后端始终从已授权 Conversation 解析 Agent 和 Session，不接受浏览器自由指定原生 ID。

职责/指令作为必要的可编辑业务分栏，不再建立独立 Agent 执行配置层。跨服务写入用短数据库事务、明确成功/失败与可重试状态处理；禁止本地先展示成功再吞掉上游错误。网络 I/O 不放在数据库锁内。

员工创建及编辑先持久化必要的未完成写入意图，再进行原生调用；所有未确认关联禁止用于新运行。同一员工的编辑在单进程内串行，防止上游与本地提交顺序相反。原生调用超时不代表未提交，保留未完成状态并核对真实记录后才恢复 ready。创建结果未知时用稳定业务员工 ID 的明确关联标记识别本次创建，不按名称近似、时间戳或事件数量猜测，也不盲目再 POST。只保留这一最小写入关联机制，不引入第二套 Agent 管理或分布式事务框架。为防止超时的旧请求迟到覆盖后续编辑，员工必要扩展保存单调写入修订号，写入意图及原生关联标记携带同一修订号；服务公开 ASGI middleware 按 Agent 串行 PATCH/DELETE 的完整处理，并拒绝低于已接受或已持久化修订的写入。恢复通过同修订幂等 PATCH 屏障与精确读取确认，不以一次读到旧值推断失败。创建结果未知的占位，仅在按标记找到唯一原生记录时绑定；释放未创建占位须核对原生确实缺失，且服务启动代次已变化，保证旧单进程请求已退出。

### 3. 匿名所有者与 workspace 隔离

浏览器只存匿名 UUID、选中项和草稿；后端验证 UUID 并将其作为 owner。采用统一内部 AgentScope 服务租户共享长期员工与原生资源；Conversation 和 Discussion 的读取、流、写入、附件均先由 Nexus 按 owner 过滤。

匿名 UUID 可以被冒用，不是认证。部署说明必须限定受控验证环境。AgentScope 不直接暴露给浏览器；内部扩展回调使用服务端秘密，不把模型凭据或内部密钥发给前端。

原生 workspace ID 采用 `PER_SESSION`；通过公开 manager 扩展按该持久 ID 建立不同物理目录，避免官方 LocalWorkspaceManager 按 Agent 共用目录导致不同匿名用户共享文件、附件或个人记忆。原生短期上下文随 Session 持久化；UI 只显示实际可读取的原生记忆信息，没有长期记忆时如实空态，不建设自有 Memory 系统。

### 4. 原生资源选择与员工删除

Skill、MCP Tools、Knowledge 从官方目录列出，空目录显示空态，不补假目录。员工存资源 ID 默认关联；新建 Session 时通过官方 workspace 关联接口与 `knowledge_config` 落实绑定。能力失效必须报错，不能只存标签。工具适配器以明确允许目录控制实际暴露能力；保留原生专用 Skill 读取工具与实际所选 MCP 工具，并在模型 schema 与权限检查两处约束。第一阶段不注册延后第三阶段的 PeerSend；默认不开放 Bash/PowerShell、任意宿主文件读写、Task、Team、Schedule 和 reset_tools。读取 Skill 文本不等同执行其脚本，脚本型能力缺少允许工具时明确报告限制，不假称运行成功。

原生运行退出屏障还须覆盖已登记但尚未取得 Session 锁的任务：通过公开依赖读取真实 chat task registry，冻结 admission 后等待已受理 Task、Session lock 与后台 registry 全部排空。公开 MessageBus 扩展在取得运行锁前后拒绝冻结 Session，覆盖原生 wakeup，避免冻结后再生成错误消息尾部。服务内临时冻结集合由 Nexus 持久 deleting 事实恢复，状态查询失败拒绝放行，不另建服务侧员工状态表。

原生保存异常可能被日志吞掉，idle/Task done 不能证明历史提交。通过公开 Storage 子类观察原生 upsert_message/update_session_state 的真实提交结果，并持久保存最小的待确认 reply/message ID、内容摘要及写入确认索引；归档前从原生 storage 读回核对全部未确认尾部。此索引不保存输出全文、不承担运行编排或另一套 Runtime 状态，原生消息仍是唯一内容事实源。重启不能把丢失观察当作提交成功；无法确认或已有未收敛保存失败时阻止 DELETE。

官方 DELETE Agent 会级联删除其 Session，而原型明确承诺历史保留。删除先冻结新触发和在途投递，向原生运行发中断请求，并等待真实运行退出、原生状态 idle 与最终持久化完成；202 中断受理和 REPLY_END 事件均不能代替这一屏障。随后完整分页导出该员工原生历史和附件引用为只读归档，再真实调用原生 DELETE；成功后标记已删除并从可运行列表/讨论成员中移除。私聊历史菜单提供已删除员工历史入口，不新增页面。归档失败禁止执行 DELETE；上游删除失败保留可重试删除状态，禁止发起新运行。不能用单纯 soft-delete 冒充已删除原生 Agent。

### 5. 私聊流、断线与幂等

Conversation 与原生 Session 一一稳定绑定。同一员工允许多个 Conversation。创建会话先落原生 Session 及模型、能力绑定，失败清理未完成资源并明确报错。

`POST /chat/` 是触发接口，返回 `started`；输出由 `GET /sessions/{sid}/stream` SSE 提供。历史接口提供 `event_cursor`，先加载历史，再从 cursor 续订，按原生 event/message/reply ID 去重；不把 `started` 或 HTTP 流关闭当成模型完成。官方瞬时 replay 最多保留 1000 条事件，assistant 消息在运行 finally 才写入 SQL；长回复重连仅靠 cursor 可能漏掉中间增量，因此流内容只作为临时显示，运行实际 idle 后再次读取完整原生历史，按 reply/message ID 校准最终内容。运行状态以官方状态接口与终态事件结合确认；重连后补历史及状态，避免旧流污染新会话。发送使用客户端幂等键，重复请求不启动第二次推理。取消/服务异常不得伪造成功回复。

发送前固定原生 input Msg ID，保存必要未完成发送意图、内容摘要及提交 generation；相同客户端键不同内容拒绝。原生 started 可能发生在 user Msg 落盘之前，Task 暂空也不证明未受理；未知结果须核对真实持久消息、真实 Task 与代次，不盲重发。原生确认写入后清理瞬时意图载荷，不复制活动 history。取消前未实际执行的输入以明确取消记录保留并纳入只读归档；执行排空和已受理输入归档完整性分别核对，后者未知时不能宣称完整归档。公开队列无 peek，不用 drain/drop 检查或制造成功。

### 6. 基础团队讨论与第三阶段边界

官方 Team 的被邀请长期员工仍受 worker 强制汇报规则限制，因此不将该机制当作平等讨论。每名讨论成员使用同一长期 AgentRecord 下的独立原生 Session，`team_id=None`。

人类 @ 选择稳定员工 ID。明确 @ 的员工收到实际消息；无 @ 仅保存讨论消息，不强制轮流发言。服务端校验 owner、成员、enabled 和 Session 绑定，以输入幂等键防止重复触发；目标忙碌时明确呈现，未知提交不盲目重发。

讨论历史以人类消息和原生回复引用聚合，不复制活动 AgentState 或建立员工从属关系。每次真实回复必须保留可核对的源 Agent、Session 和 native message/reply ID。

PeerSend、员工自行选择同事、异步回信和有界自主投递按最新要求移至第三阶段，原方案见 `docs/AGENTSCOPE_PEER_HOOKS.md`。本 change 不注册该工具，不将三员工自主协作链作为第一阶段或第二阶段前置；不得以固定轮流、关键词或模拟回复替代。

### 7. 附件真实读取

Nexus 保存随机存储名、原始文件名、MIME、大小、owner 和 Conversation/Discussion 归属。单个最多 20 MiB、每消息最多 6 个、拒绝空文件；服务端流式限量，不信 MIME、扩展名或前端检查。下载/预览仍校验 owner，拒绝路径穿越及未归属附件；HTML/SVG 等主动内容按下载或安全内容策略处理。

图片使用原生 `DataBlock` 传给实际支持视觉的模型；文本/代码/日志由服务真实读取并标明截断，不以文件名推测内容。二进制及不支持格式明确提示。DeepSeek 官方说明 `deepseek-flash` 当前为 V4.1 Flash，具备原生视觉能力，但须通过真实图片联调证明集成正确。

### 8. UI 与验证

三个页面及底部导航、绿色主题、员工抽屉、讨论弹窗、多对话切换、附件预览与 mentions 参照原型。共享消息列表、编辑器、附件组件，使用 Element Plus 原生控件，不复制 mock 或 IndexedDB 业务持久化。

每阶段按 AGENTS.md 的工具优先级由外部 Coding Agent 小步实现，Codex 独立检查代码、执行验证、发现问题后明确修复任务。唯一任务清单是本 change `tasks.md`。模拟测试与真实模型/服务测试分开报告；模型失败、缺凭据或无法验证时保持未完成并记录原因。只有全部 Scenario 满足才同步并归档 OpenSpec。

## Risks / Trade-offs

- [官方 API 快速变化] → 固定 release 与依赖锁，保存真实 OpenAPI 契约并针对性测试。
- [原生 Agent 与业务数据跨服务写入] → 显式失败处理、必要幂等和补偿，不长期保留猜测式 fallback。
- [匿名 UUID 可冒用] → 回环服务/受控验证部署；未来真实 UID 绑定保持 owner 字段可扩展。
- [单进程消息总线重启丢瞬时事件] → 原生消息/状态持久化，重新订阅补历史；不宣称多进程恢复。
- [删除级联与历史承诺冲突] → 先完成只读归档，再执行真实删除；对失败路径测试。
- [人类 @ 重试与并发冲突] → 真实输入幂等、目标单 Session 约束、未知提交核对；自主投递预算留待第三阶段。
- [Knowledge 依赖真实 embedding 配置] → 由 AgentScope 引导公开 OllamaCredential，使用本机标准 Ollama 模型服务与 embeddinggemma:300m（768维）完成真实索引/检索；向量与文档存储随 AgentScope data_dir 持久化。该模型服务属于模型层依赖，不增加业务微服务或配置页面。缺配置时明确不可用；所选 KB 检索失败必须在模型调用前中止，不沿用原生 log-and-skip 的裸模型成功行为。
- [本地 workspace 工具可接触宿主文件] → 默认仅允许本版本需要的目录/工具，所有员工启停在服务实际运行前再次检查。

## Migration Plan

无旧业务迁移。先完成规范和契约，再依次部署 AgentScope、员工管理、私聊、团队、联调。各进程可独立停止；持久数据保存在新仓库 ignored 的 `data/`，部署前备份。回滚停止新服务并恢复对应版本数据，不操作 Astra。

## Open Questions

第一阶段尚未完成，具体门槛见 `docs/PHASE1_GATE.md`。自主 peer 方案已延后第三阶段，未实施、未验收；不会以此阻止第二阶段，也不声称模拟验证通过。
