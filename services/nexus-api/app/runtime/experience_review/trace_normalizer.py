"""把 OpenCode 原生消息转换为可引用的统一事件。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.runtime.redaction import redact_text

from .schemas import TraceEvent

MAX_EXCERPT = 2000


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _excerpt(value: Any, limit: int = MAX_EXCERPT) -> str:
    text = redact_text(_text(value)).strip()
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n...\n" + text[-limit // 2 :]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _timestamp(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        from datetime import UTC, datetime

        return datetime.fromtimestamp(value / 1000, UTC).isoformat()
    if isinstance(value, str) and value:
        return value
    return None


def _tool_event(session_id: str, message: dict[str, Any], part: dict[str, Any]) -> TraceEvent | None:
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    status = str(state.get("status") or part.get("status") or "")
    output = state.get("output") if "output" in state else state.get("error")
    if output is None:
        output = part.get("text")
    excerpt = _excerpt(output)
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    exit_code = metadata.get("exit") if isinstance(metadata.get("exit"), int) else None
    info = message.get("info") if isinstance(message.get("info"), dict) else {}
    return TraceEvent(
        ref="",
        session_id=session_id,
        message_id=str(info.get("id") or "") or None,
        part_id=str(part.get("id") or "") or None,
        timestamp=_timestamp((info.get("time") or {}).get("created")),
        role=str(info.get("role") or ""),
        kind="tool_call" if status in {"pending", "running"} else "tool_result",
        tool=str(part.get("tool") or "") or None,
        status=status or None,
        exit_code=exit_code,
        content_digest=_digest(excerpt),
        excerpt=excerpt,
    )


def normalize_messages(messages: list[dict[str, Any]], session_id: str) -> list[TraceEvent]:
    events: list[TraceEvent] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        info = message.get("info") if isinstance(message.get("info"), dict) else {}
        role = str(info.get("role") or "")
        message_id = str(info.get("id") or "") or None
        timestamp = _timestamp((info.get("time") or {}).get("created"))
        error = info.get("error")
        if error:
            excerpt = _excerpt(error)
            events.append(
                TraceEvent(
                    ref="",
                    session_id=session_id,
                    message_id=message_id,
                    timestamp=timestamp,
                    role=role,
                    kind="error",
                    status="failed",
                    content_digest=_digest(excerpt),
                    excerpt=excerpt,
                )
            )
        parts = message.get("parts") if isinstance(message.get("parts"), list) else []
        for part in parts:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or "")
            if part_type == "text":
                text = _excerpt(part.get("text"))
                if not text:
                    continue
                kind = "user_input" if role == "user" else "assistant_text"
                events.append(
                    TraceEvent(
                        ref="",
                        session_id=session_id,
                        message_id=message_id,
                        part_id=str(part.get("id") or "") or None,
                        timestamp=timestamp,
                        role=role,
                        kind=kind,
                        content_digest=_digest(text),
                        excerpt=text,
                    )
                )
            elif part_type == "tool":
                event = _tool_event(session_id, message, part)
                if event is not None:
                    events.append(event)
            elif part_type in {"patch", "file"}:
                text = _excerpt(part.get("text") or part.get("diff") or part)
                events.append(
                    TraceEvent(
                        ref="",
                        session_id=session_id,
                        message_id=message_id,
                        part_id=str(part.get("id") or "") or None,
                        timestamp=timestamp,
                        role=role,
                        kind="code_change",
                        content_digest=_digest(text),
                        excerpt=text,
                    )
                )
    return events
