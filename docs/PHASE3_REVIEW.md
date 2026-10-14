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
