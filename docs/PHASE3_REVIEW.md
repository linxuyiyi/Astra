# 阶段三验证记录

## 员工创建与编辑（任务 3.1）

原因：员工业务字段需要真实更新原生 Agent，且跨服务超时、并发与本地保存失败不能留下可运行的错误关联。

改动：Employee 只保存必要扩展、顶层 Agent ID、未完成写入意图和单调修订；真实 POST/PATCH；每员工业务操作串行；通过公开 ASGI middleware 串行原生 PATCH/DELETE 并拒绝迟到旧修订。创建请求绑定实际服务启动代次，未知创建以规范末尾员工标记核对，禁止盲目重复 POST。reconcile 提供明确恢复路径，未确认时禁止作为 ready 使用。

Codex 独立审查及修复闭环覆盖：受理后超时/5xx、异常成功响应、并发编辑、在途创建与释放竞态、迟到旧写、读取失败关闭、取消后的实际写入结束、标记正文误匹配与创建配置漂移。

验证：Codex 亲自执行 `uv run pytest -q`，147 passed。部分故障为明确标注的模拟测试；原生合同测试使用官方 ASGI 和真实 SQLite。另亲自通过交付控制脚本启动两进程并安全注入已有模型凭据，执行 `scripts/smoke_employees.py --with-model`，证据 `evidence/phase3/smoke-employees-20261013T180435Z.json` 为 passed、scope=crud+model。通过业务 PATCH 写入随机验证值后新建 Session，真实模型流式返回该值并完成原生历史校准。密钥未写入源码、命令参数或证据。

风险：只保证本版本单进程部署，不支持多副本并发；无旧数据迁移。资源、运行 guard、启停、删除归档和前端尚待后续任务，不能据此声称员工管理阶段或整个系统完成。工作区 ID 不同尚不代表文件隔离。

任务 3.2.1（物理 workspace 隔离）已完成：官方 LocalWorkspaceManager 2.0.10 的 workdir 固定为 `basedir/agent_id`，仅设置 PER_SESSION 不足以隔离文件；已用公开 `WorkspaceManagerBase` + `LocalWorkspace` 实现 `SessionWorkspaceManager` 按原生持久 workspace ID 的 SHA-256 建独立目录并装配。真实验证：`tests/agent_service/smoke_workspace.py`（真实进程 start/stop/start + 官方 `/workspace/files`）证明同员工双 Session 物理目录不同、A 写文件 B 不可读、进程重启后同 ID 同文件仍在、B 仍不可读，并核对路径穿越防护、并发同 ID 只初始化一次与 `close`/`close_all`；证据 `evidence/phase3/smoke-workspace-20261013T181238Z.json`。`tests/agent_service/test_workspace_manager.py` 为对应 ASGI/真实文件系统测试。未调用模型，不声称推理验证。

Codex 复审加固：`get_workspace`/`close`/`close_all` 改为共用一把 manager 生命周期锁，覆盖初始化与关闭全过程，避免了「close 返回后迟到 initialize 再发布缓存」与同 ID 双实例/孤儿资源；新增确定性交错测试（暂停 initialize → 并发 close/close_all → 恢复）确认不复活、不漏关、不双实例。真实冒烟严格要求 `start`/`stop` 退出码为 0，并在重启前后核对 `generation_id` 已变化，防止停止失败时读到旧健康进程却误判为已重启。

Codex 复核 3.2.1：初始化与 close/close_all 的真实交错回归已修复，生命周期使用同一最小 async 锁；smoke 严格校验控制命令退出码及启动代次变化。亲自执行全量 pytest，155 passed；亲自执行真实进程 smoke_workspace，证据 `evidence/phase3/smoke-workspace-20261013T181322Z.json`，A 读取200、B读取404，真实停止/启动后新 generation 且同样结果，原生 Agent 清理成功。证明物理目录隔离和持久恢复，不宣称宿主文件沙箱或模型验证。

## 任务 3.2.2：资源与运行边界已验证，Knowledge 仍待完成

原因：原生默认工具超出 Nexus 范围，资源标签必须对应实际可用能力；共同 Session 创建还需要 owner 关联、幂等及未知结果恢复。原生 stateful MCP 存在关闭 scope 错误，本版本通过公开 API 明确收敛为无状态 Streamable HTTP，不修改核心。

改动：安全资源目录与只读 MCP 支持投影；员工真实资源 ID 关联和启停；必装 fail-closed guard；按可信 MCP 身份与精确工具名重建 Toolkit，保留原生权限决策；受控工具绑定真实 Session 且不 offload。共同 factory 持久化创建意图，精确标记/模型/workspace/能力读回确认后 ready，未知结果不重复 POST；安全释放要求新代次中的全新扫描及代次稳定；畸形响应不冒充空目录，外部 Session 不清理。内部秘密仅 start 时锁内原子落盘，status/stop 无写入。

Codex 独立全量验证：`.venv/bin/python -m pytest -q`，249 passed；`git diff --check` 通过。单元/故障注入与真实服务证据明确区分。

Codex 真实双进程及 DeepSeek API 验收：`scripts/smoke_resources.py --with-model`，证据 `evidence/phase3/smoke-resources-20261014T144308Z.json`，全部检查 passed。真实选中 MCP 工具调用返回42；员工禁用后直接原生 chat 仅 REPLY_START/REPLY_END(error)，无 MODEL_CALL_START/成功回复；两个服务真正 stop/start 后 generation 不同，原绑定恢复、MCP 重新发现且模型再次真实调用返回42。独立传输证据 `evidence/phase3/mcp-transport-root-20261014T143537Z.json` 覆盖正常、超时、慢调用取消后再次调用；新增未结束客户端 task 和 fixture live thread 均为0。

Codex 再次验证职责更新：`scripts/smoke_employees.py --with-model`，`evidence/phase3/smoke-employees-20261014T144442Z.json` passed；业务 PATCH 后经共同 factory 创建的新会话使用新指令，真实模型回答含随机验证值。

保留失败证据 `smoke-resources-20261014T143431Z.json`：工具调用已真实成功，但验收脚本把先前成功历史当作禁用后的新回复误报；已修为完整历史 baseline ID 与本轮 reply/新增消息归因，并要求明确 error 与 idle，成功复跑见上。未删除或改写该失败证据。

限制：当前只支持无状态 Streamable HTTP MCP；stdio/SSE/stateful 项明确不可选。非只读工具仍保留原生 ASK/DENY，不强制 ALLOW，不以只读计算证明所有写工具已经可用。真实 Skill 绑定使用测试专用公开 Hub，未将 Markdown 读取当脚本执行。本地 EmbeddingGemma 300M 已真实生成768维向量，完整 Knowledge 索引/检索/回答尚未通过，因此 tasks.md 的3.2.2保持未完成；删除归档、前端、私聊业务与团队交流仍待后续任务。

## 任务 3.2.2 Knowledge 完整链路独立验收

改动：官方 OllamaCredential 固定ID引导、本机 embeddinggemma:300m；公开 CollectionPerKbManager/QdrantStore/LocalBlobStore，目录从实际 data_dir 派生且生命周期沿用原生；TextParser/ApproxTokenChunker 索引。共同 Session 显式 static 参数并读回核对，ready 清理瞬时创建意图，不长期复制模型参数。子进程 NO_PROXY 合并回环并保留原代理设置。

严格检索仅观察原生 search：公开 KnowledgeBase 包装在受控运行中拒绝未选KB，委托真正检索并记录本轮成功/失败；空选择也建立独立 ContextVar。后端授权核对实际原生KB IDs/参数与可信快照；公开on_reasoning只抑制未真正调用的MODEL_CALL_START，随后on_model_call严格拒绝，异常自然展开原生RAG清理，不重写检索或私有重排。独立REST检索无Agent运行上下文时仍可用。

Codex亲自执行全量pytest：283 passed；OpenSpec strict validate与diff check通过。真实框架单元复核证据 `evidence/phase3/rag-cleanup-root-20261014T151649Z.json`（recording model，明确不算AI联调）：部分KB漏装时实际生成1个KnowledgeBase Hint，结束残留0、模型调用0、无START、ContextVar已清理。单元还覆盖未选KB的inner.search调用0、空选择、部分失败、失败后下一轮及并发Session隔离。

Codex亲自通过交付控制脚本启动两真实进程并注入真实DeepSeek凭据，执行 `scripts/smoke_knowledge.py --with-model`；`evidence/phase3/smoke-knowledge-20261014T151147Z.json` 全部13项检查passed。两份真实TXT各ready且chunk_count=1，实际embedding/search命中目标文档校验值；用户提问与员工指令没有该值，真实模型基于检索回答 ZS8d9f867a 并出现原生HINT_BLOCK。空库零结果仍可调用模型；embedding不可达、部分KB不可构建、KB删除均明确error，模型前中止。真实stop/start后generation变化，KB/文档chunk/search/原Session绑定恢复，真实模型再次按文档作答。密钥未写入证据。

3.2.2验收已覆盖资源目录/真实绑定、启停guard、共同factory、物理隔离、MCP真实模型调用及Knowledge真实检索，允许勾选。限制仍为单进程受控环境、无状态Streamable HTTP MCP、所选脚本Skill不自动获得Shell能力；删除归档与三个页面仍未实现，不据此声称完整员工管理阶段或系统完成。

## 任务 3.2.3.1：内容摘要基础，完整屏障仍未完成

外部 Coding Agent 已实现公开 Msg/AgentState 的纯摘要函数。消息保留身份、顺序、实际附件 source、工具参数/输出/metadata、合法 Thinking extra 与权限规则；仅按公开 block 路径去除构建时间及工具输出临时文本 ID。State 对实际完整 model_dump 计算摘要，不复制内容到业务存储。

Codex 独立审查复现并闭环四项问题：多模态 Hint 与非空 PermissionRule 原先无法 JSON 序列化，Thinking 的 signature/redacted_thinking_data 原先漏入摘要，metadata/structured_output 内合法 Pydantic 值原先也无法编码。修复后 Codex 亲自运行回归探针及全量 `.venv/bin/python -m pytest -q`，297 passed（12.34s）；OpenSpec strict validate通过。新增14项测试使用真实公开事件重建，验证随机嵌套ID/时间差异不误报，实际文本、工具payload、权限规则、附件内容和State变化仍可区分，嵌套 Pydantic 值与原生 JSON 安全等价对象摘要相同。

本小步没有调用模型，尚未装配存储观察、持久索引或冻结/排空接口，不作为真实保存、重启或删除验收证据。3.2.3.1保持未完成；后续仍须真实存储提交/失败、未确认记录重启、慢保存与重复取消、独立进程和真实模型尾部验证。

## 任务 3.2.3.1：持久索引基础独立验收（2026-10-15）

外部 Coding Agent 实现独立 aiosqlite 索引，只存可信绑定、message/checkpoint ID、SHA-256 摘要和提交确认；固定目录内 DB/marker nonce、版本核对，真实短事务提交，旧 pending 不被后续写入覆盖，未知摘要不得确认。尚未装配到官方消息/State 提交。

Codex 先前独立运行曾打印 315 passed，但进程持续不退出，因此明确判为失败。OpenCode 按 Root 复现修复：连接在 PRAGMA 前登记所有权并覆盖 init/verify 取消清理；close 失败明确失效且保留真实句柄可重试；rollback Task 自身取消同样污染失效；父目录 fsync 失败不再伪装初始化成功。线程栈核实原挂起来自损坏库路径泄漏的 aiosqlite 工作线程。

修复后 Codex 亲自执行 23 项索引针对性测试与全量 `.venv/bin/python -m pytest -q`：**320 passed in 12.61s，实际退出码 0**。另用真实临时 SQLite 注入 close 自身取消，验证保留句柄并在恢复后真正关闭。独立审查的真实 Task/Event 探针进一步覆盖 rollback 正在清理时的重复取消，以及 rollback 取消+close 失败组合，旧 pending 经无关写入及重开仍保留。

证据限制：现有重复取消单测连续 cancel，未等待 rollback 进入；独立探针补充了该时序验证。未做断电实测或模型调用，不能将索引单模块验收称为完整落盘屏障、业务删除或第一阶段完成。3.2.3.1仍未勾选，下一步才接真实 Storage、事件观察和读回。

## 任务 3.2.3.1：真实存储观察接入独立验收（2026-10-15）

OpenCode 已通过公开 AsyncSQLAlchemyStorage、InMemoryMessageBus、MiddlewareBase 扩展接入真实消息/State 提交观察；入口冻结副本、原生 super 提交成功后确认，内容 hash 与实际终态 hash 合并。单次 Session 运行锁内共享观察多个 reply、一次 State；成功装配后的公开 inbox consumer 注册先建 unknown State 期待，setup 失败只观察诊断消息。continuation 从可信 State.reply_id 读取旧消息，保留 name/id，支持 parked interrupt，忽略原生过滤的 fallback Msg。读回先验证真实 Session 绑定，再逐条精确确认，未知/缺失/不匹配继续阻断。

Codex 独立审查以真实 SQLite、Task/Event 复现并闭环：closed scope 迟到写覆盖；State.session_id 不匹配仍提交；续写读取失败静默失去期待；parked interrupt 遗漏；已经进入原生保存的旧 writer 在锁释放后覆盖新状态；子 Task 继承上下文绕过锁；等待锁取消后遗留上下文；内部 drain waiter 取消冒充排空。

最终采用 scope 内存 writer lease：首次 await 前受理，覆盖原生提交与索引确认；在真实 Session 锁内部关闭新受理，等待已受理 writer 全部退出后才释放锁。Scope 在实际取锁后建立，同 Session 重入检查真实 owner Task；继承 scope 仅供 writer 关联，不构成锁所有权。内部 waiter 失败/取消与外层重复取消均不能提前释放锁，不增加 Run 状态机或持久执行层。

Codex 亲自执行全量 `.venv/bin/python -m pytest -q`：**362 passed in 14.49s，实际退出码 0**；strict OpenSpec validate 与 diff check 通过。另亲自以纯公开 Bus/真实 Task 探针确认子任务绕锁被拒、等待超时后 ContextVar 恢复、内部 waiter 取消仍等待真实 writer 清零再传播。36 项组件回归使用实际临时 SQLite；6 项官方 app 测试使用公开 recording 模型，覆盖正常/并发、setup 失败、模型报错终态、confirm 失败短路 State、取消遇慢保存失败，明确不算真实 AI 联调。

边界：本小步未实现冻结/admission/quiesce、业务删除归档、私聊/附件/UI或第二阶段。尚未做新观察装配后的真实模型/独立进程尾部验证；整体 3.2.3.1 仍未完成，任务清单保持未勾选。旧 State 未确认后被新版本覆盖时仍可能持续阻断，不能猜测式清除；下一步骤须保留该失败安全规则。

后续真实回归：Codex 从提交 `ddb6e5342aa10a2ed3752e696dd9bf753b81bbf4` 提取隔离源码副本，通过交付控制脚本启动两真实进程，执行 `smoke_resources.py --with-model`，正常退出 0。真实 DeepSeek 回复、所选 HTTP MCP 调用、禁用后运行拒绝、双服务真正重启后的绑定恢复与再次工具调用共 9 项检查通过；证据 `evidence/phase3/smoke-resources-20261015T005716Z.json`。进程停止后只读取索引元数据，10 条消息与 5 条 State 全部 confirmed，unknown digest 为 0，见 `persistence-real-model-root-20261015T005716Z.json`。凭据仅通过环境注入，未写入命令参数或证据。此为新观察装配的真实模型/MCP/重启回归，尚不是冻结/quiesce、业务删除或取消尾部验收。

同一隔离提交的 Knowledge 真实复测：首次因专用 Ollama 11435 未运行而失败，保留 `smoke-knowledge-20261015T010031Z.json`。确认端口无监听后仅重新启动 Nexus 专用模型目录的本机 Ollama，再运行 `smoke_knowledge.py --with-model --keep-evidence`，真实本地 EmbeddingGemma 索引/检索、DeepSeek 按随机文档值回答、异常前置拒绝和双进程重启共 13 项检查通过，正常退出 0，证据 `smoke-knowledge-20261015T010803Z.json`。停进程后索引元数据计数见 `persistence-knowledge-root-20261015T010803Z.json`；所有消息与 State 均 confirmed，无 unknown digest。保留原始失败与成功证据；此结果仍不代替尚在审查的冻结/取消尾部或删除归档验收。

## 任务 3.2.3.1：冻结与排空组件独立审查（2026-10-15）

OpenCode 实现持久 deleting/稳定删除操作、短事务只读 run-fact、共享 Agent admission 栅栏、运行锁取得前后冻结检查、真实 chat/BG Task 取消及自然清理、停写后逐条读回。配置内部秘密时查询不可用即拒绝；freeze/unfreeze 校验内部 token 与精确租户，非法/非有限超时被拒绝。没有业务 DELETE/归档或新 Run 状态机。

Codex 真实 Task/Event 与临时 SQLite 独立复现并交回修复：接管已取消 Task 时二次 cancel 打断尾部；captured Task 仍活跃但 registry 消失时误报排空；慢 writer 未结束时读回把 pending 从1提前清成0；读回期间迟到 registry Task 漏判；取消 handler 不传播；多 Session 逐个读回遗漏早先 Session 的迟到 Task。修复后已重复独立探针：活跃 writer 时 execution/persistence 均 false 且 pending 保持1；单/多 Session 迟到 Task 均阻断；外层取消在安全等待后如实传播。捕获 Task 是否完成始终参与事实判断，首次接管已在取消的对象只登记并等待，全部读回后统一刷新全部 Session 存活事实。

Codex 亲自执行全量 `.venv/bin/python -m pytest -q`：**446 passed in 17.71s，真实退出码0**，两个独立探针也正常退出0；diff check通过。新增脚本负向断言拒绝模型error/空基线、未捕获在途、保存未确认、缺失历史及停止失败冒充通过。资源 smoke 适配真实 admission 409 并保留 guard error 验证，其他上游异常仍失败。

当前为组件与公开框架验收，真实模型进程取消尾部及本实现重启尚待下一步亲自执行；3.2.3.1保持未勾选。执行排空与落盘证明不等于已受理输入完整性，后者仍明确 not_verified；共享发送意图内核已作为 3.2.3.2.1 必要前置纳入唯一 tasks.md，完整私聊/SSE/UI仍留在4.x复用。

真实进程回归（固定源码提交 `5cc52122e8a24f1054e86b267368833afa893b01`）：`smoke_resources.py --with-model --keep-evidence` 正常退出0，真实DeepSeek、MCP、admission直接409拒绝及双服务重启共9项检查通过，见 `smoke-resources-20261015T013207Z.json`。两次取消专项 `smoke-freeze-20261015T013300Z.json` 与 `smoke-freeze-20261015T013916Z.json` 分别在长运行及基线等待输出时超时，均保留失败与私有临时数据；未进入freeze验证，不能算通过。独立最小provider探针当时模型列表HTTP200、stream Chat Completions HTTP503。`freeze-regression-root-20261015T013916Z.json` 记录范围；3.2.3.1继续未勾选，等待真实取消尾部补证。

另完成无模型推理的真实进程故障验收：在第一次取消专项的隔离测试库追加一个显式unknown pending期待，持久写入后启动两服务，核对 execution_drained=true 但 persistence_verified=false；真正停止并重启两服务后代次改变、稳定删除操作不变、unknown期待仍保留且新chat继续409拒绝。证据 `freeze-pending-process-root-20261015T014726Z.json`，真实退出0。这是合成未知写入故障注入与真实服务重启，明确不算AI取消尾部验证；使用原失败沙盒继续探针，原失败JSON保持不变。

## 任务 3.2.3.1：真实模型取消尾部最终验收（2026-10-15）

模型接口恢复后，Codex从固定代码提交 `5cc52122e8a24f1054e86b267368833afa893b01` 的隔离源码重跑 `smoke_freeze.py --with-model --keep-evidence`，真实退出0。`smoke-freeze-20261015T125804Z.json` 共13项检查通过：真实deepseek-flash正常基线；观察到实际模型调用与文本且仍busy；持久deleting后拒绝新运行；freeze确实捕获活跃真实Task并取消到interrupted，保留已输出内容；实际chat/锁/BG排空及pending为空；服务真正stop/start代次改变，持久删除身份与运行拒绝仍成立，重复quiesce继续确认。

Codex在所有服务停止后通过公开Storage独立读取最终Msg及完整State，精确核对原生user/agent/session、State.session_id及reply_id，最终消息确为interrupted。消息与State摘要分别匹配confirmed索引；4条message、2条State全部confirmed，unknown摘要0，证据 `freeze-tail-root-20261015T125804Z.json`。结合前述真实进程unknown pending重启阻断、组件446项与独立交错探针，3.2.3.1允许勾选。此前503/超时失败记录不删除、不改写。

此验收仅完成运行冻结、排空及保存证明，accepted_input_archival仍明确not_verified；共享发送内核、完整归档/真实DELETE、前端与完整私聊/附件/基础团队仍未交付。按用户继续处理前端的当前上下文，将3.3拆成独立管理基础与后续删除接入，先实施不依赖删除API的3.3.1；不以该局部界面冒充第一阶段完成。
