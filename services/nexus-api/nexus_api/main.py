"""Nexus HTTP API. Agent 推理只通过 AgentScope HTTP 发生。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .agentscope import PINNED_VERSION, AgentScopeClient, AgentScopeError
from .capability import inspect_employee
from .db import connect, employee_from_row, init_db, message_from_row, new_id, utcnow
from .groups import HumanEvents, chat_inputs, merge_timeline, messages_since
from .im import (
    ImError,
    accept_inbound,
    acknowledge,
    bind_bot,
    claim_outbox,
    contact_human,
    open_external,
    redeliver,
)
from .model_route import ModelRouteError, chat_config_for_route, gateway_api_key, require_enabled_route
from .task_skill import TASK_SKILL_ID, attach_task_capability, sync_task_capability, with_builtin_skill

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS = 6
MAX_TEXT_CHARS = 64_000
STREAM_CURSOR = re.compile(r"^\d+-\d+$")
TEXT_SUFFIXES = {
    ".txt", ".md", ".log", ".py", ".json", ".yaml", ".yml", ".js", ".ts", ".vue",
    ".css", ".html", ".xml", ".csv", ".sh", ".toml", ".ini", ".cfg", ".sql",
}
IMAGE_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
SENDER_CONTACT_LIMIT = 2
SCOPE_CONTACT_LIMIT = 6
UNATTENDED_PERMISSION = "dont_ask"


class EmployeeBody(BaseModel):
    name: str = Field(min_length=1, max_length=24)
    domain: str = Field(min_length=1, max_length=42)
    description: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    avatar: str = "🤖"
    enabled: bool = True
    knowledge: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    model_route_id: str = ""


class ConversationBody(BaseModel):
    name: str = Field(default="新对话", max_length=40)


class MessageBody(BaseModel):
    text: str = ""
    attachment_ids: list[str] = Field(default_factory=list)


class ChannelBody(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=1)
    member_ids: list[str] = Field(min_length=1)


class ImBotBody(BaseModel):
    employee_id: str
    provider: str
    bot_id: str


class ImEventBody(BaseModel):
    provider: str
    event_id: str
    bot_id: str
    sender_uid: str = ""
    text: str = ""
    external_key: str = ""
    conversation_key: str = ""


class ImGroupBody(BaseModel):
    employee_id: str
    external_key: str
    session_id: str


class ImContactBody(BaseModel):
    employee_id: str = ""
    agent_id: str = ""
    conversation_key: str
    session_id: str
    body: str


class ImAckBody(BaseModel):
    delivery_id: str


class ImRedeliverBody(BaseModel):
    conversation_key: str


def package_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def data_dir() -> Path:
    """数据目录不跟着启动时的工作目录走。

    未设置时用 API 包旁边的 data。相对路径按仓库根目录解析，
    这样从 services/nexus-api 启动也不会把相对路径再拼一次服务目录。
    """
    raw = os.environ.get("NEXUS_DATA_DIR", "").strip()
    if not raw:
        return package_dir() / "data"
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return (repo_root() / path).resolve()


def db_path() -> Path:
    return data_dir() / "nexus.sqlite3"


def load_chat_model_config() -> dict[str, Any] | None:
    """部署环境里的 Agent Service 模型引用。页面不编辑这项。"""
    raw = os.environ.get("NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG", "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG 不是 JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG 必须是对象")
    required = {"type", "credential_id", "model", "parameters"}
    missing = sorted(required - set(parsed))
    if missing:
        raise RuntimeError("NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG 缺少 " + ", ".join(missing))
    return parsed


def system_prompt(body: EmployeeBody | dict[str, Any]) -> str:
    if isinstance(body, EmployeeBody):
        name, domain, description, instructions = (
            body.name,
            body.domain,
            body.description,
            body.instructions,
        )
    else:
        name = body["name"]
        domain = body["domain"]
        description = body["description"]
        instructions = body["instructions"]
    text = (
        f"你是{name}，责任领域是{domain}。\n"
        f"职责说明：\n{description}\n"
        f"工作指令：\n{instructions}\n"
        "需要其他员工协助时调用 contact_employee；对方的回复由对方自己产生。"
    )
    skills = body.skills if isinstance(body, EmployeeBody) else list(body.get("skills") or [])
    employee_id = None if isinstance(body, EmployeeBody) else body.get("id")
    if TASK_SKILL_ID in skills and employee_id:
        text += (
            f"\n你的员工编号是 {employee_id}。"
            "需要工程执行时使用任务工具；收到任务状态提示后先查询该任务，再决定下一步。"
        )
    return text


def resolved_media_type(name: str, declared: str) -> str:
    media = (declared or "").split(";", 1)[0].strip().lower()
    if media.startswith("image/") or media.startswith("text/") or media == "application/pdf":
        return media
    return IMAGE_BY_SUFFIX.get(Path(name).suffix.lower(), media or "application/octet-stream")


def readable_text(name: str, media: str, data: bytes) -> str | None:
    suffix = Path(name).suffix.lower()
    if not (media.startswith("text/") or suffix in TEXT_SUFFIXES):
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def model_input_blocks(text: str, files: list[tuple[str, str, bytes]]) -> list[dict[str, Any]]:
    """把用户输入变成 AgentScope 的 content。读不到的文件不会被说成已经分析过。"""
    blocks: list[dict[str, Any]] = []
    if text.strip():
        blocks.append({"type": "text", "text": text.strip()})
    for name, declared, data in files:
        media = resolved_media_type(name, declared)
        if media.startswith("image/"):
            blocks.append({"type": "text", "text": f"附件 {name}"})
            blocks.append(
                {
                    "type": "data",
                    "name": name,
                    "source": {
                        "type": "base64",
                        "media_type": media,
                        "data": base64.b64encode(data).decode("ascii"),
                    },
                }
            )
            continue
        body = readable_text(name, media, data)
        if body is not None:
            omitted = ""
            if len(body) > MAX_TEXT_CHARS:
                body = body[:MAX_TEXT_CHARS]
                omitted = "\n其余内容没有放进这次输入。"
            blocks.append({"type": "text", "text": f"附件 {name}：\n{body}{omitted}"})
            continue
        if media == "application/pdf":
            blocks.append(
                {
                    "type": "text",
                    "text": f"附件 {name} 是 PDF。数据块已附上；如果当前模型不接收 PDF，不要描述文件内容。",
                }
            )
            blocks.append(
                {
                    "type": "data",
                    "name": name,
                    "source": {
                        "type": "base64",
                        "media_type": media,
                        "data": base64.b64encode(data).decode("ascii"),
                    },
                }
            )
            continue
        blocks.append(
            {
                "type": "text",
                "text": f"附件 {name}（{media}）没有放进模型输入。不要描述它的内容。",
            }
        )
    if not blocks:
        blocks.append({"type": "text", "text": "（空消息）"})
    return blocks


def block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(part for part in parts if part)


def content_strings(content: Any) -> list[str]:
    """取出正文和 hint 里的原文。不经过 json.dumps，因此保留换行。"""
    found: list[str] = []
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return found
    for block in content:
        if isinstance(block, str):
            found.append(block)
            continue
        if not isinstance(block, dict):
            continue
        for field in ("text", "hint"):
            value = block.get(field)
            if isinstance(value, str):
                found.append(value)
            elif isinstance(value, list):
                found.extend(content_strings(value))
    return found


def message_contains(message: dict[str, Any], key: str) -> bool:
    return any(key in text for text in content_strings(message.get("content")))


def assistant_replies_after(messages: list[Any], needle: str) -> list[str]:
    """取邮箱里紧跟在某条提示之后的助手正文。不生成新的答复。

    Agent Service 把收到的 hint 和答复写在同一条 assistant 消息里。
    下一条 assistant 是另一次唤醒，不能算进这一次。
    """
    key = needle.strip()
    if not key:
        return []
    found = False
    replies: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if not found:
            if not message_contains(message, key):
                continue
            found = True
            if message.get("role") == "assistant":
                text = block_text(message.get("content"))
                if text:
                    replies.append(text)
            continue
        if message.get("role") != "assistant" or replies:
            break
        text = block_text(message.get("content"))
        if text:
            replies.append(text)
    return replies


def canonical_timestamp(value: str) -> str:
    """把无时区的本地时间和带偏移的时间收成同一种 UTC 字符串。"""
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(UTC).isoformat()


def timestamp_order(value: Any) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(canonical_timestamp(value)).timestamp()
    except ValueError:
        return 0.0


def assistant_turns(messages: list[Any]) -> list[list[str]]:
    turns: list[list[str]] = []
    current: list[str] | None = None
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "user":
            if current is not None:
                turns.append(current)
            current = []
        elif role == "assistant" and current is not None:
            text = block_text(message.get("content"))
            if text:
                current.append(text)
    if current is not None:
        turns.append(current)
    return turns


def merge_transcript(
    local: list[dict[str, Any]],
    turns: list[list[str]],
    employee_id: str,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    index = 0
    for item in local:
        merged.append(item)
        if item.get("accepted") and index < len(turns):
            for text in turns[index]:
                merged.append(
                    {
                        "id": f"agent_{item['id']}_{len(merged)}",
                        "sender": employee_id,
                        "text": text,
                        "attachments": [],
                        "accepted": True,
                        "created_at": item["created_at"],
                    }
                )
            index += 1
    return merged


def tool_cards(content: Any) -> list[dict[str, str]]:
    if not isinstance(content, list):
        return []
    cards: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind not in {"tool_call", "tool_result"}:
            continue
        card_id = str(block.get("id") or "")
        card = cards.get(card_id)
        if card is None:
            card = {"id": card_id, "name": "", "input": "", "output": "", "state": ""}
            cards[card_id] = card
            order.append(card_id)
        if block.get("name"):
            card["name"] = str(block.get("name"))
        if kind == "tool_call":
            card["input"] = str(block.get("input") or "")
        else:
            output = block.get("output")
            card["output"] = block_text(output) if isinstance(output, list) else str(output or "")
        if block.get("state"):
            card["state"] = str(block.get("state"))
    return [cards[card_id] for card_id in order]


def session_transcript(remote: list[Any], employee_id: str) -> list[dict[str, Any]]:
    """私聊历史只读 AgentScope。发言人取 Msg.name，不回退成匿名 user。"""

    found: list[dict[str, Any]] = []
    for message in remote:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        display = metadata.get("display_text")
        text = display if isinstance(display, str) else block_text(message.get("content"))
        tools = tool_cards(message.get("content"))
        attachments = metadata.get("attachments") if isinstance(metadata.get("attachments"), list) else []
        if role == "assistant" and not text and not tools:
            continue
        raw_id = message.get("id")
        name = message.get("name") if isinstance(message.get("name"), str) else ""
        created = message.get("created_at")
        found.append(
            {
                "id": raw_id if isinstance(raw_id, str) and raw_id else f"remote_{len(found)}",
                "sender": "user" if role == "user" else employee_id,
                "speaker": name or str(metadata.get("verified_uid") or ""),
                "text": text,
                "tools": tools,
                "attachments": [item for item in attachments if isinstance(item, dict)],
                "accepted": True,
                "created_at": canonical_timestamp(created) if isinstance(created, str) else "",
            }
        )
    return found


def assistant_messages(remote: list[Any], employee_id: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for message in remote:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        text = block_text(message.get("content"))
        if not text:
            continue
        raw_id = message.get("id")
        suffix = raw_id if isinstance(raw_id, str) and raw_id else str(len(found))
        created = message.get("created_at")
        found.append(
            {
                "id": f"agent_{employee_id}_{suffix}",
                "sender": employee_id,
                "text": text,
                "attachments": [],
                "accepted": True,
                "created_at": canonical_timestamp(created) if isinstance(created, str) else "",
            }
        )
    return found


def interleave_messages(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    for group in groups:
        combined.extend(group)
    combined.sort(
        key=lambda item: (
            timestamp_order(item.get("created_at")),
            0 if item.get("sender") == "user" else 1,
            str(item.get("id") or ""),
        )
    )
    return combined


def reserve_peer_delivery(
    connection,
    scope: str,
    sender_agent_id: str,
    target_agent_id: str,
    message: str,
) -> None:
    """同一句话只投一次。每位发送方、每场讨论还有很小的次数上限。"""
    key = message.strip()
    duplicate = connection.execute(
        """
        SELECT 1 FROM peer_deliveries
        WHERE scope = ? AND sender_agent_id = ? AND target_agent_id = ? AND message_key = ?
        """,
        (scope, sender_agent_id, target_agent_id, key),
    ).fetchone()
    if duplicate:
        raise HTTPException(status_code=409, detail="已经把同样的话交给对方，不要再联系。")
    sender_count = connection.execute(
        "SELECT COUNT(*) AS count FROM peer_deliveries WHERE scope = ? AND sender_agent_id = ?",
        (scope, sender_agent_id),
    ).fetchone()["count"]
    if sender_count >= SENDER_CONTACT_LIMIT:
        raise HTTPException(status_code=409, detail="你在这次讨论里已经联系过同事，不要继续互相调用。")
    scope_count = connection.execute(
        "SELECT COUNT(*) AS count FROM peer_deliveries WHERE scope = ?",
        (scope,),
    ).fetchone()["count"]
    if scope_count >= SCOPE_CONTACT_LIMIT:
        raise HTTPException(status_code=409, detail="这次讨论的同事联系已经达到上限，停止继续联系。")
    connection.execute(
        """
        INSERT INTO peer_deliveries (
            id, scope, sender_agent_id, target_agent_id, message_key, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (new_id("peer"), scope, sender_agent_id, target_agent_id, key, utcnow()),
    )
    connection.commit()


def inbound_private_scope(connection, agent_id: str | None) -> str | None:
    if not agent_id:
        return None
    row = connection.execute(
        """
        SELECT scope FROM peer_deliveries
        WHERE target_agent_id = ? AND scope LIKE 'private:%'
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (agent_id,),
    ).fetchone()
    if row is None:
        return None
    return str(row["scope"])


async def hand_off(
    connection,
    client: AgentScopeClient,
    scope: str,
    sender: dict,
    target: dict,
    message: str,
    session_id: str,
):
    reserve_peer_delivery(
        connection,
        scope,
        sender["agentscope_agent_id"],
        target["agentscope_agent_id"],
        message,
    )
    await client.set_permission_mode(
        target["agentscope_agent_id"],
        session_id,
        UNATTENDED_PERMISSION,
    )
    return {
        "agent_id": target["agentscope_agent_id"],
        "session_id": session_id,
        "sender_name": sender["name"],
    }


async def peer_replies_for_conversation(
    connection,
    client: AgentScopeClient,
    conversation_id: str,
) -> list[dict[str, Any]]:
    """把同事在自己邮箱里写下的正文带回用户正在看的私聊。不由发送方代写。"""
    rows = connection.execute(
        "SELECT * FROM peer_deliveries WHERE scope = ? ORDER BY created_at, id",
        (f"private:{conversation_id}",),
    ).fetchall()
    visible: list[dict[str, Any]] = []
    transcripts: dict[str, list[Any]] = {}
    for row in rows:
        employee = connection.execute(
            "SELECT * FROM employees WHERE agentscope_agent_id = ?",
            (row["target_agent_id"],),
        ).fetchone()
        if employee is None or not employee["mailbox_session_id"]:
            continue
        agent_id = row["target_agent_id"]
        if agent_id not in transcripts:
            try:
                transcripts[agent_id] = await client.list_session_messages(
                    agent_id,
                    employee["mailbox_session_id"],
                )
            except AgentScopeError:
                transcripts[agent_id] = []
        for index, text in enumerate(assistant_replies_after(transcripts[agent_id], row["message_key"])):
            visible.append(
                {
                    "id": f"peer_{row['id']}_{index}",
                    "sender": employee["id"],
                    "text": text,
                    "attachments": [],
                    "accepted": True,
                    "created_at": row["created_at"],
                }
            )
    return visible


def session_owned_by(connection, employee_id: str, session_id: str, mailbox_session_id: str | None) -> bool:
    if mailbox_session_id and session_id == mailbox_session_id:
        return True
    conversation = connection.execute(
        """
        SELECT 1 FROM conversations
        WHERE employee_id = ? AND agentscope_session_id = ?
        """,
        (employee_id, session_id),
    ).fetchone()
    if conversation is not None:
        return True
    member = connection.execute(
        """
        SELECT 1 FROM channel_members
        WHERE employee_id = ? AND agentscope_session_id = ?
        """,
        (employee_id, session_id),
    ).fetchone()
    return member is not None


def lines(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        text = value.strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


_conversation_locks: dict[str, asyncio.Lock] = {}
_conversation_locks_guard = asyncio.Lock()


async def conversation_lock(conversation_id: str) -> asyncio.Lock:
    async with _conversation_locks_guard:
        lock = _conversation_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            _conversation_locks[conversation_id] = lock
        return lock


def attachment_visible(connection, attachment_id: str, owner_uid: str, requester: str) -> bool:
    if owner_uid == requester:
        return True
    rows = connection.execute(
        """
        SELECT channel_id FROM messages
        WHERE channel_id IS NOT NULL AND attachments_json LIKE ?
        """,
        (f'%"{attachment_id}"%',),
    ).fetchall()
    for item in rows:
        member = connection.execute(
            "SELECT 1 FROM channel_humans WHERE channel_id = ? AND user_uid = ?",
            (item["channel_id"], requester),
        ).fetchone()
        if member is not None:
            return True
    return False


def claim_private_session(connection, conversation_id: str, session_id: str, workspace_id: str) -> str:
    """只绑定还没有 Session 的私聊。并发时使用先写入的那一个。"""

    connection.execute(
        """
        UPDATE conversations
        SET agentscope_session_id = ?, workspace_id = ?, updated_at = ?
        WHERE id = ? AND agentscope_session_id IS NULL
        """,
        (session_id, workspace_id, utcnow(), conversation_id),
    )
    connection.commit()
    row = connection.execute(
        "SELECT agentscope_session_id FROM conversations WHERE id = ?",
        (conversation_id,),
    ).fetchone()
    stored = row["agentscope_session_id"] if row is not None else None
    return str(stored or session_id)


def require_owner(
    x_verified_uid: str | None = Header(default=None, alias="X-Verified-UID"),
    x_owner_id: str | None = Header(default=None, alias="X-Owner-Id"),
    nexus_uid: str | None = Cookie(default=None, alias="nexus_uid"),
    uid: str | None = Query(default=None),
) -> str:
    """身份来自可信头。`?uid=` 不参与授权。Cookie 只给页面里的图片和下载使用。"""

    del uid
    value = (x_verified_uid or x_owner_id or nexus_uid or "").strip()
    if not value:
        raise HTTPException(status_code=401, detail="缺少 X-Verified-UID")
    return value


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        root = data_dir()
        root.mkdir(parents=True, exist_ok=True)
        (root / "files").mkdir(parents=True, exist_ok=True)
        init_db(db_path())
        app.state.agentscope = AgentScopeClient(
            os.environ.get("NEXUS_AGENTSCOPE_BASE_URL") or None,
            os.environ.get("NEXUS_AGENTSCOPE_USER_ID", "nexus"),
            chat_model_config=load_chat_model_config(),
        )
        app.state.human_events = HumanEvents()
        yield
        await app.state.agentscope.aclose()

    app = FastAPI(title="Nexus", version="1.0.1", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def database():
        connection = connect(db_path())
        try:
            yield connection
        finally:
            connection.close()

    def client_of(request: Request) -> AgentScopeClient:
        return request.app.state.agentscope

    async def checked_route(route_id: str) -> None:
        wanted = route_id.strip()
        if not wanted:
            return
        await require_enabled_route(wanted)
        gateway_api_key()

    async def model_for(client: AgentScopeClient, employee: dict[str, Any]) -> dict[str, Any] | None:
        route_id = str(employee.get("model_route_id") or "").strip()
        if not route_id:
            raise AgentScopeError("请先选择模型路由", 422)
        if not client.configured:
            return None
        try:
            return await chat_config_for_route(client, route_id)
        except ModelRouteError as exc:
            raise AgentScopeError(exc.detail, exc.status_code) from exc

    def bound_knowledge(employee: dict[str, Any] | None) -> list[str]:
        raw = employee.get("knowledge") if employee else None
        if not isinstance(raw, list):
            return []
        return [str(item) for item in raw if item]

    def session_ids(connection, employee: dict[str, Any]) -> list[str]:
        found: list[str] = []
        mailbox = employee.get("mailbox_session_id")
        if mailbox:
            found.append(str(mailbox))
        rows = connection.execute(
            "SELECT agentscope_session_id FROM conversations WHERE employee_id = ?",
            (employee["id"],),
        ).fetchall()
        rows += connection.execute(
            "SELECT agentscope_session_id FROM channel_members WHERE employee_id = ?",
            (employee["id"],),
        ).fetchall()
        for row in rows:
            session_id = row["agentscope_session_id"]
            if session_id and session_id not in found:
                found.append(str(session_id))
        return found

    def load_employee(connection, employee_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="员工不存在")
        return employee_from_row(row)

    def find_duplicate(connection, name: str, domain: str, exclude: str | None) -> str | None:
        rows = connection.execute("SELECT id, name, domain FROM employees").fetchall()
        for row in rows:
            if exclude and row["id"] == exclude:
                continue
            if row["name"].lower() == name.lower():
                return "员工名称不可重复"
            if row["domain"].lower() == domain.lower():
                return "该职责已有员工"
        return None

    async def accepted_from_catalog(
        client: AgentScopeClient,
        requested: list[str],
        previous: list[str],
        *,
        load,
        unknown_detail: str,
        unavailable_detail: str,
    ) -> tuple[list[str], dict[str, Any]]:
        requested = lines(requested)
        catalog: list[dict[str, str]] | None = None
        if client.configured:
            try:
                catalog = await load()
            except AgentScopeError:
                catalog = None
        if catalog is not None:
            allowed = {item["id"] for item in catalog}
            unknown = [item for item in requested if item not in allowed]
            if unknown:
                raise HTTPException(status_code=422, detail=unknown_detail)
            return requested, {"available": True, "items": catalog}
        extras = [item for item in requested if item not in set(previous)]
        if extras:
            raise HTTPException(status_code=422, detail=unavailable_detail)
        return requested, {"available": False, "items": []}

    async def accepted_skills(
        client: AgentScopeClient, requested: list[str], previous: list[str]
    ) -> list[str]:
        requested = lines(requested)
        catalog: list[dict[str, str]] | None = None
        if client.configured:
            try:
                catalog = await client.list_skills()
            except AgentScopeError:
                catalog = None
        if catalog is not None:
            allowed = {item["id"] for item in catalog} | {TASK_SKILL_ID}
            unknown = [item for item in requested if item not in allowed]
            if unknown:
                raise HTTPException(status_code=422, detail="技能不在 AgentScope 技能库中")
            return requested
        extras = [item for item in requested if item not in set(previous) and item != TASK_SKILL_ID]
        if extras:
            raise HTTPException(status_code=422, detail="当前无法读取技能库，不能新增技能")
        return requested

    async def chosen_resources(
        client: AgentScopeClient,
        body: EmployeeBody,
        previous: dict[str, list[str]],
    ) -> tuple[list[str], list[str], list[str]]:
        skills = await accepted_skills(client, body.skills, previous.get("skills", []))
        knowledge, _knowledge = await accepted_from_catalog(
            client,
            body.knowledge,
            previous.get("knowledge", []),
            load=client.list_knowledge_bases,
            unknown_detail="知识库不在 AgentScope 知识库中",
            unavailable_detail="当前无法读取知识库，不能新增知识库",
        )
        tools, _tools = await accepted_from_catalog(
            client,
            body.tools,
            previous.get("tools", []),
            load=client.list_launcher_tools,
            unknown_detail="工具不在 AgentScope 可关联工具中",
            unavailable_detail="当前无法确认 AgentScope 工具，不能新增工具",
        )
        return skills, knowledge, tools

    def task_sessions(connection, employee: dict[str, Any]) -> list[dict[str, str]]:
        found: list[dict[str, str]] = []
        seen: set[str] = set()

        def add(session_id: str | None, *, conversation_id: str = "", channel_id: str = "") -> None:
            value = (session_id or "").strip()
            if not value or value in seen:
                return
            seen.add(value)
            found.append(
                {"session_id": value, "conversation_id": conversation_id, "channel_id": channel_id}
            )

        add(employee.get("mailbox_session_id"))
        conversations = connection.execute(
            "SELECT id, agentscope_session_id FROM conversations WHERE employee_id = ?",
            (employee["id"],),
        ).fetchall()
        for row in conversations:
            add(row["agentscope_session_id"], conversation_id=row["id"])
        members = connection.execute(
            "SELECT channel_id, agentscope_session_id FROM channel_members WHERE employee_id = ?",
            (employee["id"],),
        ).fetchall()
        for row in members:
            add(row["agentscope_session_id"], channel_id=row["channel_id"])
        return found

    async def sync_agent(
        client: AgentScopeClient, employee: dict[str, Any], connection
    ) -> dict[str, Any]:
        prompt = system_prompt(employee)
        agent_id = employee["agentscope_agent_id"]
        mailbox = employee["mailbox_session_id"]
        try:
            invite = employee["domain"] or employee["name"]
            if agent_id:
                await client.update_agent(agent_id, employee["name"], prompt, invite)
            elif client.configured:
                agent_id = await client.create_agent(employee["name"], prompt, invite)
            else:
                raise AgentScopeError("AgentScope 未连接，员工目录已保存，Agent 尚未创建")
            if not mailbox and agent_id:
                config = await model_for(client, employee)
                mailbox = await client.create_session(
                    agent_id,
                    f"{employee['name']}的邮箱",
                    chat_model_config=config,
                    knowledge_base_ids=bound_knowledge(employee),
                )
            if agent_id and mailbox:
                await client.set_permission_mode(agent_id, mailbox, UNATTENDED_PERMISSION)
                current = {**employee, "agentscope_agent_id": agent_id, "mailbox_session_id": mailbox}
                await sync_task_capability(client, current, task_sessions(connection, current))
            return {"ok": True, "agent_id": agent_id, "mailbox_session_id": mailbox, "detail": ""}
        except AgentScopeError as exc:
            return {"ok": False, "agent_id": agent_id, "mailbox_session_id": mailbox, "detail": exc.detail}

    async def store_capability(connection, client: AgentScopeClient, employee: dict[str, Any]) -> None:
        record = await inspect_employee(client, employee, utcnow())
        connection.execute(
            "UPDATE employees SET capability_json = ? WHERE id = ?",
            (json.dumps(record, ensure_ascii=False), employee["id"]),
        )
        connection.commit()

    def save_agent_link(connection, employee_id: str, sync: dict[str, Any]) -> None:
        connection.execute(
            """
            UPDATE employees
            SET agentscope_agent_id = ?, mailbox_session_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (sync["agent_id"], sync["mailbox_session_id"], utcnow(), employee_id),
        )
        connection.commit()

    def insert_employee(
        connection,
        body: EmployeeBody,
        skills: list[str],
        knowledge: list[str],
        tools: list[str],
    ) -> dict[str, Any]:
        employee_id = new_id("emp")
        now = utcnow()
        connection.execute(
            """
            INSERT INTO employees (
                id, name, domain, description, instructions, avatar, enabled,
                knowledge_json, skills_json, tools_json, agentscope_agent_id,
                mailbox_session_id, model_route_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
            """,
            (
                employee_id,
                body.name.strip(),
                body.domain.strip(),
                body.description.strip(),
                body.instructions.strip(),
                body.avatar,
                int(body.enabled),
                json.dumps(knowledge, ensure_ascii=False),
                json.dumps(skills, ensure_ascii=False),
                json.dumps(tools, ensure_ascii=False),
                body.model_route_id.strip(),
                now,
                now,
            ),
        )
        connection.commit()
        return load_employee(connection, employee_id)

    def list_messages(connection, *, conversation_id: str | None = None, channel_id: str | None = None):
        if conversation_id:
            rows = connection.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at",
                (conversation_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM messages WHERE channel_id = ? ORDER BY created_at",
                (channel_id,),
            ).fetchall()
        return [message_from_row(row) for row in rows]

    def load_attachments(
        connection,
        attachment_ids: list[str],
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if len(attachment_ids) > MAX_ATTACHMENTS:
            raise HTTPException(status_code=400, detail="每条消息最多添加 6 个附件")
        found: list[dict[str, Any]] = []
        for attachment_id in attachment_ids:
            row = connection.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
            if row is None or (owner_id is not None and row["owner_uid"] != owner_id):
                raise HTTPException(status_code=404, detail="附件不存在")
            found.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "type": row["media_type"],
                    "size": row["size"],
                    "path": row["path"],
                }
            )
        return found

    def attachment_bytes(connection, message: dict[str, Any]) -> list[tuple[str, str, bytes]]:
        files: list[tuple[str, str, bytes]] = []
        for item in message.get("attachments") or []:
            row = connection.execute("SELECT * FROM attachments WHERE id = ?", (item["id"],)).fetchone()
            if row is None:
                continue
            files.append((row["name"], row["media_type"], Path(row["path"]).read_bytes()))
        return files

    def store_user_message(
        connection,
        text: str,
        attachments: list[dict[str, Any]],
        *,
        speaker_uid: str = "",
        **scope: str | None,
    ):
        if not text.strip() and not attachments:
            raise HTTPException(status_code=400, detail="请输入消息或添加附件")
        message_id = new_id("msg")
        created_at = utcnow()
        visible = [
            {"id": item["id"], "name": item["name"], "type": item["type"], "size": item["size"]}
            for item in attachments
        ]
        connection.execute(
            """
            INSERT INTO messages (
                id, conversation_id, channel_id, sender, text, attachments_json, accepted,
                speaker_uid, created_at
            ) VALUES (?, ?, ?, 'user', ?, ?, 0, ?, ?)
            """,
            (
                message_id,
                scope.get("conversation_id"),
                scope.get("channel_id"),
                text.strip(),
                json.dumps(visible, ensure_ascii=False),
                speaker_uid,
                created_at,
            ),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return message_from_row(row)

    async def ensure_session(
        connection,
        client: AgentScopeClient,
        agent_id: str,
        current: str | None,
        name: str,
        permission_mode: str | None = None,
        chat_model_config: dict[str, Any] | None = None,
        knowledge_base_ids: list[str] | None = None,
    ) -> str:
        session_id = current or await client.create_session(
            agent_id,
            name,
            chat_model_config=chat_model_config,
            knowledge_base_ids=knowledge_base_ids,
        )
        if permission_mode:
            await client.set_permission_mode(agent_id, session_id, permission_mode)
        return session_id

    async def claim_member_session(connection, client, employee, channel_id: str, name: str) -> str:
        """群成员会话只绑定还是空的那一行。并发时使用先写入的那一个。"""

        lock_id = f"member:{channel_id}:{employee['id']}"
        async with await conversation_lock(lock_id):
            row = connection.execute(
                """
                SELECT agentscope_session_id FROM channel_members
                WHERE channel_id = ? AND employee_id = ?
                """,
                (channel_id, employee["id"]),
            ).fetchone()
            current = row["agentscope_session_id"] if row is not None else None
            if current:
                await client.set_permission_mode(
                    employee["agentscope_agent_id"],
                    current,
                    UNATTENDED_PERMISSION,
                )
                return current
            session_id = await client.create_session(
                employee["agentscope_agent_id"],
                name,
                chat_model_config=await model_for(client, employee),
                knowledge_base_ids=bound_knowledge(employee),
            )
            await client.set_permission_mode(
                employee["agentscope_agent_id"],
                session_id,
                UNATTENDED_PERMISSION,
            )
            connection.execute(
                """
                UPDATE channel_members SET agentscope_session_id = ?
                WHERE channel_id = ? AND employee_id = ? AND agentscope_session_id IS NULL
                """,
                (session_id, channel_id, employee["id"]),
            )
            connection.commit()
            stored = connection.execute(
                """
                SELECT agentscope_session_id FROM channel_members
                WHERE channel_id = ? AND employee_id = ?
                """,
                (channel_id, employee["id"]),
            ).fetchone()
            return str(stored["agentscope_session_id"] or session_id) if stored is not None else session_id

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "product": "nexus", "version": "1.0.1"}

    @app.get("/api/v1/me")
    def me(owner_id: str = Depends(require_owner)) -> dict[str, str]:
        return {"uid": owner_id}

    @app.get("/api/v1/runtime")
    async def runtime(request: Request) -> dict[str, Any]:
        client = client_of(request)
        return {
            "agentscope_version": PINNED_VERSION,
            "configured": client.configured,
            "reachable": await client.probe(),
            "agentscope_user_id": os.environ.get("NEXUS_AGENTSCOPE_USER_ID", "nexus").strip() or "nexus",
        }

    async def publish_catalog(client: AgentScopeClient, load) -> dict[str, Any]:
        if not client.configured:
            return {"available": False, "items": []}
        try:
            items = await load()
        except AgentScopeError:
            return {"available": False, "items": []}
        return {"available": True, "items": items}

    @app.get("/api/v1/skills")
    async def skills(request: Request) -> dict[str, Any]:
        client = client_of(request)
        payload = await publish_catalog(client, client.list_skills)
        payload["items"] = with_builtin_skill(payload["items"])
        return payload

    @app.get("/api/v1/internal/wake-target")
    def wake_target(
        employee_id: str,
        session_id: str = "",
        connection=Depends(database),
    ) -> dict[str, str]:
        employee = load_employee(connection, employee_id)
        if not employee["enabled"]:
            raise HTTPException(status_code=409, detail="员工已禁用，稍后再投递")
        agent_id = employee["agentscope_agent_id"]
        if not agent_id:
            raise HTTPException(status_code=404, detail="员工还没有 Agent")
        requested = session_id.strip()
        if requested:
            if not session_owned_by(connection, employee_id, requested, employee["mailbox_session_id"]):
                raise HTTPException(status_code=404, detail="会话不属于该员工")
            target = requested
        else:
            target = employee["mailbox_session_id"]
            if not target:
                raise HTTPException(status_code=404, detail="员工还没有邮箱")
        return {"agent_id": agent_id, "session_id": target}

    @app.get("/api/v1/internal/mailbox")
    def mailbox(agent_id: str, connection=Depends(database)) -> dict[str, str]:
        row = connection.execute(
            "SELECT mailbox_session_id FROM employees WHERE agentscope_agent_id = ?",
            (agent_id,),
        ).fetchone()
        if row is None or not row["mailbox_session_id"]:
            raise HTTPException(status_code=404, detail="员工还没有邮箱")
        return {"session_id": row["mailbox_session_id"]}

    @app.get("/api/v1/knowledge")
    async def knowledge(request: Request) -> dict[str, Any]:
        client = client_of(request)
        return await publish_catalog(client, client.list_knowledge_bases)

    @app.get("/api/v1/tools")
    async def tools(request: Request) -> dict[str, Any]:
        client = client_of(request)
        return await publish_catalog(client, client.list_launcher_tools)

    @app.get("/api/v1/employees")
    def employees(q: str = "", connection=Depends(database)) -> dict[str, Any]:
        rows = connection.execute("SELECT * FROM employees ORDER BY created_at").fetchall()
        items = [employee_from_row(row) for row in rows]
        needle = q.strip().lower()
        if needle:
            items = [
                item
                for item in items
                if needle in f"{item['name']} {item['domain']} {item['description']}".lower()
            ]
        return {"items": items}

    @app.post("/api/v1/employees", status_code=201)
    async def create_employee(
        body: EmployeeBody,
        request: Request,
        connection=Depends(database),
    ) -> dict[str, Any]:
        conflict = find_duplicate(connection, body.name.strip(), body.domain.strip(), None)
        if conflict:
            raise HTTPException(status_code=409, detail=conflict)
        try:
            await checked_route(body.model_route_id)
        except ModelRouteError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        client = client_of(request)
        skills, knowledge, tools = await chosen_resources(client, body, {})
        employee = insert_employee(connection, body, skills, knowledge, tools)
        sync = await sync_agent(client, employee, connection)
        save_agent_link(connection, employee["id"], sync)
        employee = load_employee(connection, employee["id"])
        await store_capability(connection, client, employee)
        employee = load_employee(connection, employee["id"])
        return {"employee": employee, "agentscope": {"ok": sync["ok"], "detail": sync["detail"]}}

    @app.patch("/api/v1/employees/{employee_id}")
    async def update_employee(
        employee_id: str,
        body: EmployeeBody,
        request: Request,
        connection=Depends(database),
    ) -> dict[str, Any]:
        current = load_employee(connection, employee_id)
        conflict = find_duplicate(connection, body.name.strip(), body.domain.strip(), employee_id)
        if conflict:
            raise HTTPException(status_code=409, detail=conflict)
        try:
            await checked_route(body.model_route_id)
        except ModelRouteError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        client = client_of(request)
        skills, knowledge, tools = await chosen_resources(client, body, current)
        connection.execute(
            """
            UPDATE employees
            SET name = ?, domain = ?, description = ?, instructions = ?, avatar = ?, enabled = ?,
                knowledge_json = ?, skills_json = ?, tools_json = ?, model_route_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                body.name.strip(),
                body.domain.strip(),
                body.description.strip(),
                body.instructions.strip(),
                body.avatar,
                int(body.enabled),
                json.dumps(knowledge, ensure_ascii=False),
                json.dumps(skills, ensure_ascii=False),
                json.dumps(tools, ensure_ascii=False),
                body.model_route_id.strip(),
                utcnow(),
                employee_id,
            ),
        )
        connection.commit()
        employee = load_employee(connection, employee_id)
        previous_route = str(current.get("model_route_id") or "")
        sync = await sync_agent(client, employee, connection)
        save_agent_link(connection, employee_id, sync)
        employee = load_employee(connection, employee_id)
        await store_capability(connection, client, employee)
        employee = load_employee(connection, employee_id)
        previous_knowledge = bound_knowledge(current)
        route_id = str(employee.get("model_route_id") or "")
        if route_id and route_id != previous_route and employee["agentscope_agent_id"] and client.configured:
            try:
                config = await chat_config_for_route(client, route_id)
                for session_id in session_ids(connection, employee):
                    await client.replace_session_model(employee["agentscope_agent_id"], session_id, config)
            except (ModelRouteError, AgentScopeError) as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        knowledge_ids = bound_knowledge(employee)
        if knowledge_ids != previous_knowledge and employee["agentscope_agent_id"] and client.configured:
            try:
                for session_id in session_ids(connection, employee):
                    await client.replace_session_knowledge(
                        employee["agentscope_agent_id"],
                        session_id,
                        knowledge_ids,
                    )
            except AgentScopeError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return {
            "employee": employee,
            "agentscope": {"ok": sync["ok"], "detail": sync["detail"]},
        }

    @app.delete("/api/v1/employees/{employee_id}")
    async def delete_employee(
        employee_id: str,
        request: Request,
        connection=Depends(database),
    ) -> dict[str, bool]:
        employee = load_employee(connection, employee_id)
        client = client_of(request)
        if employee["agentscope_agent_id"] and client.configured:
            try:
                await client.delete_agent(employee["agentscope_agent_id"])
            except AgentScopeError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        connection.execute("DELETE FROM channel_members WHERE employee_id = ?", (employee_id,))
        connection.execute("DELETE FROM employees WHERE id = ?", (employee_id,))
        connection.commit()
        return {"deleted": True}

    @app.post("/api/v1/attachments", status_code=201)
    async def upload_attachment(
        file: UploadFile = File(...),
        owner_id: str = Depends(require_owner),
        connection=Depends(database),
    ) -> dict[str, Any]:
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="不能添加空文件")
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(status_code=400, detail="单个附件不能超过 20 MB")
        attachment_id = new_id("att")
        target = data_dir() / "files" / attachment_id
        target.write_bytes(content)
        media_type = file.content_type or "application/octet-stream"
        filename = file.filename or "未命名文件"
        connection.execute(
            """
            INSERT INTO attachments (id, owner_uid, name, media_type, size, path, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (attachment_id, owner_id, filename, media_type, len(content), str(target), utcnow()),
        )
        connection.commit()
        return {"id": attachment_id, "name": filename, "type": media_type, "size": len(content)}

    @app.get("/api/v1/attachments/{attachment_id}")
    def download_attachment(
        attachment_id: str,
        owner_id: str = Depends(require_owner),
        connection=Depends(database),
    ) -> FileResponse:
        row = connection.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
        visible = row is not None and Path(row["path"]).is_file() and attachment_visible(
            connection,
            attachment_id,
            row["owner_uid"],
            owner_id,
        )
        if not visible or row is None:
            raise HTTPException(status_code=404, detail="附件内容不存在")
        disposition = "inline" if str(row["media_type"]).startswith("image/") else "attachment"
        return FileResponse(
            row["path"],
            media_type=row["media_type"],
            filename=row["name"],
            content_disposition_type=disposition,
        )

    @app.get("/api/v1/employees/{employee_id}/conversations")
    def list_conversations(
        employee_id: str,
        owner_id: str = Depends(require_owner),
        connection=Depends(database),
    ):
        load_employee(connection, employee_id)
        rows = connection.execute(
            """
            SELECT * FROM conversations
            WHERE employee_id = ? AND owner_id = ?
            ORDER BY created_at
            """,
            (employee_id, owner_id),
        ).fetchall()
        return {
            "items": [
                {
                    "id": row["id"],
                    "employee_id": row["employee_id"],
                    "name": row["name"],
                    "agentscope_session_id": row["agentscope_session_id"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]
        }

    @app.post("/api/v1/employees/{employee_id}/conversations", status_code=201)
    def create_conversation(
        employee_id: str,
        body: ConversationBody,
        response: Response,
        owner_id: str = Depends(require_owner),
        connection=Depends(database),
    ):
        load_employee(connection, employee_id)
        existing = connection.execute(
            """
            SELECT * FROM conversations
            WHERE employee_id = ? AND owner_id = ? AND kind = 'private'
            """,
            (employee_id, owner_id),
        ).fetchone()
        if existing is not None:
            response.status_code = 200
            return {
                "id": existing["id"],
                "name": existing["name"],
                "agentscope_session_id": existing["agentscope_session_id"],
            }
        conversation_id = new_id("conv")
        now = utcnow()
        name = body.name.strip() or "私聊"
        connection.execute(
            """
            INSERT OR IGNORE INTO conversations (
                id, employee_id, owner_id, kind, name, agentscope_session_id, created_at, updated_at
            ) VALUES (?, ?, ?, 'private', ?, NULL, ?, ?)
            """,
            (conversation_id, employee_id, owner_id, name, now, now),
        )
        connection.commit()
        stored = connection.execute(
            """
            SELECT * FROM conversations
            WHERE employee_id = ? AND owner_id = ? AND kind = 'private'
            """,
            (employee_id, owner_id),
        ).fetchone()
        if stored["id"] != conversation_id:
            response.status_code = 200
        return {
            "id": stored["id"],
            "name": stored["name"],
            "agentscope_session_id": stored["agentscope_session_id"],
        }

    def owned_conversation(connection, conversation_id: str, owner_id: str):
        row = connection.execute(
            "SELECT * FROM conversations WHERE id = ? AND owner_id = ?",
            (conversation_id, owner_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="对话不存在")
        return row

    @app.get("/api/v1/conversations/{conversation_id}")
    async def get_conversation(
        conversation_id: str,
        request: Request,
        owner_id: str = Depends(require_owner),
        connection=Depends(database),
    ):
        row = owned_conversation(connection, conversation_id, owner_id)
        employee = load_employee(connection, row["employee_id"])
        history: list[dict[str, Any]] = []
        history_error = ""
        event_cursor = ""
        if row["agentscope_session_id"] and employee["agentscope_agent_id"]:
            try:
                remote, event_cursor = await client_of(request).read_session(
                    employee["agentscope_agent_id"],
                    row["agentscope_session_id"],
                )
                history = session_transcript(remote, employee["id"])
            except AgentScopeError as exc:
                history_error = exc.detail
        return {
            "id": row["id"],
            "employee_id": row["employee_id"],
            "name": row["name"],
            "agentscope_session_id": row["agentscope_session_id"],
            "event_cursor": event_cursor,
            "messages": interleave_messages(
                history,
                await peer_replies_for_conversation(connection, client_of(request), conversation_id),
            ),
            "history_error": history_error,
        }

    @app.get("/api/v1/conversations/{conversation_id}/messages")
    async def native_conversation_messages(
        conversation_id: str,
        request: Request,
        owner_id: str = Depends(require_owner),
        connection=Depends(database),
    ):
        """原生消息：完整 content 块、事件游标和运行态，供嵌入式聊天使用。

        扁平响应（GET /conversations/{id}）是旧渲染的契约，保持不动。
        """

        row = owned_conversation(connection, conversation_id, owner_id)
        employee = load_employee(connection, row["employee_id"])
        if not row["agentscope_session_id"] or not employee["agentscope_agent_id"]:
            return {"messages": [], "event_cursor": "", "is_running": False}
        try:
            messages, event_cursor, is_running = await client_of(request).read_session_detail(
                employee["agentscope_agent_id"],
                row["agentscope_session_id"],
            )
        except AgentScopeError as exc:
            raise HTTPException(status_code=502, detail=exc.detail) from exc
        return {
            "messages": messages,
            "event_cursor": event_cursor,
            "is_running": is_running,
        }

    @app.post("/api/v1/conversations/{conversation_id}/messages")
    async def send_private(
        conversation_id: str,
        body: MessageBody,
        request: Request,
        owner_id: str = Depends(require_owner),
        connection=Depends(database),
    ):
        row = owned_conversation(connection, conversation_id, owner_id)
        employee = load_employee(connection, row["employee_id"])
        if not employee["enabled"]:
            raise HTTPException(status_code=409, detail="该员工已禁用")
        attachments = load_attachments(connection, body.attachment_ids, owner_id)
        visible = [
            {"id": item["id"], "name": item["name"], "type": item["type"], "size": item["size"]}
            for item in attachments
        ]
        if not body.text.strip() and not attachments:
            raise HTTPException(status_code=400, detail="请输入消息或添加附件")
        message = {
            "id": new_id("msg"),
            "sender": "user",
            "speaker": owner_id,
            "text": body.text.strip(),
            "attachments": visible,
            "accepted": False,
            "created_at": utcnow(),
        }
        session_name = row["name"]
        if message["text"] and row["name"] in {"新对话", "私聊"}:
            title = message["text"] if len(message["text"]) <= 20 else message["text"][:20] + "…"
            connection.execute(
                "UPDATE conversations SET name = ?, updated_at = ? WHERE id = ?",
                (title, utcnow(), conversation_id),
            )
            connection.commit()
        client = client_of(request)
        outcome = {
            "accepted": False,
            "detail": "AgentScope 未连接，没有生成回复",
            "session_id": row["agentscope_session_id"],
        }
        if not employee["agentscope_agent_id"]:
            outcome["detail"] = "AgentScope 尚未创建该员工，没有生成回复"
        else:
            try:
                if row["agentscope_session_id"]:
                    session_id = row["agentscope_session_id"]
                    await client.set_permission_mode(
                        employee["agentscope_agent_id"],
                        session_id,
                        UNATTENDED_PERMISSION,
                    )
                else:
                    async with await conversation_lock(conversation_id):
                        current = owned_conversation(connection, conversation_id, owner_id)
                        if current["agentscope_session_id"]:
                            session_id = current["agentscope_session_id"]
                        else:
                            workspace_id = current["workspace_id"] or f"ws-{conversation_id}"
                            session_id = await client.create_session(
                                employee["agentscope_agent_id"],
                                session_name,
                                workspace_id,
                                chat_model_config=await model_for(client, employee),
                                knowledge_base_ids=bound_knowledge(employee),
                            )
                            await client.set_permission_mode(
                                employee["agentscope_agent_id"],
                                session_id,
                                UNATTENDED_PERMISSION,
                            )
                            session_id = claim_private_session(
                                connection,
                                conversation_id,
                                session_id,
                                workspace_id,
                            )
                await attach_task_capability(
                    client,
                    employee,
                    session_id,
                    conversation_id=conversation_id,
                )
                files: list[tuple[str, str, bytes]] = []
                for item in attachments:
                    files.append((item["name"], item["type"], Path(item["path"]).read_bytes()))
                await client.trigger_chat(
                    employee["agentscope_agent_id"],
                    session_id,
                    model_input_blocks(body.text, files),
                    owner_id,
                    verified_uid=owner_id,
                    attachments=visible,
                    display_text=message["text"],
                )
                message["accepted"] = True
                outcome = {"accepted": True, "detail": "", "session_id": session_id}
            except AgentScopeError as exc:
                outcome["detail"] = exc.detail
        return {"message": message, "agentscope": outcome}

    @app.post("/api/v1/conversations/{conversation_id}/rebind")
    async def rebind_private(
        conversation_id: str,
        request: Request,
        owner_id: str = Depends(require_owner),
        connection=Depends(database),
    ):
        row = owned_conversation(connection, conversation_id, owner_id)
        employee = load_employee(connection, row["employee_id"])
        workspace_id = row["workspace_id"]
        if not employee["agentscope_agent_id"] or not workspace_id:
            raise HTTPException(status_code=409, detail="这条私聊还没有可复用的 workspace")
        client = client_of(request)
        try:
            session_id = await client.create_session(
                employee["agentscope_agent_id"],
                row["name"],
                workspace_id,
                chat_model_config=await model_for(client, employee),
                knowledge_base_ids=bound_knowledge(employee),
            )
        except AgentScopeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        connection.execute(
            """
            UPDATE conversations
            SET agentscope_session_id = ?, workspace_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (session_id, workspace_id, utcnow(), conversation_id),
        )
        connection.commit()
        return {"session_id": session_id, "workspace_id": workspace_id}

    @app.get("/api/v1/conversations/{conversation_id}/stream")
    async def stream_private(
        conversation_id: str,
        request: Request,
        owner_id: str = Depends(require_owner),
        connection=Depends(database),
        after: str | None = None,
        raw: bool = False,
    ):
        row = owned_conversation(connection, conversation_id, owner_id)
        employee = load_employee(connection, row["employee_id"])
        if not row["agentscope_session_id"] or not employee["agentscope_agent_id"]:
            raise HTTPException(status_code=409, detail="这条对话还没有 AgentScope Session")
        return await proxy_stream(
            client_of(request),
            employee["agentscope_agent_id"],
            row["agentscope_session_id"],
            stream_cursor(after),
            raw=raw,
        )

    def channel_payload(connection, channel_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="讨论不存在")
        members = connection.execute(
            "SELECT employee_id, agentscope_session_id FROM channel_members WHERE channel_id = ?",
            (channel_id,),
        ).fetchall()
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "members": [
                {"employee_id": item["employee_id"], "agentscope_session_id": item["agentscope_session_id"]}
                for item in members
            ],
            "created_at": row["created_at"],
        }

    def ensure_members_exist(connection, member_ids: list[str]) -> list[str]:
        unique: list[str] = []
        for member_id in member_ids:
            if member_id in unique:
                continue
            load_employee(connection, member_id)
            unique.append(member_id)
        if not unique:
            raise HTTPException(status_code=422, detail="至少选择一名员工")
        return unique

    @app.get("/api/v1/channels")
    def list_channels(connection=Depends(database)) -> dict[str, Any]:
        rows = connection.execute("SELECT id FROM channels ORDER BY created_at DESC").fetchall()
        return {"items": [channel_payload(connection, row["id"]) for row in rows]}

    @app.post("/api/v1/channels", status_code=201)
    def create_channel(body: ChannelBody, connection=Depends(database)) -> dict[str, Any]:
        member_ids = ensure_members_exist(connection, body.member_ids)
        channel_id = new_id("ch")
        now = utcnow()
        connection.execute(
            "INSERT INTO channels (id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (channel_id, body.name.strip(), body.description.strip(), now, now),
        )
        for member_id in member_ids:
            connection.execute(
                """
                INSERT INTO channel_members (channel_id, employee_id, agentscope_session_id)
                VALUES (?, ?, NULL)
                """,
                (channel_id, member_id),
            )
        connection.commit()
        return channel_payload(connection, channel_id)

    @app.patch("/api/v1/channels/{channel_id}")
    def update_channel(channel_id: str, body: ChannelBody, connection=Depends(database)) -> dict[str, Any]:
        channel_payload(connection, channel_id)
        before = connection.execute("SELECT created_at FROM channels WHERE id = ?", (channel_id,)).fetchone()
        member_ids = ensure_members_exist(connection, body.member_ids)
        connection.execute(
            "UPDATE channels SET name = ?, description = ?, updated_at = ? WHERE id = ?",
            (body.name.strip(), body.description.strip(), utcnow(), channel_id),
        )
        existing = {
            row["employee_id"]: (row["agentscope_session_id"], row["last_delivered_message_id"])
            for row in connection.execute(
                """
                SELECT employee_id, agentscope_session_id, last_delivered_message_id
                FROM channel_members WHERE channel_id = ?
                """,
                (channel_id,),
            ).fetchall()
        }
        connection.execute("DELETE FROM channel_members WHERE channel_id = ?", (channel_id,))
        for member_id in member_ids:
            session_id, cursor = existing.get(member_id, (None, ""))
            connection.execute(
                """
                INSERT INTO channel_members (
                    channel_id, employee_id, agentscope_session_id, last_delivered_message_id
                ) VALUES (?, ?, ?, ?)
                """,
                (channel_id, member_id, session_id, cursor or ""),
            )
        connection.execute(
            "UPDATE channels SET created_at = ? WHERE id = ?",
            (before["created_at"], channel_id),
        )
        connection.commit()
        return channel_payload(connection, channel_id)

    @app.get("/api/v1/channels/{channel_id}/messages")
    async def channel_messages(
        channel_id: str,
        request: Request,
        connection=Depends(database),
        x_owner_id: str | None = Header(default=None),
        x_verified_uid: str | None = Header(default=None),
    ):
        channel = channel_payload(connection, channel_id)
        viewer = (x_verified_uid or x_owner_id or "").strip()
        if viewer:
            connection.execute(
                """
                INSERT INTO channel_humans (channel_id, user_uid, display_name)
                VALUES (?, ?, ?)
                ON CONFLICT(channel_id, user_uid) DO UPDATE SET display_name = excluded.display_name
                """,
                (channel_id, viewer, viewer),
            )
            connection.commit()
        local = list_messages(connection, channel_id=channel_id)
        replies: list[dict[str, Any]] = []
        errors: list[str] = []
        client = client_of(request)
        if client.configured:
            for member in channel["members"]:
                session_id = member["agentscope_session_id"]
                if not session_id:
                    continue
                employee = load_employee(connection, member["employee_id"])
                if not employee["agentscope_agent_id"]:
                    continue
                try:
                    remote = await client.list_session_messages(employee["agentscope_agent_id"], session_id)
                except AgentScopeError as exc:
                    errors.append(f"{employee['name']}：{exc.detail}")
                    continue
                replies.extend(assistant_messages(remote, employee["id"]))
        channel["messages"] = interleave_messages(local, replies)
        channel["history_error"] = " ".join(errors)
        return channel

    def mentions(text: str, members: list[dict[str, Any]]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        remaining = text
        for employee in sorted(members, key=lambda item: len(item["name"]), reverse=True):
            token = f"@{employee['name']}"
            if token in remaining:
                found.append(employee)
                remaining = remaining.replace(token, "")
        return found

    async def ensure_record_session(
        connection,
        client: AgentScopeClient,
        channel: dict[str, Any],
    ) -> tuple[str, str]:
        row = connection.execute(
            "SELECT record_agent_id, record_session_id FROM channels WHERE id = ?",
            (channel["id"],),
        ).fetchone()
        if row["record_session_id"] and row["record_agent_id"]:
            return row["record_agent_id"], row["record_session_id"]
        agent_id = ""
        for member in channel["members"]:
            employee = load_employee(connection, member["employee_id"])
            if employee["agentscope_agent_id"]:
                agent_id = employee["agentscope_agent_id"]
                break
        if not agent_id:
            return "", ""
        record_employee = next(
            (
                load_employee(connection, member["employee_id"])
                for member in channel["members"]
                if load_employee(connection, member["employee_id"])["agentscope_agent_id"] == agent_id
            ),
            None,
        )
        session_id = await client.create_session(
            agent_id,
            f"记录:{channel['name']}",
            chat_model_config=await model_for(client, record_employee) if record_employee else None,
            knowledge_base_ids=bound_knowledge(record_employee),
        )
        connection.execute(
            """
            UPDATE channels SET record_agent_id = ?, record_session_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (agent_id, session_id, utcnow(), channel["id"]),
        )
        connection.commit()
        return agent_id, session_id

    @app.post("/api/v1/channels/{channel_id}/messages")
    async def send_channel(
        channel_id: str,
        body: MessageBody,
        request: Request,
        connection=Depends(database),
        x_owner_id: str | None = Header(default=None),
        x_verified_uid: str | None = Header(default=None),
    ):
        channel = channel_payload(connection, channel_id)
        trusted = (x_verified_uid or x_owner_id or "").strip()
        speaker = trusted or "user"
        if trusted:
            connection.execute(
                """
                INSERT INTO channel_humans (channel_id, user_uid, display_name)
                VALUES (?, ?, ?)
                ON CONFLICT(channel_id, user_uid) DO UPDATE SET display_name = excluded.display_name
                """,
                (channel_id, speaker, speaker),
            )
            connection.commit()
        if body.attachment_ids and not trusted:
            raise HTTPException(status_code=401, detail="缺少 X-Verified-UID")
        attachments = load_attachments(connection, body.attachment_ids, trusted or None)
        message = store_user_message(
            connection,
            body.text,
            attachments,
            speaker_uid=speaker if speaker != "user" else "",
            conversation_id=None,
            channel_id=channel_id,
        )
        members = [load_employee(connection, item["employee_id"]) for item in channel["members"]]
        named = mentions(body.text, members)
        client = client_of(request)
        record = {"accepted": False, "detail": ""}
        if client.configured:
            try:
                _agent_id, record_session = await ensure_record_session(connection, client, channel)
                if record_session:
                    await client.record_message(
                        record_session,
                        message["id"],
                        speaker,
                        message["text"],
                        {"verified_uid": speaker} if speaker != "user" else {},
                    )
                    record = {"accepted": True, "detail": ""}
            except AgentScopeError as exc:
                record = {"accepted": False, "detail": exc.detail}
                if exc.status_code == 409:
                    return {
                        "message": message,
                        "triggered": [
                            {
                                "employee_id": employee["id"],
                                "name": employee["name"],
                                "accepted": False,
                                "detail": exc.detail,
                                "session_id": None,
                            }
                            for employee in named
                        ],
                        "record": record,
                    }
        request.app.state.human_events.publish(
            channel_id,
            speaker,
            {"id": message["id"], "text": message["text"], "speaker": speaker},
        )
        files = [(item["name"], item["type"], Path(item["path"]).read_bytes()) for item in attachments]
        content = model_input_blocks(body.text, files)
        history = [
            message_from_row(row)
            for row in connection.execute(
                "SELECT * FROM messages WHERE channel_id = ? ORDER BY created_at, id",
                (channel_id,),
            ).fetchall()
        ]
        for item in history:
            item["speaker_uid"] = item.get("speaker") or ""
            item["content"] = model_input_blocks(
                str(item.get("text") or ""),
                attachment_bytes(connection, item),
            )
        triggered: list[dict[str, Any]] = []
        for employee in named:
            result = {
                "employee_id": employee["id"],
                "name": employee["name"],
                "accepted": False,
                "detail": "",
                "session_id": None,
            }
            if not employee["enabled"]:
                result["detail"] = f"{employee['name']} 已禁用，没有触发"
                triggered.append(result)
                continue
            if not employee["agentscope_agent_id"]:
                result["detail"] = "AgentScope 尚未创建该员工，没有生成回复"
                triggered.append(result)
                continue
            binding = connection.execute(
                """
                SELECT agentscope_session_id, last_delivered_message_id
                FROM channel_members WHERE channel_id = ? AND employee_id = ?
                """,
                (channel_id, employee["id"]),
            ).fetchone()
            try:
                session_id = await claim_member_session(
                    connection,
                    client,
                    employee,
                    channel_id,
                    channel["name"],
                )
                await attach_task_capability(client, employee, session_id, channel_id=channel_id)
                pending = messages_since(history, binding["last_delivered_message_id"] or "")
                await client.trigger_inputs(
                    employee["agentscope_agent_id"],
                    session_id,
                    chat_inputs(
                        pending,
                        speaker=speaker,
                        verified_uid=speaker if speaker != "user" else "",
                        current_content=content,
                    ),
                )
                connection.execute(
                    """
                    UPDATE channel_members SET last_delivered_message_id = ?
                    WHERE channel_id = ? AND employee_id = ?
                    """,
                    (message["id"], channel_id, employee["id"]),
                )
                connection.commit()
                result.update(accepted=True, session_id=session_id)
            except AgentScopeError as exc:
                result["detail"] = exc.detail
                result["session_id"] = binding["agentscope_session_id"]
            triggered.append(result)
        if any(item["accepted"] for item in triggered):
            connection.execute("UPDATE messages SET accepted = 1 WHERE id = ?", (message["id"],))
            connection.commit()
            message = message_from_row(
                connection.execute("SELECT * FROM messages WHERE id = ?", (message["id"],)).fetchone()
            )
        employee_messages = connection.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE channel_id = ? AND sender != 'user'",
            (channel_id,),
        ).fetchone()
        if employee_messages["count"]:
            raise HTTPException(status_code=500, detail="团队消息写入了非用户发言")
        return {"message": message, "triggered": triggered, "record": record}

    @app.get("/api/v1/channels/{channel_id}/events")
    async def channel_events(
        channel_id: str,
        request: Request,
        owner_id: str = Depends(require_owner),
        connection=Depends(database),
    ):
        channel_payload(connection, channel_id)
        queue = request.app.state.human_events.subscribe(channel_id, owner_id)

        async def generate():
            try:
                while not await request.is_disconnected():
                    payload = await queue.get()
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            finally:
                request.app.state.human_events.unsubscribe(channel_id, queue)

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/api/v1/channels/{channel_id}/timeline")
    async def channel_timeline(
        channel_id: str,
        request: Request,
        connection=Depends(database),
        cursor: str = "",
        limit: int = 50,
    ):
        channel = channel_payload(connection, channel_id)
        local = list_messages(connection, channel_id=channel_id)
        streams: list[dict[str, Any]] = [
            {
                "session_id": channel_id,
                "label": "human",
                "messages": [
                    {
                        "id": item["id"],
                        "role": "user",
                        "name": item.get("speaker") or "user",
                        "created_at": item["created_at"],
                        "content": [{"type": "text", "text": item["text"]}],
                        "metadata": {
                            "display_text": item["text"],
                            "attachments": item.get("attachments") or [],
                        },
                    }
                    for item in local
                ],
            }
        ]
        seen_sessions = {channel_id}
        client = client_of(request)
        if client.configured:
            for member in channel["members"]:
                session_id = member["agentscope_session_id"]
                if not session_id or session_id in seen_sessions:
                    continue
                employee = load_employee(connection, member["employee_id"])
                if not employee["agentscope_agent_id"]:
                    continue
                seen_sessions.add(session_id)
                try:
                    remote = await client.list_session_messages(employee["agentscope_agent_id"], session_id)
                except AgentScopeError:
                    remote = []
                streams.append(
                    {"session_id": session_id, "label": employee["name"], "messages": remote}
                )
                try:
                    views = await client.list_agent_sessions(employee["agentscope_agent_id"])
                except AgentScopeError:
                    views = []
                for peer_session, peer_label, peer_agent in team_peers(views, session_id):
                    if peer_session in seen_sessions or not peer_agent:
                        continue
                    seen_sessions.add(peer_session)
                    try:
                        peer_messages = await client.list_session_messages(peer_agent, peer_session)
                    except AgentScopeError:
                        continue
                    streams.append(
                        {"session_id": peer_session, "label": peer_label, "messages": peer_messages}
                    )
        bounded = limit if 1 <= limit <= 200 else 50
        return merge_timeline(streams, cursor=cursor, limit=bounded)

    @app.get("/api/v1/channels/{channel_id}/members/{employee_id}/stream")
    async def stream_member(
        channel_id: str,
        employee_id: str,
        request: Request,
        connection=Depends(database),
        after: str | None = None,
    ):
        channel = channel_payload(connection, channel_id)
        employee = load_employee(connection, employee_id)
        binding = next((item for item in channel["members"] if item["employee_id"] == employee_id), None)
        if binding is None or not binding["agentscope_session_id"] or not employee["agentscope_agent_id"]:
            raise HTTPException(status_code=409, detail="该成员还没有 AgentScope Session")
        return await proxy_stream(
            client_of(request),
            employee["agentscope_agent_id"],
            binding["agentscope_session_id"],
            stream_cursor(after),
        )

    @app.get("/api/v1/peers/resolve")
    async def resolve_peer(
        session_id: str,
        name: str,
        request: Request,
        connection=Depends(database),
        message: str = "",
    ) -> dict[str, str]:
        target_name = name.strip()
        private = connection.execute(
            "SELECT * FROM conversations WHERE agentscope_session_id = ?",
            (session_id,),
        ).fetchone()
        if private is not None:
            sender = load_employee(connection, private["employee_id"])
            target = employee_by_name(connection, target_name)
            if target is not None and target["id"] != sender["id"] and not target["enabled"]:
                raise HTTPException(status_code=404, detail="该员工已禁用，不能开始新的运行")
            unreachable = (
                target is None
                or target["id"] == sender["id"]
                or not target["agentscope_agent_id"]
                or not target["mailbox_session_id"]
            )
            if unreachable:
                raise HTTPException(status_code=404, detail="没有找到可联系的员工")
            return await hand_off(
                connection,
                client_of(request),
                f"private:{private['id']}",
                sender,
                target,
                message,
                target["mailbox_session_id"],
            )
        member = connection.execute(
            "SELECT * FROM channel_members WHERE agentscope_session_id = ?",
            (session_id,),
        ).fetchone()
        if member is None:
            mailbox_owner = connection.execute(
                "SELECT id FROM employees WHERE mailbox_session_id = ?",
                (session_id,),
            ).fetchone()
            if mailbox_owner is None:
                raise HTTPException(status_code=404, detail="没有找到可联系的员工")
            sender = load_employee(connection, mailbox_owner["id"])
            target = employee_by_name(connection, target_name)
            if target is not None and target["id"] != sender["id"] and not target["enabled"]:
                raise HTTPException(status_code=404, detail="该员工已禁用，不能开始新的运行")
            unreachable = (
                target is None
                or target["id"] == sender["id"]
                or not target["agentscope_agent_id"]
                or not target["mailbox_session_id"]
            )
            if unreachable:
                raise HTTPException(status_code=404, detail="没有找到可联系的员工")
            scope = inbound_private_scope(connection, sender["agentscope_agent_id"])
            if scope is None:
                scope = f"mailbox:{sender['id']}"
            return await hand_off(
                connection,
                client_of(request),
                scope,
                sender,
                target,
                message,
                target["mailbox_session_id"],
            )
        sender = load_employee(connection, member["employee_id"])
        if not sender["agentscope_agent_id"]:
            raise HTTPException(status_code=404, detail="没有找到可联系的员工")
        channel = connection.execute(
            "SELECT name FROM channels WHERE id = ?",
            (member["channel_id"],),
        ).fetchone()
        peers = connection.execute(
            "SELECT * FROM channel_members WHERE channel_id = ?",
            (member["channel_id"],),
        ).fetchall()
        client = client_of(request)
        for peer in peers:
            employee = load_employee(connection, peer["employee_id"])
            if employee["name"] != target_name or employee["id"] == sender["id"]:
                continue
            if not employee["enabled"]:
                raise HTTPException(status_code=404, detail="该员工已禁用，不能开始新的运行")
            if not employee["agentscope_agent_id"]:
                raise HTTPException(status_code=404, detail="没有找到可联系的员工")
            peer_session = peer["agentscope_session_id"]
            if not peer_session:
                if not client.configured:
                    raise HTTPException(status_code=503, detail="AgentScope 未连接，没有生成回复")
                try:
                    peer_session = await claim_member_session(
                        connection,
                        client,
                        employee,
                        member["channel_id"],
                        channel["name"] if channel is not None else employee["name"],
                    )
                except AgentScopeError as exc:
                    raise HTTPException(status_code=503, detail=exc.detail) from exc
            await attach_task_capability(
                client,
                employee,
                peer_session,
                channel_id=member["channel_id"],
            )
            return await hand_off(
                connection,
                client,
                member["channel_id"],
                sender,
                employee,
                message,
                peer_session,
            )
        raise HTTPException(status_code=404, detail="没有找到可联系的员工")

    async def proxy_stream(
        client: AgentScopeClient,
        agent_id: str,
        session_id: str,
        after: str | None = None,
        raw: bool = False,
    ) -> StreamingResponse:
        if not client.configured:
            raise HTTPException(status_code=503, detail="AgentScope 未连接，没有生成回复")
        if after is None:
            cursor = await client.latest_event_cursor(agent_id, session_id)
            if cursor and STREAM_CURSOR.fullmatch(cursor):
                after = cursor

        async def generate():
            started = 0
            pending = b""
            try:
                async with client.stream_session(agent_id, session_id, after) as response:
                    if response.status_code >= 400:
                        detail = (await response.aread()).decode("utf-8", "replace")
                        payload = json.dumps(
                            {"type": "NEXUS_UPSTREAM_ERROR", "detail": detail},
                            ensure_ascii=False,
                        )
                        yield f"data: {payload}\n\n"
                        return
                    async for chunk in response.aiter_bytes():
                        pending += chunk
                        frames, pending = split_sse(pending)
                        for frame in frames:
                            yield frame + b"\n\n"
                            if raw:
                                # 原生客户端自己判断一轮是否结束；这里不注入收尾事件，
                                # 也不因为 REPLY_END 关闭连接，否则第二条回复收不到。
                                continue
                            started, terminal, close = observe_stream_event(started, sse_payload(frame))
                            if terminal is not None:
                                encoded = json.dumps(terminal, ensure_ascii=False)
                                yield f"data: {encoded}\n\n".encode()
                            if close:
                                return
            except AgentScopeError as exc:
                payload = json.dumps(
                    {"type": "NEXUS_UPSTREAM_ERROR", "detail": exc.detail},
                    ensure_ascii=False,
                )
                yield f"data: {payload}\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")

    def raise_im(exc: ImError) -> None:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.post("/api/v1/im/bots")
    def save_im_bot(body: ImBotBody, connection=Depends(database)) -> dict[str, str]:
        load_employee(connection, body.employee_id)
        try:
            return bind_bot(connection, body.employee_id, body.provider, body.bot_id)
        except ImError as exc:
            raise_im(exc)

    @app.post("/api/v1/im/groups")
    def save_im_group(body: ImGroupBody, connection=Depends(database)) -> dict[str, str]:
        load_employee(connection, body.employee_id)
        try:
            return open_external(connection, body.employee_id, body.external_key, body.session_id)
        except ImError as exc:
            raise_im(exc)

    @app.post("/api/v1/im/events")
    async def im_event(body: ImEventBody, request: Request, connection=Depends(database)) -> dict[str, Any]:
        client = client_of(request)

        async def ensure_private_chat(employee_id: str, owner_id: str) -> str:
            employee = load_employee(connection, employee_id)
            existing = connection.execute(
                """
                SELECT * FROM conversations
                WHERE employee_id = ? AND owner_id = ? AND kind = 'private'
                """,
                (employee_id, owner_id),
            ).fetchone()
            if existing is None:
                conversation_id = new_id("conv")
                now = utcnow()
                connection.execute(
                    """
                    INSERT OR IGNORE INTO conversations (
                        id, employee_id, owner_id, kind, name, agentscope_session_id, created_at, updated_at
                    ) VALUES (?, ?, ?, 'private', '私聊', NULL, ?, ?)
                    """,
                    (conversation_id, employee_id, owner_id, now, now),
                )
                connection.commit()
                existing = connection.execute(
                    """
                    SELECT * FROM conversations
                    WHERE employee_id = ? AND owner_id = ? AND kind = 'private'
                    """,
                    (employee_id, owner_id),
                ).fetchone()
            if existing is None:
                return ""
            if existing["agentscope_session_id"]:
                return existing["agentscope_session_id"]
            if not client.configured or not employee["agentscope_agent_id"]:
                return ""
            conversation_id = existing["id"]
            async with await conversation_lock(conversation_id):
                current = connection.execute(
                    "SELECT * FROM conversations WHERE id = ?",
                    (conversation_id,),
                ).fetchone()
                if current["agentscope_session_id"]:
                    return current["agentscope_session_id"]
                workspace_id = current["workspace_id"] or f"ws-{conversation_id}"
                session_id = await client.create_session(
                    employee["agentscope_agent_id"],
                    current["name"] or "私聊",
                    workspace_id,
                    chat_model_config=await model_for(client, employee),
                    knowledge_base_ids=bound_knowledge(employee),
                )
                await client.set_permission_mode(
                    employee["agentscope_agent_id"],
                    session_id,
                    UNATTENDED_PERMISSION,
                )
                return claim_private_session(connection, conversation_id, session_id, workspace_id)

        prior_reply: dict[str, Any] = {"cursor": None, "known": set()}

        async def accept(employee_id: str, session_id: str, text: str) -> None:
            employee = load_employee(connection, employee_id)
            agent_id = employee["agentscope_agent_id"]
            if not client.configured or not agent_id:
                raise ImError("AgentScope 还没有接受这次输入", 503)
            prior_reply["cursor"] = await client.latest_event_cursor(agent_id, session_id)
            try:
                existing = await client.list_session_messages(agent_id, session_id)
            except AgentScopeError:
                existing = []
            prior_reply["known"] = {
                str(item.get("id"))
                for item in existing
                if isinstance(item, dict) and item.get("role") == "assistant" and item.get("id")
            }
            await client.trigger_chat(
                agent_id,
                session_id,
                [{"type": "text", "text": text}],
                body.sender_uid or "im",
                verified_uid=body.sender_uid or None,
            )

        try:
            result = await accept_inbound(
                connection,
                provider=body.provider,
                event_id=body.event_id,
                bot_id=body.bot_id,
                sender_uid=body.sender_uid,
                text=body.text,
                accept=accept,
                external_key=body.external_key,
                conversation_key=body.conversation_key,
                ensure_private=ensure_private_chat,
            )
        except ImError as exc:
            raise_im(exc)
        except AgentScopeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        if not result.get("duplicate"):
            key = body.conversation_key.strip() or body.external_key.strip() or body.sender_uid.strip()
            employee = load_employee(connection, result["employee_id"])
            await capture_im_reply(
                connection,
                client,
                employee,
                result["session_id"],
                key,
                after=prior_reply["cursor"],
                known_reply_ids=prior_reply["known"],
            )
        return result

    @app.get("/api/v1/im/outbox")
    def im_outbox(connection=Depends(database)) -> dict[str, str]:
        item = claim_outbox(connection)
        if item is None:
            raise HTTPException(status_code=404, detail="没有待投递的消息")
        return item

    @app.post("/api/v1/im/contacts")
    def im_contact(body: ImContactBody, connection=Depends(database)) -> dict[str, str]:
        employee_id = body.employee_id.strip()
        if not employee_id and body.agent_id.strip():
            row = connection.execute(
                "SELECT id FROM employees WHERE agentscope_agent_id = ?",
                (body.agent_id.strip(),),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="没有找到可联系的员工")
            employee_id = row["id"]
        load_employee(connection, employee_id)
        try:
            return contact_human(
                connection,
                employee_id=employee_id,
                conversation_key=body.conversation_key,
                session_id=body.session_id,
                body=body.body,
            )
        except ImError as exc:
            raise_im(exc)

    @app.post("/api/v1/im/acks")
    def im_ack(body: ImAckBody, connection=Depends(database)) -> dict[str, str]:
        try:
            return acknowledge(connection, body.delivery_id)
        except ImError as exc:
            raise_im(exc)

    @app.post("/api/v1/im/redeliver")
    def im_redeliver(body: ImRedeliverBody, connection=Depends(database)) -> dict[str, str]:
        try:
            return redeliver(connection, body.conversation_key)
        except ImError as exc:
            raise_im(exc)

    return app


def split_sse(buffer: bytes) -> tuple[list[bytes], bytes]:
    parts = buffer.split(b"\n\n")
    if buffer.endswith(b"\n\n"):
        return parts[:-1], b""
    return parts[:-1], parts[-1]


def sse_payload(frame: bytes) -> dict[str, Any] | None:
    for line in frame.decode("utf-8", "replace").split("\n"):
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
    return None


def observe_stream_event(
    started: int,
    payload: dict[str, Any] | None,
) -> tuple[int, dict[str, Any] | None, bool]:
    """REPLY_END 是 AgentScope 暴露的一轮结束信号。

    重放日志里可能只剩上一轮的 REPLY_END。只有本连接里见过 REPLY_START 的那一轮结束时，
    才额外发出 NEXUS_RUN_FINISHED 并关掉这次 SSE。
    """
    if not payload:
        return started, None, False
    kind = payload.get("type")
    if kind == "REPLY_START":
        return started + 1, None, False
    if kind == "REPLY_END" and started > 0:
        started -= 1
        terminal = {
            "type": "NEXUS_RUN_FINISHED",
            "reply_id": payload.get("reply_id"),
            "finished_reason": payload.get("finished_reason"),
        }
        return started, terminal, started == 0
    return started, None, False


def team_peers(views: list[dict[str, Any]], leader_session_id: str) -> list[tuple[str, str, str]]:
    """AgentInvite 写进 team 的成员会话。leader 自己的会话不重复返回。"""

    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for view in views:
        team = view.get("team") if isinstance(view.get("team"), dict) else {}
        record = team.get("team") if isinstance(team.get("team"), dict) else {}
        members = team.get("members") if isinstance(team.get("members"), list) else []
        session = view.get("session") if isinstance(view.get("session"), dict) else {}
        related = record.get("session_id") == leader_session_id or session.get("id") == leader_session_id
        if not related:
            related = any(
                isinstance(member, dict) and member.get("session_id") == leader_session_id
                for member in members
            )
        if not related:
            continue
        for member in members:
            if not isinstance(member, dict):
                continue
            session_id = str(member.get("session_id") or "")
            if not session_id or session_id == leader_session_id or session_id in seen:
                continue
            agent = member.get("agent") if isinstance(member.get("agent"), dict) else {}
            seen.add(session_id)
            found.append((session_id, str(agent.get("name") or session_id), str(agent.get("id") or "")))
    return found


REPLY_LOOKUP_DELAYS = (0.2, 0.5, 1.0, 2.0)


async def assistant_reply_text(client, agent_id: str, session_id: str, reply_id: str) -> str:
    """REPLY_END 到达时，这条助手消息可能还没写入。按固定间隔再查几次。"""

    pauses = (0.0, *REPLY_LOOKUP_DELAYS)
    for delay in pauses:
        if delay:
            await asyncio.sleep(delay)
        messages = await client.list_session_messages(agent_id, session_id)
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            if str(message.get("id") or "") != reply_id:
                continue
            return block_text(message.get("content"))
    return ""


async def capture_im_reply(
    connection,
    client,
    employee: dict[str, Any],
    session_id: str,
    conversation_key: str,
    after: str | None = None,
    known_reply_ids: set[str] | None = None,
) -> None:
    """从触发 chat 之前的游标订阅。重放里的上一轮 reply end 不入队。"""

    agent_id = employee.get("agentscope_agent_id")
    if not client.configured or not agent_id or not conversation_key or not session_id:
        return
    known = known_reply_ids or set()
    started = 0
    pending = b""
    try:
        async with client.stream_session(agent_id, session_id, after=after) as response:
            if response.status_code >= 400:
                return
            async for chunk in response.aiter_bytes():
                pending += chunk
                frames, pending = split_sse(pending)
                for frame in frames:
                    started, terminal, close = observe_stream_event(started, sse_payload(frame))
                    if not close or not terminal:
                        continue
                    reply_id = str(terminal.get("reply_id") or "")
                    if reply_id and reply_id in known:
                        continue
                    body = await assistant_reply_text(client, agent_id, session_id, reply_id)
                    if body.strip():
                        contact_human(
                            connection,
                            employee_id=employee["id"],
                            conversation_key=conversation_key,
                            session_id=session_id,
                            body=body,
                        )
                    return
    except AgentScopeError:
        return


def stream_cursor(after: str | None) -> str | None:
    """Agent Service 的 after 是 Redis 回放游标，不是 AgentEvent.id。"""
    if after and STREAM_CURSOR.fullmatch(after):
        return after
    return None


def employee_by_name(connection, name: str) -> dict[str, Any] | None:
    rows = connection.execute("SELECT * FROM employees").fetchall()
    for row in rows:
        employee = employee_from_row(row)
        if employee["name"] == name:
            return employee
    return None


app = create_app()
