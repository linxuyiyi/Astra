"""启动官方 AgentScope 2.0.10，并注册对等联系工具。

这个文件运行在 Agent Service 进程里。Nexus API 不导入它。
"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, field_validator


class RecordMessageBody(BaseModel):
    session_id: str
    message_id: str
    name: str = "user"
    text: str = ""
    user_id: str = "nexus"
    metadata: dict = Field(default_factory=dict)


class TaskHintBody(BaseModel):
    """模块级请求体。延迟注解下，FastAPI 解析不到 main() 里的局部类。"""

    session_id: str
    agent_id: str
    hint: str
    user_id: str = "nexus"
    event_id: str = ""

    @field_validator("event_id", mode="before")
    @classmethod
    def stringify_event_id(cls, value: object) -> str:
        if value is None:
            return ""
        return str(value).strip()


_event_lock = asyncio.Lock()
_seen_events: dict[str, None] = {}
_inflight_events: set[str] = set()
_SEEN_LIMIT = 1000


def reset_event_claims() -> None:
    _seen_events.clear()
    _inflight_events.clear()


def _event_key(event_id: str | int | None) -> str:
    return str(event_id or "").strip()


async def claim_event(event_id: str | int | None) -> str:
    """返回 owner、done 或 inflight。失败后释放，只有 inbox 接受后才记 done。"""

    value = _event_key(event_id)
    if not value:
        return "owner"
    async with _event_lock:
        if value in _seen_events:
            return "done"
        if value in _inflight_events:
            return "inflight"
        _inflight_events.add(value)
        return "owner"


async def finish_event(event_id: str | int | None, *, delivered: bool) -> None:
    value = _event_key(event_id)
    if not value:
        return
    async with _event_lock:
        _inflight_events.discard(value)
        if not delivered:
            return
        _seen_events[value] = None
        while len(_seen_events) > _SEEN_LIMIT:
            _seen_events.pop(next(iter(_seen_events)))


def register_record_message(app, storage, message_bus, keys, msg_cls) -> None:
    @app.post("/nexus/record-message")
    async def record_message(body: RecordMessageBody) -> dict[str, bool]:
        if not body.session_id.strip() or not body.message_id.strip():
            raise HTTPException(status_code=422, detail="缺少记录会话或消息")
        message = msg_cls(
            id=body.message_id,
            name=body.name or "user",
            role="user",
            content=[{"type": "text", "text": body.text}],
            metadata=body.metadata,
        )
        try:
            await write_record_message(
                storage,
                message_bus,
                message,
                user_id=body.user_id or "nexus",
                session_id=body.session_id,
                keys=keys,
            )
        except RecordRunning as exc:
            raise HTTPException(status_code=409, detail="会话正在运行，没有写入") from exc
        return {"ok": True}


def register_task_hint(app, deliver) -> None:
    """注册真实唤醒路由。Request 必须在模块全局里，延迟注解才能解析。"""

    @app.post("/nexus/task-hint")
    async def task_hint(body: TaskHintBody, request: Request) -> dict[str, bool]:
        if not body.session_id.strip() or not body.agent_id.strip() or not body.hint.strip():
            raise HTTPException(status_code=422, detail="缺少会话、员工或提示")
        event_id = request.headers.get("x-nexus-event-id") or body.event_id
        state = await claim_event(event_id)
        if state == "done":
            return {"ok": True}
        if state == "inflight":
            raise HTTPException(status_code=409, detail="同一事件正在投递")
        try:
            await deliver(body.user_id, body.agent_id, body.session_id, body.hint)
        except Exception:
            await finish_event(event_id, delivered=False)
            raise
        await finish_event(event_id, delivered=True)
        return {"ok": True}


NEXUS_TASK_MCP_PREFIX = "mcp__nexus-task__"
NEXUS_SCHEDULE_MCP_PREFIX = "mcp__nexus-schedule__"


def allow_nexus_task_mcp(mcp_tool_class, decision_class, behavior_class) -> None:
    """任务和日程 MCP 都是平台内置能力，在 dont_ask 下显式放行。"""

    original = mcp_tool_class.check_permissions

    allowed = (NEXUS_TASK_MCP_PREFIX, NEXUS_SCHEDULE_MCP_PREFIX)

    async def check_permissions(self, *args, **kwargs):
        name = str(getattr(self, "name", ""))
        if name.startswith(allowed):
            return decision_class(behavior=behavior_class.ALLOW, message="允许数字员工使用平台工具")
        return await original(self, *args, **kwargs)

    mcp_tool_class.check_permissions = check_permissions


# 任务规划和临时 Agent 会把岗位员工带偏。团队协作留下 TeamCreate、TeamSay、
# TeamDelete 和 AgentInvite。AgentCreate 会造出 source=team 的临时 Agent，继续卸掉。
REMOVED_AGENT_TOOLS = (
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
    "ToolStop",
    "AgentCreate",
    "ScheduleCreate",
    "ScheduleDelete",
    "ScheduleList",
    "ScheduleView",
)


class RecordRunning(RuntimeError):
    """记录 Session 上已有运行，纯写入直接拒绝。"""


def select_invite_session(sessions, mailbox_session_id: str | None):
    """只借用该员工自己的邮箱 Session。没有邮箱就新建 workspace。"""

    if not mailbox_session_id:
        return None
    for session in sessions:
        if getattr(session, "id", None) == mailbox_session_id:
            return session
    return None


class MailboxSessionView:
    """挡住 list_sessions 的第一条。那条通常是别人的私聊或其他团队。"""

    def __init__(self, inner, lookup) -> None:
        self._inner = inner
        self._lookup = lookup

    async def list_sessions(self, user_id: str, agent_id: str):
        sessions = await self._inner.list_sessions(user_id, agent_id)
        mailbox_id = await self._lookup(agent_id)
        chosen = select_invite_session(sessions, mailbox_id)
        return [chosen] if chosen is not None else []

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def retarget_agent_invites(toolkit, lookup) -> None:
    groups = getattr(toolkit, "tool_groups", None) or []
    for group in groups:
        for tool in getattr(group, "tools", []) or []:
            if getattr(tool, "name", "") != "AgentInvite":
                continue
            storage = getattr(tool, "_storage", None)
            if isinstance(storage, MailboxSessionView):
                continue
            tool._storage = MailboxSessionView(storage, lookup)


def without_team_and_task_tools(get_toolkit, mailbox_lookup=None):
    async def assembled(**kwargs):
        toolkit = await get_toolkit(**kwargs)
        await toolkit.remove_tool(list(REMOVED_AGENT_TOOLS))
        if mailbox_lookup is not None:
            retarget_agent_invites(toolkit, mailbox_lookup)
        groups = getattr(toolkit, "tool_groups", None)
        if groups is not None:
            toolkit.tool_groups = [
                group for group in groups if group.name == "basic" or group.tools
            ]
        return toolkit

    return assembled


_memory_state: dict[str, str] = {}


def memory_status(key: str | None = None) -> dict[str, str | bool | dict]:
    """按 agent 或 workspace 记录。没有进程级的一份状态。"""

    if key is None:
        return {
            "items": {
                name: {"ok": False, "status": "degraded", "error": error}
                for name, error in _memory_state.items()
                if error
            }
        }
    error = _memory_state.get(key, "")
    if error:
        return {"ok": False, "status": "degraded", "error": error}
    return {"ok": True, "status": "ok", "error": ""}


class GuardedMemory:
    """记忆 hook 抛错时继续后面的对话，并把这一份标成 degraded。"""

    def __init__(self, inner, key: str) -> None:
        self._inner = inner
        self._key = key

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def _mark(self, exc: Exception) -> None:
        _memory_state[self._key] = str(exc)

    async def on_system_prompt(self, agent, current_prompt: str) -> str:
        method = getattr(self._inner, "on_system_prompt", None)
        if method is None:
            return current_prompt
        try:
            return await method(agent, current_prompt)
        except Exception as exc:
            self._mark(exc)
            return current_prompt

    async def _guard(self, name: str, agent, input_kwargs, next_handler):
        method = getattr(self._inner, name, None)
        if method is None:
            async for item in next_handler(**input_kwargs):
                yield item
            return
        started = False
        finished = False

        async def tracked(**kwargs):
            nonlocal started, finished
            started = True
            async for item in next_handler(**kwargs):
                yield item
            finished = True

        try:
            async for item in method(agent, input_kwargs, tracked):
                yield item
        except Exception as exc:
            if started and not finished:
                raise
            self._mark(exc)
            if not started:
                async for item in next_handler(**input_kwargs):
                    yield item

    async def on_reply(self, agent, input_kwargs, next_handler):
        async for item in self._guard("on_reply", agent, input_kwargs, next_handler):
            yield item

    async def on_reasoning(self, agent, input_kwargs, next_handler):
        async for item in self._guard("on_reasoning", agent, input_kwargs, next_handler):
            yield item


def card_context_size(model_name: str, classes) -> int | None:
    """网关模型名对上任意厂商的模型卡时，用那张卡的 context_size。"""

    seen: set[int] = set()
    for cls in classes:
        identity = id(cls)
        if identity in seen:
            continue
        seen.add(identity)
        list_models = getattr(cls, "list_models", None)
        if list_models is None:
            continue
        try:
            cards = list_models()
        except Exception:
            continue
        for card in cards:
            if getattr(card, "name", None) == model_name:
                return int(card.context_size)
    return None


def install_card_context_size() -> None:
    """OpenAI 兼容网关不会命中本厂商卡片。注入时改用同名卡片的 context_size。"""

    from agentscope.app._service import _chat as chat_service
    from agentscope.app._service import _model as model_service
    from agentscope.model import (
        AnthropicChatModel,
        DashScopeChatModel,
        DeepSeekChatModel,
        GeminiChatModel,
        MiniMaxChatModel,
        MoonshotChatModel,
        OllamaChatModel,
        OpenAIChatModel,
        OpenAIResponseModel,
        VolcengineChatModel,
        XAIChatModel,
    )

    classes = [
        OpenAIChatModel,
        DeepSeekChatModel,
        AnthropicChatModel,
        DashScopeChatModel,
        GeminiChatModel,
        MiniMaxChatModel,
        MoonshotChatModel,
        OllamaChatModel,
        OpenAIResponseModel,
        VolcengineChatModel,
        XAIChatModel,
    ]
    original = model_service.get_model

    async def get_model(user_id, config, access):
        model = await original(user_id, config, access)
        size = card_context_size(getattr(config, "model", ""), [type(model), *classes])
        if size is not None:
            model.context_size = size
        return model

    model_service.get_model = get_model
    chat_service.get_model = get_model


def attach_memory(workspace, factory=None, key: str = "") -> list:
    """官方接法：workdir 和 backend 都来自这次运行的 workspace。失败时主聊天继续。

    个人记忆只留在这个 workspace，不写入图书馆。
    """

    state_key = key or str(getattr(workspace, "workdir", "") or "memory")
    if factory is None:
        try:
            from agentscope.middleware import AgenticMemoryMiddleware
        except Exception as exc:
            _memory_state[state_key] = str(exc)
            return []
        factory = AgenticMemoryMiddleware
    try:
        middleware = factory(workdir=workspace.workdir, backend=workspace.get_backend())
    except Exception as exc:
        _memory_state[state_key] = str(exc)
        return []
    _memory_state.pop(state_key, None)
    return [GuardedMemory(middleware, state_key)]


async def memory_middlewares(user_id: str, agent_id: str, session_id: str, workspace) -> list:
    workdir = str(getattr(workspace, "workdir", "") or "")
    return attach_memory(workspace, key=agent_id or workdir)


async def write_record_message(storage, message_bus, message, *, user_id: str, session_id: str, keys) -> None:
    """checkpoint 锁下只 upsert。不调用 ChatService.run，也不占用运行锁。"""

    if await message_bus.is_locked(keys.session_lock(session_id)):
        raise RecordRunning(session_id)
    async with message_bus.acquire_lock(keys.session_event_checkpoint_lock(session_id)):
        await storage.upsert_message(user_id, session_id, message)
    await message_bus.publish(
        keys.session_events(session_id),
        {"type": "record", "message_id": str(getattr(message, "id", "") or "")},
    )


def main() -> None:
    import agentscope.app._service._chat as chat_service
    import agentscope.app._service._realtime as realtime_service
    import httpx
    import uvicorn
    from agentscope.app import create_app
    from agentscope.app._bus_ops import deliver_to_inbox
    from agentscope.app.hub import ClawSkillHub, GitHubMCPHub
    from agentscope.app._service._toolkit import get_toolkit
    from agentscope.app.message_bus import MessageBusKeys, RedisMessageBus
    from agentscope.app.rag.blob_store import LocalBlobStore
    from agentscope.app.rag.knowledge_base_manager import CollectionPerKbManager
    from agentscope_sql import create_storage, guard_lifespan
    from schedule_access import ScheduleResourceAccessPolicy
    from agentscope.app.workspace_manager import (
        IsolationPolicy,
        LocalWorkspaceManager,
    )
    from agentscope.message import HintBlock, Msg
    from agentscope.permission import PermissionBehavior, PermissionDecision
    from agentscope.rag import QdrantStore
    from agentscope.tool import FunctionTool, MCPTool

    install_card_context_size()
    allow_nexus_task_mcp(MCPTool, PermissionDecision, PermissionBehavior)

    async def mailbox_lookup(agent_id: str) -> str | None:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{nexus_api}/api/v1/internal/mailbox",
                params={"agent_id": agent_id},
            )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        session_id = response.json().get("session_id")
        return str(session_id) if session_id else None

    peer_toolkit = without_team_and_task_tools(get_toolkit, mailbox_lookup)
    chat_service.get_toolkit = peer_toolkit
    realtime_service.get_toolkit = peer_toolkit

    host = os.environ.get("AGENTSCOPE_REDIS_HOST", "localhost")
    port = int(os.environ.get("AGENTSCOPE_REDIS_PORT", "6379"))
    redis_db = int(os.environ.get("AGENTSCOPE_REDIS_DB", "1"))
    password = os.environ.get("AGENTSCOPE_REDIS_PASSWORD") or None
    nexus_api = os.environ.get("NEXUS_API_BASE", "http://127.0.0.1:18120").rstrip("/")
    workspace_dir = os.environ.get("AGENTSCOPE_WORKSPACE_DIR", "services/agent-service/data/workspaces")
    os.makedirs(workspace_dir, exist_ok=True)

    storage = create_storage()
    message_bus = RedisMessageBus(host=host, port=port, db=redis_db, password=password)
    workspace_manager = LocalWorkspaceManager(
        basedir=workspace_dir,
        isolation=IsolationPolicy.PER_SESSION,
    )
    qdrant_path = os.environ.get("AGENTSCOPE_QDRANT_PATH", "services/agent-service/data/qdrant")
    blob_dir = os.environ.get("AGENTSCOPE_BLOB_DIR", "services/agent-service/data/blobs")
    os.makedirs(qdrant_path, exist_ok=True)
    knowledge_base_manager = CollectionPerKbManager(
        storage=storage,
        vector_store=QdrantStore(path=qdrant_path),
    )

    async def extra_agent_tools(user_id: str, agent_id: str, session_id: str):
        async def contact_employee(employee_name: str, message: str) -> str:
            """把一条提示交给另一位数字员工，由对方自己推理后回复。

            employee_name: 同事的姓名。私聊和自己的邮箱会话都按员工目录查找对方的邮箱。
            message: 需要对方看到的原话。不要代替对方编写结论。
            """
            return await deliver_peer_hint(
                httpx,
                deliver_to_inbox,
                HintBlock,
                message_bus,
                nexus_api,
                user_id,
                agent_id,
                session_id,
                employee_name,
                message,
            )

        async def contact_human(conversation_key: str, message: str) -> str:
            """把一条消息交给人类。同一会话按原来的 delivery_id 投递，不在这里再次推理。

            conversation_key: 这条人和员工会话的键。
            message: 要送给人类的原文。
            """
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    f"{nexus_api}/api/v1/im/contacts",
                    json={
                        "agent_id": agent_id,
                        "conversation_key": conversation_key,
                        "session_id": session_id,
                        "body": message,
                    },
                )
            if response.status_code >= 400:
                return response.text
            return str(response.json().get("delivery_id") or "")

        return [
            FunctionTool(
                contact_employee,
                name="contact_employee",
                permission=PermissionDecision(
                    behavior=PermissionBehavior.ALLOW,
                    message="允许数字员工联系同事",
                ),
            ),
            FunctionTool(
                contact_human,
                name="contact_human",
                permission=PermissionDecision(
                    behavior=PermissionBehavior.ALLOW,
                    message="允许数字员工联系人类",
                ),
            ),
        ]

    app = create_app(
        storage=storage,
        resource_access_policy=ScheduleResourceAccessPolicy(
            os.environ.get("NEXUS_AGENTSCOPE_USER_ID", "nexus")
        ),
        message_bus=message_bus,
        workspace_manager=workspace_manager,
        knowledge_base_manager=knowledge_base_manager,
        blob_store=LocalBlobStore(blob_dir),
        extra_agent_tools=extra_agent_tools,
        extra_agent_middlewares=memory_middlewares,
        mcp_hubs=[GitHubMCPHub(api_token=os.environ.get("AGENTSCOPE_GITHUB_TOKEN") or None)],
        skill_hubs=[ClawSkillHub(api_token=os.environ.get("AGENTSCOPE_CLAWHUB_TOKEN") or None)],
    )
    guard_lifespan(app)
    listen_host = os.environ.get("AGENTSCOPE_HOST", "0.0.0.0")
    listen_port = int(os.environ.get("AGENTSCOPE_PORT", "18000"))

    async def deliver(user_id: str, agent_id: str, session_id: str, hint: str) -> None:
        await deliver_task_hint(
            deliver_to_inbox,
            HintBlock,
            message_bus,
            user_id,
            agent_id,
            session_id,
            hint,
        )

    register_task_hint(app, deliver)
    register_record_message(app, storage, message_bus, MessageBusKeys, Msg)

    @app.get("/nexus/memory-status")
    async def nexus_memory_status(key: str = "") -> dict[str, str | bool | dict]:
        return memory_status(key or None)
    uvicorn.run(app, host=listen_host, port=listen_port)


def peer_error_text(response, employee_name: str) -> str:
    detail = ""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
        detail = payload["detail"]
    if detail:
        return detail
    if response.status_code == 404:
        return f"没有找到可联系的员工：{employee_name}"
    return "没有把消息交给对方。"


async def deliver_peer_hint(
    httpx_module,
    deliver_to_inbox,
    hint_block,
    message_bus,
    nexus_api: str,
    user_id: str,
    agent_id: str,
    session_id: str,
    employee_name: str,
    message: str,
) -> str:
    async with httpx_module.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"{nexus_api}/api/v1/peers/resolve",
            params={"session_id": session_id, "name": employee_name, "message": message},
        )
    if response.status_code >= 400:
        return json.dumps(
            {"ok": False, "peer": employee_name, "error": peer_error_text(response, employee_name)},
            ensure_ascii=False,
        )
    peer = response.json()
    hint = hint_block(hint=message, source=peer.get("sender_name") or agent_id)
    await deliver_to_inbox(
        message_bus,
        user_id=user_id,
        session_id=peer["session_id"],
        agent_id=peer["agent_id"],
        payload=hint.model_dump(mode="json"),
    )
    return json.dumps(
        {"ok": True, "peer": employee_name, "status": "delivered"},
        ensure_ascii=False,
    )


async def deliver_task_hint(
    deliver_to_inbox,
    hint_block,
    message_bus,
    user_id: str,
    agent_id: str,
    session_id: str,
    hint: str,
) -> None:
    block = hint_block(hint=hint, source="nexus-task")
    await deliver_to_inbox(
        message_bus,
        user_id=user_id,
        session_id=session_id,
        agent_id=agent_id,
        payload=block.model_dump(mode="json"),
    )


if __name__ == "__main__":
    main()
