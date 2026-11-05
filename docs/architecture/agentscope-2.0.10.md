# AgentScope 2.0.10 接口记录

Nexus 钉住的版本是 **2.0.10**。

| 来源 | 结果 |
| --- | --- |
| PyPI `agentscope` | 最新正式版 `2.0.10`，`Requires-Python >=3.11` |
| GitHub tag | [`v2.0.10`](https://github.com/agentscope-ai/agentscope/releases/tag/v2.0.10)，2026-10-12 |
| 安装 | `pip install agentscope==2.0.10` |

核对日期是 2026-10-13。文档站路径 `/en/versions/2.0.10/deploy/agent-service` 返回 404。`2.0.7` 与 `2.0.8` 的架构页仍把消息分页写成 `offset` 和 `limit`。下表以 tag `v2.0.10` 的源码为准。

身份头都是 `X-User-ID`。这是 Agent Service 的占位身份，不是登录。

## Agent

源码：`src/agentscope/app/_router/_agent.py`，前缀 `/agent`。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/agent/schema` | 旧版 schema |
| GET | `/agent/schema/v2` | `AgentData` schema |
| GET | `/agent/` | 列出 Agent |
| POST | `/agent/` | 创建。正文含 `name`、`system_prompt`、`chat_config` |
| PATCH | `/agent/{agent_id}` | 更新 |
| DELETE | `/agent/{agent_id}` | 删除 |

创建成功返回 `agent_id`。

## Session 与 chat SSE

源码：`_router/_session.py`、`_router/_chat.py`、`_router/_schema/_chat.py`。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/sessions/` | 列出会话 |
| POST | `/sessions/` | 创建。正文含 `agent_id`，`name` 与 `chat_model_config` 可空 |
| PATCH | `/sessions/{session_id}` | 更新 |
| DELETE | `/sessions/{session_id}` | 删除 |
| POST | `/sessions/{session_id}/interrupt?agent_id=` | 中断运行或人工确认中的回合 |
| GET | `/sessions/{session_id}/status?agent_id=` | 查询状态 |
| GET | `/sessions/{session_id}/messages?agent_id=&before=&limit=` | 读取已持久化消息。`limit` 为 1 到 200，默认 50。`offset` 在 2.0.10 中已废弃且被忽略 |
| GET | `/sessions/{session_id}/stream?agent_id=&after=` | SSE |
| POST | `/chat/` | 触发一轮推理，立即返回 |

`POST /sessions/` 对 `(user_id, agent_id, workspace_id)` 做 upsert。默认 workspace 隔离是 `IsolationPolicy.PER_AGENT`。Nexus 部署必须使用 `IsolationPolicy.PER_SESSION`，否则同一员工的多条对话会合成一条 Session。

`POST /chat/` 的正文：

```json
{
  "agent_id": "agent-id",
  "session_id": "session-id",
  "input": {
    "name": "user",
    "role": "user",
    "content": [{"type": "text", "text": "你好"}]
  }
}
```

`input` 还可以是消息列表、`UserConfirmResultEvent`、`ExternalExecutionResultEvent` 或 `null`。成功体是 `{"status":"started","session_id":"..."}`，里面没有回复文本。同一进程中该 Session 已有运行时返回 409。

SSE 为 `text/event-stream`。先回放，再推实时事件。数据帧是 `data: {JSON}\n\n`。约 30 秒一次心跳 `:\\n\\n`。文本增量事件：

```json
{"type": "TEXT_BLOCK_DELTA", "reply_id": "...", "block_id": "...", "delta": "增量文本"}
```

事件类型定义在 `src/agentscope/event/_event.py` 的 `EventType`。

## Skill

两套接口，不要混用。

用户技能库，`_router/_skill.py`：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/skill` | 列出已安装技能 |
| GET | `/skill/{skill_id}` | 读取一条，含 `SKILL.md` 正文 |
| DELETE | `/skill/{skill_id}` | 从库中删除，不收回已拷进 workspace 的文件 |

Workspace 内技能，`_router/_workspace.py`，前缀 `/workspace`。查询参数都要 `agent_id` 与 `session_id`：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/workspace/skill` | 列出该 Session workspace 里的技能 |
| POST | `/workspace/skill` | 按服务器路径添加。源码标为 deprecated |
| POST | `/workspace/skill/upload` | 上传技能目录 |
| POST | `/workspace/skill/from-library` | 从用户技能库装入 workspace |
| DELETE | `/workspace/skill/{skill_name}` | 从 workspace 移除 |

Nexus 自建技能保存在 `data/skills/<id>/<version>/`，通过 `/workspace/skill/upload` 装入会话；AgentScope 已安装技能只读 `GET /skill`，通过 `/workspace/skill/from-library` 绑定，不在 Nexus 复制正文。同名技能在 AgentScope 里会加 “ (2)” 后缀，Nexus 收回时按前缀一并处理。

## MCP

`_router/_mcp.py` 管理用户 MCP 库，`_router/_workspace.py` 管理 Session 工作区。两套接口同样不要混用：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/mcp` | 列出已安装 MCP。响应不含渲染后的密钥配置 |
| PATCH | `/mcp/{mcp_id}` | 改名、启停或重渲染凭据 |
| DELETE | `/mcp/{mcp_id}` | 从库中删除，不收回已装入 workspace 的副本 |
| GET | `/workspace/mcp` | 列出工作区 MCP，返回 `is_healthy`、`tools` 和 `error` |
| POST | `/workspace/mcp` | 按 `MCPClient` 配置直接装入工作区，首次使用该名称时顺带写入当前 User 的库 |
| POST | `/workspace/mcp/from-library` | 按库 id 装入工作区 |
| DELETE | `/workspace/mcp/{mcp_name}` | 从工作区移除 |

AgentScope 2.0.10 没有独立的库创建接口。Agent Service 通过内部 `PUT /nexus/mcp-registry/{name}` 调用官方 Storage，在 `NEXUS_AGENTSCOPE_USER_ID`（默认 `nexus`）下按稳定运行名写入记录。Nexus 创建、编辑和手动同步均先登记，即使没有员工绑定也能完成。配置和修订号由 Nexus 管理，AgentScope 库记录复用原 ID。管理页分别展示库登记和员工会话同步状态。未绑定资源删除时先删除平台库记录。

员工私聊从平台库按 ID 挂载。日程使用相同的库 ID，工作区仍使用安排者的 User 和 Session。连接测试在临时工作区直接挂载，不写库。绑定 MCP 的会话继续使用 AgentScope 的 `bypass` 权限模式，工具调用仍按员工、会话、任务或日程来源校验。

Task 与 Schedule 是部署管理的独立 HTTP MCP。安装器用同一内部入口登记，随后在校验工作区验证工具发现。员工仍须显式绑定这些资源。Library 的 Worker 连接由 OpenCode 部署配置提供。若另在 Nexus 新建 Library MCP 供员工使用，其登记与其他自建资源相同。

### 清理旧版日程副本

更新 Nexus 和 Agent Service 后，在 MCP 管理页同步涉及的自建资源，确认平台库登记成功。使用已配置 `AGENTSCOPE_SQL_URL` 的 Agent Service 环境运行下列命令预览候选，再添加 `--apply` 执行。

```bash
.venv-agentscope/bin/python cleanup_schedule_mcp_duplicates.py \
  nexus_nexus_library_97d5717d nexus_nexus_vision_b4054028 \
  nexus_test_system_40e02091 nexus_wetask_cf0ae0d0
```

脚本只删除命令中指定、已有平台登记且归属日程 User 的记录。会话资源按日程准备流程继续挂载同一平台库记录。

## Knowledge

源码：`_router/_knowledge_base.py`，前缀 `/knowledge_bases`。未向 `create_app` 传入 `knowledge_base_manager` 时，这些路由不可用。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/knowledge_bases/embedding_models` | 可用嵌入模型 |
| GET | `/knowledge_bases/chunkers` | 分块器 |
| GET | `/knowledge_bases/middleware/parameters_schema` | 中间件参数 schema |
| GET | `/knowledge_bases/supported_content_types` | 可入库的文件类型 |
| POST | `/knowledge_bases/` | 创建知识库 |
| GET | `/knowledge_bases/` | 列表 |
| PATCH | `/knowledge_bases/{knowledge_base_id}` | 更新 |
| DELETE | `/knowledge_bases/{knowledge_base_id}` | 删除 |
| GET | `/knowledge_bases/{knowledge_base_id}/documents` | 文档列表 |
| GET | `/knowledge_bases/{knowledge_base_id}/documents/status` | 索引状态 |
| POST | `/knowledge_bases/{knowledge_base_id}/documents` | 上传文档 |
| DELETE | `/knowledge_bases/{knowledge_base_id}/documents/{document_id}` | 删除文档 |
| POST | `/knowledge_bases/{knowledge_base_id}/search` | 自然语言检索 |
| GET | `/knowledge_bases/{knowledge_base_id}/documents/{document_id}` | 读取文档 |
| GET | `/knowledge_bases/{knowledge_base_id}/documents/{document_id}/chunks` | 读取分块 |
| POST | `/knowledge_bases/{knowledge_base_id}/documents/{document_id}/download_token` | 下载令牌 |

## Inbox、wakeup 与对等联系

`MessageBus` 提供 session 锁、回放日志、inbox 队列和 wakeup。`v2.0.10` 里投递一条提示的公开辅助函数是 `agentscope.app._bus_ops.deliver_to_inbox`：

- 在 `MessageBusKeys.inbox_lock(session_id)` 下把 payload 推入 `MessageBusKeys.inbox(session_id)`。
- payload 通常是 `HintBlock.model_dump(mode="json")`。`HintBlock` 的 `type` 固定为 `"hint"`，`hint` 是字符串或内容块列表，`source` 是发送方名字。
- 若 `inbox_consumer` 为空，调用 `enqueue_run_trigger(..., kind="wake")`。`wake` 不携带 input。Session 已在运行时，这次 wakeup 会被跳过，因为当前 run 自己会排空 inbox。

`InboxMiddleware` 在下一步推理前把 inbox 里的 `HintBlock` 变成 `HintBlockEvent`。`WakeupDispatcher` 负责真正启动 `ChatService.run`。

Agent Team 的 `TeamSay` 也走这条 inbox 加 wakeup 路径，但它要求 Leader 与 Worker。Nexus 不注册 `TeamCreate`、`AgentCreate`、`TeamSay`、`TeamDelete` 或 `AgentInvite`。对等联系由 `services/agent-service/agent_service.py` 里的 `contact_employee` 直接调用 `deliver_to_inbox`。
