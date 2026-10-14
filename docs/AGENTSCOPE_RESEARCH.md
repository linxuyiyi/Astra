# AgentScope 2.0 集成研究

研究日期：2026-10-14。对象为 Nexus v1.0.1。本文区分官方源码事实、本机已执行验证和待实现设计；不把框架示例或源码阅读当作真实模型联调完成。

## 1. 固定版本和资料边界

采用 **AgentScope Python 2.0.10**，官方 tag 为 `v2.0.10`，固定 commit 为 `e6c014f0be49b2a6f6f62a9453082a0b975484e1`。GitHub release 日期为 2026-10-12，PyPI 发布包存在，要求 Python ≥ 3.11。[官方 release](https://github.com/agentscope-ai/agentscope/releases/tag/v2.0.10)、[固定 commit](https://github.com/agentscope-ai/agentscope/commit/e6c014f0be49b2a6f6f62a9453082a0b975484e1)、[PyPI 2.0.10](https://pypi.org/project/agentscope/2.0.10/)、[Python 和安装依赖](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/pyproject.toml#L20)

本次获取官方 tag 源码的只读路径为 `/tmp/nexus-agentscope-research`。安装和源码示例必须携带版本，不使用浮动 `main` 作为交付依赖。

需要区分以下产品：

| 产品 | 本项目中的定位 |
| --- | --- |
| AgentScope Python 2.0 | 本项目使用的 SDK、原生 Agent Service、Storage、事件、Workspace、Tools 和 Middleware |
| AgentScope Runtime | 旧独立运行时；官方已宣布其能力合入 AgentScope 2.0，计划归档，不再作为新项目额外依赖 |
| AgentScope Studio | 开发可视化工具，不是本项目要对接的员工业务服务 |
| AgentScope Java | 独立 Java 实现，不能将它的 AgentState/Session API 套用于 Python |

来源：[Runtime 官方公告](https://github.com/agentscope-ai/agentscope-runtime#archive-notice)、[Studio 官方仓库](https://github.com/agentscope-ai/agentscope-studio)、[Java 官方仓库](https://github.com/agentscope-ai/agentscope-java)。

文档存在版本滞后：研究时 `/stable/en/index` 重定向至 `2.0.9`，`/latest/en/deploy/agent-service` 重定向至 `2.0.10dev`，而正式包已经是 `2.0.10`。本文契约以固定 tag 源码和已安装 2.0.10 导出的 OpenAPI 为准。旧域名 `doc.agentscope.io` 的 `ReActAgent`、`JSONSession` 等 1.x 示例不可直接使用。[stable 文档入口](https://docs.agentscope.io/stable/en/index)、[开发文档入口](https://docs.agentscope.io/latest/en/deploy/agent-service)

## 2. 官方服务与最小部署

官方 `agentscope.app.create_app` 返回 FastAPI 应用，注册原生 Agent、Session、Chat、Credential、Skill、MCP、Workspace、Knowledge 等路由，并管理运行和存储生命周期。Nexus 可独立部署这一官方应用，通过 HTTP 集成；不需要重写 Agent 执行服务，也无需复制或修改框架源码。[create_app](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_app.py#L113)、[官方服务示例](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/examples/agent_service/main.py)

单进程受控验证环境的最小组成：

- `agentscope[service,storage-sql]==2.0.10`、`aiosqlite`。
- 原生 `AsyncSQLAlchemyStorage`，使用 `sqlite+aiosqlite:///绝对路径/agentscope.db`。
- 原生 `InMemoryMessageBus`，承载当前进程的会话锁、事件和唤醒。
- 原生 `LocalWorkspaceManager`，根目录使用持久化路径。
- 明确使用 `IsolationPolicy.PER_SESSION` 分配持久 workspace ID，并通过公开 WorkspaceManager 扩展将物理目录按 workspace ID 隔离。官方 2.0.10 LocalWorkspaceManager 实际按 Agent ID 建目录，仅配置 PER_SESSION 不足以隔离文件。
- Nexus Backend 与 Agent Service 为独立进程；不引入 Celery、Worker Pool 或 Task 业务状态机。

公开导入路径为 `agentscope.app`、`agentscope.app.storage`、`agentscope.app.message_bus`、`agentscope.app.workspace_manager`。多进程或多节点才考虑官方 RedisMessageBus；不能在多进程部署中继续用 InMemoryMessageBus 假定共享锁和消息正常。[服务入口](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_app.py#L166)、[MessageBus 导出](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/message_bus/__init__.py)

`GET /health` 需要 `X-User-ID`。它检查组件是否已挂载、初始化，**不会实际探测数据库、模型或网络**，所以健康检查通过不等于模型联调通过。服务默认启动生命周期需要正常执行；仅挂载为子应用却不执行其 lifespan 会导致组件未初始化。[health 实现](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_health.py#L29)

官方示例的 Qdrant 使用 `location=":memory:"`，不能直接作为重启后知识索引恢复方案。正式验证应选 Qdrant 本地持久化目录或其他官方持久化 VectorStore，并持久化 BlobStore；当前阶段没有验证知识摄取、检索或索引重启恢复。[示例中的 VectorStore](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/examples/agent_service/main.py#L55)

## 3. 原生 SQL 持久化与建表

`AsyncSQLAlchemyStorage(url, create_tables=True, auto_migrate=False, engine=None, engine_kwargs=None)` 是官方存储实现。创建对象时不打开连接；应用 lifespan 进入 `__aenter__` 后创建 async engine/session factory，再按配置执行迁移或建表；退出生命周期关闭自有 engine。[构造参数](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/storage/_sql/_storage.py#L95)

- `create_tables=True` 调用 `_Base.metadata.create_all`，适用于全新数据库和单节点验证。
- `auto_migrate=True` 调用包内 Alembic `upgrade head`；生产多副本应作为独立部署步骤，避免多个副本并发迁移。
- 具体异步数据库 driver 不在 `storage-sql` extra 中，SQLite 需额外安装 `aiosqlite`。
- SQL Storage 原生保存 credentials、agents、sessions、messages、teams、skills、mcps、knowledge 等记录；Nexus 无需重新保存另一份运行上下文和消息事实源。
- 持久化 Storage 不等于在内存中的消息总线也持久化；进程重启后旧 SSE replay buffer、运行锁等会丢失，刷新时应从原生持久化消息恢复。

来源：[建表与迁移执行顺序](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/storage/_sql/_storage.py#L198)、[SQL 表定义](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/storage/_sql/_tables.py)、[依赖声明](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/pyproject.toml#L74)。

## 4. Agent API 与两个 ID

业务请求通过 `X-User-ID` 标明资源所属用户。官方明确这是临时 header 身份，没有内置可靠认证；当前匿名 UUID 只能用于受控环境，不能作为生产授权保障。[身份依赖](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/deps.py#L35)

| 操作 | 原生接口 | 要点 |
| --- | --- | --- |
| 获取表单 Schema | `GET /agent/schema/v2` | 完整 AgentData Schema，避免旧分段 Schema |
| 列表 | `GET /agent/` | 返回 `agents` 和 `total` |
| 创建 | `POST /agent/` | 返回 `agent_id`，HTTP 201 |
| 修改 | `PATCH /agent/{agent_id}` | 只提交需要修改的字段 |
| 删除 | `DELETE /agent/{agent_id}` | HTTP 204；原生存在 Session 级联清理，见第 10 节 |

创建请求示例只展示必要字段；未展示的 `chat_config` 子字段使用原生默认值：

```json
{
  "name": "防线运营员工",
  "system_prompt": "身份、职责与工作指令",
  "chat_config": {
    "invite_config": {
      "invitable": false,
      "invite_description": null
    }
  }
}
```

创建响应：

```json
{"agent_id": "agent-record-id"}
```

列表中的原生记录形状为：

```json
{
  "agents": [{
    "id": "agent-record-id",
    "user_id": "owner-id",
    "source": "user",
    "data": {
      "id": "different-agent-data-id",
      "name": "防线运营员工",
      "system_prompt": "身份、职责与工作指令",
      "chat_config": {}
    },
    "editable": true
  }],
  "total": 1
}
```

**`AgentRecord.id` 与 `AgentRecord.data.id` 是两个独立生成的 ID，实测不同。POST 返回的 `agent_id` 是顶层 `AgentRecord.id`。员工关联、Session.agent_id、Chat.agent_id、资源装载 query、讨论成员关联都必须使用顶层 ID，禁止使用 `data.id`。**

源码依据：[_RecordBase.id](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/storage/_model/_base.py#L10)、[AgentData.id](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/storage/_model/_agent.py#L95)、[Agent 创建](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_agent.py#L169)、[请求 Schema](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_schema/_agent.py#L15)。上面的响应省略了时间等字段，`chat_config: {}` 只是结构占位，不表示真实返回缺少其默认配置。

原生 Agent 没有 enabled、头像、职责分栏和员工资源默认关联等产品字段。Nexus 保存这些必要业务扩展，AgentScope 保存原生 Agent；姓名和执行 prompt 以原生配置为源，不能让两边出现互不关联的 Agent 定义。修改 prompt 后 ChatService 会按最新 AgentRecord 装配每轮 Agent，新对话自然使用新配置。[AgentData 字段](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/storage/_model/_agent.py#L110)、[每轮装配](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_service/_chat.py#L1172)

## 5. Credential、模型配置与 Session

原生 Credential API 接受包裹在 `data` 中的 provider 配置，例如服务端运维创建 OpenAI-compatible 凭据：

```http
POST /credential/
X-User-ID: nexus-service
```

```json
{
  "data": {
    "type": "openai_credential",
    "name": "service-model",
    "api_key": "从服务端环境变量读取的真实密钥",
    "base_url": "https://模型服务地址/v1"
  }
}
```

返回 `{"credential_id":"credential-record-id"}`。上述密钥文本仅说明来源，不是可运行凭据；配置过程不得把真实密钥写入提交文件或日志。[Credential 路由](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_credential.py#L80)、[OpenAICredential](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/credential/_openai.py#L16)

资源查询：`GET /credential/schemas`、`GET /credential/`、`GET /model/?provider=openai_credential`。虽然 model API 部分注释写了 `openai`，实际 CredentialFactory 按完整 Literal discriminator 匹配；本机内省确认 `openai` 不匹配，`openai_credential` 匹配。[Model 路由](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_model.py#L16)、[factory 实际匹配](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/credential/_factory.py#L90)

模型运行配置属于 Session，不属于 AgentData。创建原生 Session：

```http
POST /sessions/
X-User-ID: nexus-service
```

```json
{
  "agent_id": "agent-record-id",
  "name": "新对话",
  "chat_model_config": {
    "type": "openai_chat",
    "credential_id": "credential-record-id",
    "model": "服务端已确定的真实模型名称",
    "parameters": {}
  },
  "knowledge_config": {
    "knowledge_base_ids": ["knowledge-base-id"],
    "parameters": {}
  }
}
```

返回 `{"session_id":"native-session-id"}`。`workspace_id` 可省略，由原生 manager 按隔离策略分配。`chat_model_config` 可空以创建未配置会话，但空模型配置不能完成真实对话。Nexus 不增加模型配置页面；凭据、默认模型选择和新 Session 模型绑定由 Agent Service 的部署配置或最小服务侧适配负责。

来源：[Session 创建 Schema](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_schema/_session.py#L57)、[原生 ChatModelConfig](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/storage/_model/_session.py#L111)、[Model 从原生凭据装配](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_service/_model.py#L43)。

`GET /sessions/?agent_id=agent-record-id` 返回的是 SessionView 包裹数组，**不是 SessionRecord 数组**：

```json
{
  "sessions": [{
    "session": {
      "id": "native-session-id",
      "agent_id": "agent-record-id",
      "user_id": "nexus-service",
      "team_id": null,
      "config": {},
      "state": {}
    },
    "is_running": false,
    "status": "idle",
    "team": null
  }],
  "total": 1
}
```

列表会裁掉上下文等大字段，展示消息应使用专用历史接口。Session 状态包括 `running`、`idle`、`awaiting_permission`、`awaiting_external_result`；不要只凭 `is_running=false` 认为一轮执行已经正常完成。[SessionView](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_schema/_session.py#L164)

更新使用 `PATCH /sessions/{session_id}?agent_id=...`；模型/Knowledge 配置按原生 Schema 提交。`POST /sessions/{session_id}/interrupt?agent_id=...` 支持停止正在运行或等待人工确认的会话。原生 DELETE Session 会删除消息，不能作为“新建对话”操作。[Session 路由](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_session.py#L377)

额外发现：SQL Storage 的 `get_session(user_id, agent_id, session_id)` 仅按 session ID 和 user ID 取值，`agent_id` 参数未用于校验。Nexus 接口和 PeerSend 投递必须从服务端保存的 Conversation/讨论成员关联推导原生 Agent ID，并校验 Session.agent_id，不能信任客户端自由组合的两个 ID。该项是源码发现，未执行身份串用攻击探针。[SQL get_session](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/storage/_sql/_storage.py#L1329)

## 6. Chat、消息、SSE 与恢复

触发真实推理：

```http
POST /chat/
X-User-ID: nexus-service
```

```json
{
  "agent_id": "agent-record-id",
  "session_id": "native-session-id",
  "input": {
    "id": "nexus-created-stable-message-id",
    "name": "人类",
    "role": "user",
    "content": [{"type": "text", "text": "请分析失败情况"}],
    "metadata": {"conversation_id": "business-conversation-id"}
  }
}
```

原生 Msg.id 可显式提供，也可由框架生成；metadata 是公开扩展字段。不同上下文的同一逻辑消息应携带稳定业务关联，不能以时间戳和文本猜测身份。原生 `/chat/` 是 fire-and-forget，返回 `{"status":"started","session_id":"..."}`，不在响应里流式返回文本。“started”只证明已调度，不证明模型成功。[ChatRequest](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_schema/_chat.py#L10)、[Chat 路由](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_chat.py#L49)、[Msg 字段](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/message/_base.py#L70)

订阅使用 `GET /sessions/{session_id}/stream?agent_id=...&after=event-cursor`，响应为 `text/event-stream`。事件通过 `data: JSON\n\n` 发送，心跳为 `:\n\n`。先订阅再 replay，由原生代码按总线 entry ID 消除交界处重复。浏览器 EventSource 无法直接设置 `X-User-ID`，Nexus 应用 fetch streaming 或由业务后端代理。[SSE 路由](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_session.py#L810)

典型事件形状：

```json
{"type":"REPLY_START","id":"event-id","session_id":"native-session-id","reply_id":"reply-message-id","name":"防线运营员工","role":"assistant"}
```

```json
{"type":"TEXT_BLOCK_DELTA","id":"event-id","reply_id":"reply-message-id","block_id":"text-block-id","delta":"文本增量"}
```

```json
{"type":"REPLY_END","id":"event-id","session_id":"native-session-id","reply_id":"reply-message-id","finished_reason":"completed","error":null}
```

事件还包含工具调用、工具结果、思考块、多模态块和人工确认类型。前端需要按 reply_id/block_id 组合内容，按 event ID 去重；结束原因可能为错误、中断或超过迭代预算，不能遇到断流就当成功。上述示例省略 created_at/metadata 等字段。[事件枚举与模型](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/event/_event.py#L25)

历史使用 `GET /sessions/{session_id}/messages?agent_id=...&limit=50&before=message-id`。`before` 是消息游标，`offset` 已废弃且忽略；返回 `messages`、`event_cursor`、`is_running`、`has_more`。刷新时从消息接口恢复，再用 `event_cursor` 订阅后续事件。InMemoryMessageBus 重建后 cursor 可能为空，必须仍能展示 SQL 消息，不能把 replay buffer 当长期历史。[历史实现](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_session.py#L586)、[历史响应](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_schema/_session.py#L228)

Msg.id 的存在不等于公开 `/chat/` 自动提供端到端 exactly-once 保证。网络超时后的重试、讨论消息 outbox 重放和模型重复调用需要 Nexus 的稳定投递 ID、数据库唯一约束以及公开 middleware 去重验证。

## 7. 附件与多模态真实能力

2.0 用户消息只允许 text/data blocks。二进制内容使用 `DataBlock`，不是旧版 ImageBlock/FileBlock；source 支持 Base64Source 或 URLSource。例如：

```json
{
  "type": "data",
  "name": "截图.png",
  "source": {
    "type": "base64",
    "media_type": "image/png",
    "data": "真实图片的Base64数据"
  }
}
```

来源：[DataBlock 与来源类型](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/message/_block.py#L56)、[User Msg 校验](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/message/_base.py#L32)。

没有找到通用聊天附件上传路由；原生存在 Knowledge 文档上传、Skill 目录上传和 Workspace 文件读取，这些不可等同于普通消息附件上传。Nexus 需要完成附件落盘、消息关联、下载和图片预览这一必要业务适配。图片传原生 DataBlock，模型是否理解取决于真实模型能力；文本、代码和日志必须由服务端实际读取后送入模型，或通过真实附件读取工具提供内容。不能仅传文件名便声称文件已被分析。

可使用 `Msg.metadata` 保留附件 ID/读取证据关联。原始文件与下载仍由服务端持久化，不把浏览器 IndexedDB 当事实源；不支持的类型应明确拒绝或标识未读取。

## 8. Skill、MCP/Tools 和 Knowledge

| 目的 | 原生接口与真实机制 |
| --- | --- |
| Skill 资源目录 | `GET /skill`，已安装的用户 library；`GET /skill/{skill_id}` 返回原始 Skill 记录 |
| MCP 资源目录 | `GET /mcp`，已安装 MCP library |
| 查看运行时 MCP 工具 | `GET /workspace/mcp?agent_id=...&session_id=...`，返回实际 MCP 健康状态和 tools |
| 装载 Skill | `POST /workspace/skill/from-library?agent_id=...&session_id=...`，body 为 `{"skill_ids":["..."]}` |
| 装载 MCP | `POST /workspace/mcp/from-library?agent_id=...&session_id=...`，body 为 `{"mcp_ids":["..."]}` |
| 查看已装载 Skill | `GET /workspace/skill?agent_id=...&session_id=...` |
| 移除能力 | `DELETE /workspace/skill/{skill_name}` 或 `/workspace/mcp/{mcp_name}`，携带 agent_id/session_id |
| Knowledge 目录 | `GET /knowledge_bases/`；支持按 ID、名称筛选和分页 |
| Knowledge 关联 | Session 的 `knowledge_config.knowledge_base_ids`；通过创建/PATCH Session 绑定 |

装载 library 返回 `{"added":["资源名称"],"failed":{"资源ID或名称":"失败原因"}}`；每个资源独立处理，HTTP 成功不代表所有资源都已装载，必须检查 failed。Skill from-library 还依赖原安装 hub 可用，不能只保存一个 ID 就认为以后必然装载成功。[Workspace 路由](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_workspace.py#L138)、[装载请求响应模型](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_schema/_workspace.py#L14)、[Skill library](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_skill.py#L17)、[MCP library](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_mcp.py#L22)、[Knowledge 列表](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_knowledge_base.py#L307)

Skill 在 workspace 内按 Agent 归属，MCP 按 Agent/Session 归属；Knowledge 按 Session 绑定。Nexus 的员工配置可保存默认资源关联 ID，但应由原生接口真正装载能力，不创建虚构的 Skills/Knowledge CRUD 平台。原生内置 Python Tools 没有与所有 MCP 工具完全统一的业务资源目录；如果原型 Tools 需要展示内置工具，应使用受控服务侧 tool 注册清单，并验证这些确实在 agent toolkit 中。[Toolkit 装配](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_service/_toolkit.py#L270)

## 9. 平等长期员工通信的最小适配

官方 AgentInvite 会引用既有 AgentRecord，为其创建团队 Session，保留既有名称、职责 prompt 和配置；解散团队时保留被邀请的 Agent。因此它不是临时创建的 SubAgent。[AgentInvite 语义](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_tool/_agent_invite.py#L2)

但原生 Team 仍是 leader-worker 模式：只有 leader 能 invite，worker 只获得 TeamSay；所有非 leader Session，包括被邀请的长期 Agent，都被装入强制向 leader 汇报的 TeamMemberLoopMiddleware。B 可以向已经在队中的 C 发 TeamSay，却不能自主 invite C。不能把这个模式直接称为 Nexus 平等团队交流。[工具分配](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_service/_toolkit.py#L177)、[成员 loop 约束](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/middleware/_team_member_middleware.py#L21)、[ChatService 装配](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_service/_chat.py#L882)

公开扩展足以构建薄的 peer 消息工具，不需要修改核心：

- `create_app(extra_agent_tools=...)` 接收 async factory `(user_id, agent_id, session_id) -> list[ToolBase]`。
- `extra_agent_middlewares` 接收 async factory `(user_id, agent_id, session_id, workspace) -> list[MiddlewareBase]`。
- `MiddlewareBase.on_reply(agent, input_kwargs, next_handler)` 可在推理前调整公开 toolkit，调用 `await agent.toolkit.remove_tool([...])` 移除 TeamCreate、AgentCreate、AgentInvite、TeamSay、TeamDelete，以及本版本不开放的计划/定时工具。
- `MiddlewareBase.on_check_permission` 可检查禁用状态、当前参与者和能力许可；返回公开 PermissionDecision，不能只靠 prompt 禁止越界。
- `MiddlewareBase.on_acting` 收到真实 `tool_call`，可关联 tool call ID 作为投递去重依据。
- ToolBase 子类以 `call` 实现真实发送，返回 ToolChunk；不要依据关键词或预设轮次伪造回复。

来源：[create_app hooks](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_app.py#L253)、[MiddlewareBase](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/middleware/_base.py#L68)、[Toolkit.remove_tool](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/tool/_toolkit.py#L682)、[ToolBase.call](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/tool/_base.py#L159)。

拟采用的消息路径：

1. 三名长期 Employee 分别关联同一组稳定 AgentRecord，每个讨论参与者绑定自己的原生 Session。所有这些 Session 保持 `team_id=None`，不进入原生 leader-worker 执行分支。
2. PeerSend 工具由工厂绑定真实 user/agent/session。模型输入只包括目标员工、正文和必要附件引用，不允许模型自行声明 sender。
3. 工具调用 Nexus 内部投递接口，服务端校验讨论成员、员工启用状态、Session 关联和附件归属，持久化 envelope/去重键/本轮预算。
4. 投递器对目标原生 Session 调用公开 `/chat/`，使用已绑定的 Agent ID 和身份。源工具立即返回已投递确认，**不等待接收者模型完成**，避免 A 等 B、B 又等 A 时形成会话锁环。
5. B 自己的真实推理、工具调用和回答从 B Session 的 SSE 展示；B 可通过自己的 PeerSend 回复 A 或联系 C。没有固定发言顺序。
6. Nexus 仅保存 Discussion、成员、Session 关联和投递状态；原生 Session/消息仍为内容事实源，讨论历史保存必要消息引用或公共发言 envelope，不复制私有推理上下文。
7. 人类输入创建 exchange ID，约束消息数、链路深度、总耗时，检查唯一投递 ID并终止循环。重启后 pending 投递的恢复和 dedup 必须实测，不假设 `/chat/` 天然 exactly-once。

这是基于公开边界的设计可行性结论，目前未完成三 Agent 真实模型验证。整个服务要保持独立进程，业务后端不能导入私有 `_service` 或 `_router` 调用框架内部方法。

## 10. 已选定的匿名隔离、Memory 与删除历史策略

### 10.1 匿名浏览器与统一员工

最终设计采用**统一内部 AgentScope 服务租户 + Nexus Conversation/Discussion owner 过滤 + PER_SESSION workspace**，与 [OpenSpec design](../openspec/changes/nexus-v1-0-1/design.md) 保持一致。员工 Agent、Credential、Knowledge、Skill/MCP library 都归内部租户，避免每个浏览器复制一份员工或一套资源。浏览器不直连 AgentScope；Nexus 对对话、历史、流、发送、附件和讨论成员逐项校验匿名 owner，之后才用内部 `X-User-ID: nexus-service` 调用 AgentScope。原生 ID 只能从已授权业务关联中推导。

这是 Nexus 业务后端提供的匿名所有者隔离，不应宣称不同浏览器使用不同原生 tenant。匿名 UUID 本身可以被冒用，因此仍限受控验证环境。AgentScope 和内部扩展回调只接受受控服务请求，回调秘密和模型凭据不下发前端。

官方 ResourceAccessPolicyBase 可以共享 Agent、Credential 和 Knowledge，但不含同样的 Skill/MCP library 跨 owner 机制；本版本不采用跨 owner 路径，也不依赖跨 owner 删除或遗留 Session 的实现细节。[公开 policy](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/access/_policy.py#L79)、[ResourceKind 范围](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/access/_policy.py#L19)

### 10.2 Memory

短期上下文由原生 AgentState/Session 持久化，不重建一份 Nexus Memory。长期记忆可用官方 AgenticMemoryMiddleware，数据为 workspace 下 `Memory/MEMORY.md` 和 Markdown 记忆文件；也有 Mem0/ReMe 扩展，但不是现成的统一 Memory CRUD 服务。右侧只能展示实际存在的记忆，不能塞入演示条目。[AgenticMemoryMiddleware](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/middleware/_longterm_memory/_agentic_memory/_middleware.py#L359)

原生 workspace 默认 PER_AGENT，实际共享范围是 `(user_id, agent_id)`；在统一内部租户下直接使用默认值会把不同浏览器的文件或个人记忆混到同一员工目录。最终设计显式选 PER_SESSION 分配持久工作区 ID；后续源码审查发现官方 LocalWorkspaceManager 实际将 workdir 固定为 basedir/agent_id，忽略不同 workspace_id。因此必须用公开 WorkspaceManagerBase 与 LocalWorkspace 扩展按持久 workspace_id 建立独立物理目录，并验证真实写入隔离及重启恢复。仅验证 ID 不同不能证明文件隔离。短期上下文随原生 Session 保存；此版本不承诺跨 Conversation 共享个人长期记忆。[workspace 隔离分配](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/workspace_manager/_base.py#L85)

AgenticMemory 的默认指令鼓励主动写入；本版本不开发经验自动沉淀，因此不默认启用其整套行为。UI 只展示实际可读取的原生记忆信息，没有长期记忆文件时显示空态；不建立 Nexus 自有 Memory 系统，也不写模拟条目。

### 10.3 删除员工与保留历史：先只读归档，再真实 DELETE

HTML 原型 `/Users/niuniu/Downloads/Nexus_v1.0.1.html:224` 明确提示删除后历史对话仍保留。而原生 `DELETE /agent/{id}` 调用 SessionService.delete_agent，通常会遍历该 owner 的关联 Session，取消运行并删除消息、清理工作区中的 Agent 资源。**不能直接硬删原生 Agent 后仍声称同 owner 的原生历史会保留。**[原生 DELETE 路由](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_agent.py#L287)、[SessionService 删除](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_service/_session.py#L478)

最终设计用一次性只读归档解决原型承诺与原生级联语义的差异，执行顺序为：

1. 先将员工置于删除中状态，统一阻止人类入口、peer 投递和 Agent middleware 发起新运行；中断已有 Session，并确认已结束或已按策略处理失败。
2. 分页完整导出该员工的原生消息、身份快照和附件引用，保存为永久只读、不可执行的归档。归档完整性失败禁止执行原生 DELETE；不得只导出历史接口默认的 50 条。
3. 归档成功后真实调用 `DELETE /agent/{agent_id}`，检查原生响应与资源实际状态。
4. 原生删除成功后标记员工已删除，从可运行列表和讨论成员中移除；原有历史通过业务映射访问一次性归档，私聊历史菜单保留入口，不增加新页面。

活动消息只以 AgentScope 为事实源，归档只用于删除后的历史，不作为可继续聊天的第二套 Session。不用单纯 soft-delete 冒充删除原生 Agent，不依赖跨 owner 的漏删或 orphan Session。

剩余失败风险必须实测：中断过程中仍有尾部事件时何时取最终快照；归档完成后 DELETE 失败时保留可重试删除状态并持续禁止新运行；DELETE 成功但本地状态提交失败时如何在重试中确认上游已不存在；部分归档或重复请求不能覆盖已完成快照。HTTP 404 是否可视为先前删除已完成，应同时校验已有完整归档和本地删除操作 ID，不能吞掉任意异常。服务间网络 I/O 不放在数据库锁内。当前尚未执行这条删除归档链路，不能报告为已通过。

## 11. 本机验证证据与验收边界

本机已安装 `/tmp/nexus-agentscope-venv`，依赖为 `agentscope[service,storage-sql]==2.0.10` 和 `aiosqlite`。本次独立读取该环境确认 `agentscope.__version__ == "2.0.10"`；导出的 OpenAPI info.version 同为 2.0.10，包含 77 个路径。

主审在当前工作轮已使用官方 create_app + ASGI lifespan，执行真实 Agent CRUD、Session 创建和 SQLite Storage 关闭重开；结果通过。验证产物：

- `/tmp/nexus-native-probe-g660e80j/agentscope.db`
- `/tmp/nexus-native-probe-g660e80j/openapi.json`

独立检查数据库确认已创建 13 张原生表；现存 1 条 Agent、1 条 Session、0 条消息。实测 AgentRecord.id 与 data.id 不同，Session 列表返回 SessionView 包裹。内省还确认 provider 应传 `openai_credential`，不是 `openai`。

证据应准确解释为：**真实 ASGI 应用生命周期重建与 SQLite 重新打开后数据仍存在**。尚未验证真实 Uvicorn 进程停止/启动、真实模型调用、多轮流式聊天、消息内容持久化、附件理解、知识检索、Nexus owner 过滤与 PER_SESSION 工作区隔离、员工删除归档后历史保留、三员工主动通信和循环终止。不能把无模型 CRUD 验证写成完整系统可用。

后续验收至少覆盖：

1. 独立 Agent Service 启停与真实进程重启恢复；Nexus 经 HTTP 正常连接。
2. Agent CRUD 使用顶层 ID；新对话使用最新 prompt；禁用/删除后的新运行从所有入口被阻止。
3. 原生消息历史、SSE replay、断线后恢复与真实模型失败状态正确呈现。
4. 不同匿名身份的 Session、附件、workspace 和长期 Memory 不串用。
5. Knowledge/Skill/MCP 关联实际生效；资源不存在或装载失败不显示为已具备能力。
6. 文本与图片附件实际被读取，其他类型明确限制。
7. A 通过真实模型工具调用联系 B，B 基于自己的职责和上下文回复，需要时联系 C；去重、预算、中断和重启后的待投递恢复有证据。
8. 删除策略符合最终确认的历史保留契约，不能以模拟回复、假接口或纯前端状态通过验收。

## 12. 后续源码审查更正与实施边界

物理工作区：固定版本的 `LocalWorkspaceManager.get_workspace` 将磁盘目录设为 `basedir/agent_id`，即使 `IsolationPolicy.PER_SESSION` 生成了不同 ID 也仍共用目录。必须通过公开 `WorkspaceManagerBase`、`LocalWorkspace` 按原生持久 workspace ID 分目录，补实际文件写入隔离与重启验证。阶段二的不同 ID 证据不作为文件隔离通过依据。[官方实现](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/workspace_manager/_local_workspace_manager.py#L128)

资源及工具：未配置 Knowledge manager 时目录接口返回 503 feature-disabled，不能伪装成成功空目录；当前 DeepSeek chat credential 不支持 embedding。MCP workspace 返回包含完整配置，Nexus 必须只投影名称、工具名、健康状态，不能转发凭据、环境变量和认证 URL。原生工具默认包含宿主文件、Shell、Task、Team、Schedule，关闭 scheduler worker 也不会移除工具。保留真实所选 MCP、原生专用 `Skill` 文本读取工具及后续 PeerSend；模型 schema 过滤与权限检查共同限制，不能只移除 toolkit 工具。脚本型技能不因读取 Markdown 就算执行成功。[工具装配](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_service/_toolkit.py#L139)

运行退出与归档：原生先发布 REPLY_END，再在 finally 保存消息与 Session state，最后解除运行锁；保存异常会被外层日志处理，idle 本身不证明成功落盘。慢速 MCP 默认十秒后可能继续作为后台任务，普通 interrupt 不取消后台任务。可通过公开 MessageBus、MessageBusKeys 取消 run/background，等待 session lock 释放且 bg_tasks registry 清空，再对照已观察尾部 reply_id、持久消息与 checkpoint 核对，失败或无法确认时禁止 DELETE。原生 DELETE 自身取消等待超时后仍可能继续删除，不能把它当作归档前屏障。[保存顺序](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_service/_chat.py#L1476)

游标：SSE payload 的 `id` 是 Event ID，`after` 接受的是 MessageBus transport entry ID；原生 SSE 没有暴露 `_entry_id` 或 SSE id。恢复应使用 messages 返回的 `event_cursor`，或公开服务扩展直接取得 bus `(entry_id, event)`，不能将 payload.id 当作 after。replay 最多一千条、成功 checkpoint 会裁剪、重启会清空，因此最终历史仍以原生 SQL 消息校准。[原生 stream](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/app/_router/_session.py#L893)

## 13. Knowledge 真实验收补充

官方公开 OllamaCredential 无云密钥字段，返回 OllamaEmbeddingModel，实际调用标准 Ollama /api/embed；pip客户端不包含模型服务。当前机器已有 Ollama 0.17.5，Codex 在隔离 11435 端口及 ignored data/embedding/models 下载 embeddinggemma:300m，并真实生成两段不同中文的768维向量。证据 `evidence/phase3/embedding-bootstrap-20261013T182311Z.json` 仅证明本地 embedding，不代表 Knowledge 完整链路已通过。

Knowledge manager启用后官方已有默认 TextParser/ApproxTokenChunker；CollectionPerKbManager 生命周期负责 vector store，不重复进入。上传TXT返回pending，文档真实完成状态为ready且chunk_count>0（并无indexed枚举）；状态接口 GET /knowledge_bases/{kb}/documents/status?ids={docid}，检索 POST /knowledge_bases/{kb}/search，body query/top_k。新Session需显式 static RAG 参数，默认agentic不会保证首轮检索。

官方会跳过无法装配的KB，并吞掉static检索异常继续模型调用。最小严格扩展应仅观察原生搜索：公开CollectionPerKbManager.get_knowledge返回公开KnowledgeBase包装，search仍委托原生实现；本轮ContextVar观察成功/失败KB，on_model_call核实所选KB全部成功搜索。零结果可继续，异常或漏装则明确中止；不另做一次检索、不存搜索副本/新RAG状态机。默认vector/blob目录必须从实际AGENTSCOPE_DATA_DIR派生，不能使隔离smoke落入项目默认正式目录。

## 14. MCP 支持范围收敛

2.0.10 原生 stateful MCP 将 AnyIO transport scopes 保存在连接 task，HTTP 后续请求/关闭可能在另一 task；即使 workspace 仅用一个 owner task，多连接 A→B 后先移除 A 仍存在非 LIFO 退出风险。原生连接失败清理还使用 shield 创建新 task；公开 workspace 包装不足以完整修复，不能吞掉关闭异常冒充通过。

v1.0.1 明确只支持官方无状态 Streamable HTTP 客户端：`is_stateful=False`、`mcp_config.type=http_mcp`，有效 HTTP(S) URL，path 不以 `/sse` 或 `/messages/` 结尾，并通过真实握手。stdio、SSE 与 stateful 条目安全投影为不可选及原因，保存/恢复/运行均拒绝；不修改核心、不复制 MCP 实现，也不让用户把 stdio 错配成无状态。该边界是当前版本限制，不意味着所有 MCP 传输均可用。

无状态 list_raw_tools 与 MCPTool.call 在当前调用 task 中以嵌套 async with 进入并退出 transport/ClientSession，不持有跨请求 scopes。后续必须实测发现/工具调用、协议失败/超时/取消后的再次调用，以及服务重启后恢复；远端操作取消不承诺回滚。受控 facade 去掉框架 state 参数后委托原 MCPTool，并保留原权限决策，不把本地允许目录等同于强制 ALLOW。[发现](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/mcp/_mcp_client.py#L439)、[执行](https://github.com/agentscope-ai/agentscope/blob/v2.0.10/src/agentscope/tool/_adapters.py#L330)
