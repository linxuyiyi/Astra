"""人类群的积压投递和群时间线。不调用推理。"""

from __future__ import annotations

import asyncio
import re
from typing import Any

_TEAM_FROM = re.compile(r'<team-message from="([^"]*)">')
_TEAM_BODY = re.compile(r"<team-message\b[^>]*>\n?(.*?)\n?</team-message>", re.DOTALL)


def messages_since(messages: list[dict[str, Any]], cursor_id: str) -> list[dict[str, Any]]:
    """游标是已投递的最后一条。空游标表示该成员还没听过这段群记录。"""

    if not cursor_id:
        return list(messages)
    for index, message in enumerate(messages):
        if message.get("id") == cursor_id:
            return messages[index + 1 :]
    return []


def chat_inputs(
    messages: list[dict[str, Any]],
    *,
    speaker: str,
    verified_uid: str,
    current_content: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """积压和当前 @ 共用原消息 id，一次送进 POST /chat/。"""

    payload: list[dict[str, Any]] = []
    last = len(messages) - 1
    for index, message in enumerate(messages):
        own = str(message.get("speaker_uid") or message.get("speaker") or "")
        name = own or speaker or "user"
        uid = own or verified_uid
        content = message.get("content")
        if not content:
            if index == last:
                content = current_content
            else:
                content = [{"type": "text", "text": message.get("text") or ""}]
        metadata: dict[str, Any] = {"display_text": str(message.get("text") or "")}
        if uid:
            metadata["verified_uid"] = uid
        attachments = message.get("attachments")
        if isinstance(attachments, list) and attachments:
            metadata["attachments"] = attachments
        item: dict[str, Any] = {
            "id": message["id"],
            "name": name,
            "role": "user",
            "content": content,
            "metadata": metadata,
        }
        payload.append(item)
    return payload


class HumanEvents:
    """群里一次纯写入后，只通知其他人类成员。"""

    def __init__(self) -> None:
        self._listeners: dict[str, list[tuple[str, asyncio.Queue]]] = {}

    def subscribe(self, channel_id: str, uid: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._listeners.setdefault(channel_id, []).append((uid, queue))
        return queue

    def unsubscribe(self, channel_id: str, queue: asyncio.Queue) -> None:
        current = self._listeners.get(channel_id, [])
        self._listeners[channel_id] = [(uid, item) for uid, item in current if item is not queue]

    def publish(self, channel_id: str, sender_uid: str, payload: dict[str, Any]) -> int:
        sent = 0
        for uid, queue in self._listeners.get(channel_id, []):
            if uid == sender_uid:
                continue
            queue.put_nowait(payload)
            sent += 1
        return sent


def team_sender(text: str) -> str:
    match = _TEAM_FROM.search(text)
    return match.group(1) if match else ""


def team_body(text: str) -> str:
    match = _TEAM_BODY.search(text)
    return match.group(1).strip() if match else text.strip()


def _cursor(row: dict[str, Any]) -> str:
    return f"{row['created_at']}|{row['session_id']}|{row['id']}"


def _after(row: dict[str, Any], cursor: str) -> bool:
    if not cursor:
        return True
    created, session_id, ident = cursor.split("|", 2)
    return (row["created_at"], row["session_id"], row["id"]) > (created, session_id, ident)


def merge_timeline(
    streams: list[dict[str, Any]],
    *,
    cursor: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """人类消息按 id 只留一条。同一 HintBlock id 的 TeamSay 只留一条。

    streams 的每一项是一个 Session：session_id、label、messages。
    定向只有一个接收方时显示 A→B。多个接收方盖住其余成员时显示 A→全体。
    """

    member_labels = {str(stream.get("label") or stream["session_id"]) for stream in streams}
    seen_users: set[str] = set()
    hints: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for stream in streams:
        session_id = str(stream["session_id"])
        label = str(stream.get("label") or session_id)
        for message in stream.get("messages") or []:
            if not isinstance(message, dict):
                continue
            created_at = str(message.get("created_at") or "")
            if message.get("role") == "user":
                metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
                raw_id = metadata.get("message_id") or message.get("id")
                message_id = str(raw_id or "")
                display = metadata.get("display_text")
                text = display if isinstance(display, str) else _plain_text(message.get("content"))
                speaker = str(message.get("name") or "user")
                # 只按消息 id 去重：同一条人类消息会被写进多个员工会话，id 相同才是同一条。
                # 曾经再按「发送人 + 文本 + 分钟」去重，会把用户同一分钟内发送的两条相同文字
                # 折叠成一条；那是掩盖 id 不一致，不是修复。
                if not message_id or message_id in seen_users:
                    continue
                seen_users.add(message_id)
                raw_attachments = metadata.get("attachments")
                attachments = raw_attachments if isinstance(raw_attachments, list) else []
                rows.append(
                    {
                        "id": message_id,
                        "session_id": session_id,
                        "created_at": created_at,
                        "kind": "human",
                        "speaker": str(message.get("name") or "user"),
                        "text": text,
                        "attachments": attachments,
                    }
                )
                continue
            plain: list[str] = []
            message_id = str(message.get("id") or "")
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text = str(block.get("text") or "")
                    if text:
                        plain.append(text)
                    continue
                if block.get("type") != "hint":
                    continue
                text = str(block.get("text") or block.get("hint") or "")
                if "<team-message" not in text:
                    continue
                hint_id = str(block.get("id") or message_id)
                if not hint_id:
                    continue
                slot = hints.setdefault(
                    hint_id,
                    {
                        "id": hint_id,
                        "created_at": created_at,
                        "session_id": session_id,
                        "sender": team_sender(text),
                        "text": team_body(text),
                        "labels": [],
                    },
                )
                slot["labels"].append(label)
                if (created_at, session_id, hint_id) < (slot["created_at"], slot["session_id"], slot["id"]):
                    slot["created_at"] = created_at
                    slot["session_id"] = session_id
            if plain and message_id:
                rows.append(
                    {
                        "id": message_id,
                        "session_id": session_id,
                        "created_at": created_at,
                        "kind": "reply",
                        "speaker": label,
                        "text": "\n".join(plain),
                        "attachments": [],
                    }
                )
    for hint in hints.values():
        rows.append(
            {
                "id": hint["id"],
                "session_id": hint["session_id"],
                "created_at": hint["created_at"],
                "kind": "team",
                "speaker": f"{hint['sender']}→{_team_target(hint['sender'], hint['labels'], member_labels)}",
                "text": hint["text"],
            }
        )
    rows.sort(key=lambda item: (item["created_at"], item["session_id"], item["id"]))
    if cursor:
        visible = [row for row in rows if _after(row, cursor)]
        page = visible[:limit]
        next_cursor = _cursor(page[-1]) if len(visible) > limit and page else ""
    else:
        page = rows[-limit:] if limit else list(rows)
        next_cursor = ""
    return {"items": page, "next_cursor": next_cursor}


def _team_target(sender: str, labels: list[str], member_labels: set[str]) -> str:
    recipients = {label for label in labels if label != sender}
    if not recipients:
        recipients = set(labels)
    if len(recipients) == 1:
        return next(iter(recipients))
    others = {label for label in member_labels if label != sender}
    if others and recipients >= others:
        return "全体"
    return "、".join(sorted(recipients))


def _plain_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts)
