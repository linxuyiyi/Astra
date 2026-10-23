"""固定 DSH v4 事件到通用工作台/复盘的只读投影，不构造 OpenCode message。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from app.runtime.experience_review.schemas import TraceEvent


def text_content(content) -> str:
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item["text"])
        for item in content
        if isinstance(item, dict) and item.get("type") == "text" and "text" in item
    )


def project_event(event: dict, native_id: str) -> dict | None:
    kind, data = event.get("type"), event.get("data", {})
    role, tool, status, call_id = "", None, None, None
    message_id = None
    if kind == "user/message":
        if isinstance(data.get("source"), dict) and data["source"].get("kind") == "runtime-context":
            return None
        role, text, message_id = "user", text_content(data.get("content")), data.get("id")
    elif kind == "assistant/message":
        message = data.get("message", {})
        role, text, message_id = "assistant", text_content(message.get("content")), message.get("id")
        if not text:
            return None  # 工具调用独立投影，reasoning 不向浏览器公开。
    elif kind == "tool/call":
        call_id, tool, role = data.get("callId"), data.get("name"), "assistant"
        arguments = data.get("arguments")
        text = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
        status = "running"
        message_id = call_id
    elif kind == "tool/result":
        message = data.get("message", {})
        call_id, role = message.get("toolCallId"), "tool"
        text, message_id = text_content(message.get("content")), message.get("id")
        status = "failed" if message.get("isError") else "completed"
    elif kind == "turn/end":
        text, status = str(data.get("reason", "")), "settled"
    else:
        return None
    if role == "user":
        text = text.split("\nASTRA_RUNTIME_CONTEXT:", 1)[0]
    return {
        "seq": event["seq"],
        "native_session_id": native_id,
        "turn": data.get("turn"),
        "kind": kind,
        "role": role,
        "text": text,
        "tool": tool,
        "status": status,
        "tool_call_id": call_id,
        "message_id": message_id,
        "timestamp": event.get("time"),
    }


def review_event(event: dict, native_id: str) -> TraceEvent | None:
    value = project_event(event, native_id)
    if not value or not value["message_id"]:
        return None
    text = value["text"]
    return TraceEvent(
        ref=f"dsh:{native_id}:{event['seq']}",
        source_system="dsh",
        session_id=native_id,
        message_id=value["message_id"],
        part_id=value["tool_call_id"],
        role=value["role"],
        kind={"tool/call": "tool_call", "tool/result": "tool_result"}.get(value["kind"], "text"),
        tool=value["tool"],
        status=value["status"],
        timestamp=datetime.fromtimestamp(event["time"] / 1000, UTC).isoformat(),
        content_digest=hashlib.sha256(text.encode()).hexdigest(),
        excerpt=text if len(text) <= 2000 else text[:1000] + "\n...\n" + text[-1000:],
    )
