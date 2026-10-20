"""OpenCode Event → NEXUS Assistant Event 的纯函数映射。

前端完全不依赖 OpenCode Event Schema；将来 OpenCode 升级时只改这里。
输入是 OpenCode Server SSE 的原始事件 ``{"type": ..., "properties": {...}}``。
"""

from __future__ import annotations

import json
from typing import Any


def _properties(event: dict[str, Any]) -> dict[str, Any]:
    properties = event.get("properties")
    return properties if isinstance(properties, dict) else {}


def _info(properties: dict[str, Any]) -> dict[str, Any]:
    info = properties.get("info")
    return info if isinstance(info, dict) else {}


def _part(properties: dict[str, Any]) -> dict[str, Any]:
    part = properties.get("part")
    return part if isinstance(part, dict) else {}


def _state(part: dict[str, Any]) -> dict[str, Any]:
    state = part.get("state")
    return state if isinstance(state, dict) else {}


def session_id_of(event: dict[str, Any]) -> str | None:
    """事件归属的 OpenCode Session。"""
    properties = _properties(event)
    part = _part(properties)
    info = _info(properties)
    value = properties.get("sessionID") or part.get("sessionID") or info.get("sessionID")
    return str(value) if value else None


def normalize_tool_name(raw: str) -> str:
    """还原平台工具的业务名。

    OpenCode 会给 MCP 工具加前缀，例如 ``mcp__nexus-assistant__nexus_task_get``
    或 ``nexus-assistant_nexus_task_get``，统一截取 ``nexus_`` 起始部分。
    """
    name = str(raw or "").strip()
    if name.startswith("mcp__"):
        pieces = name.split("__")
        name = pieces[-1] if len(pieces) >= 3 else name
    index = name.find("nexus_")
    if index > 0:
        name = name[index:]
    return name


def tool_output_payload(part: dict[str, Any]) -> Any:
    """工具输出可能是字符串（MCP 文本结果）或结构化对象，尽量解析为 JSON。"""
    state = _state(part)
    output = state.get("output")
    if output is None:
        output = part.get("output")
    if isinstance(output, str):
        text = output.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return json.loads(text)
            except ValueError:
                return output
        return output
    return output


def map_server_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    """把一条 OpenCode 事件映射为零到多条助手事件。

    返回结构：``{"event": 名称, "session_id": ..., "data": {...}}``，
    由 Orchestrator 补充 conversation_id、平台 message_id 后推送给前端。
    """
    if not isinstance(event, dict):
        return []
    event_type = str(event.get("type") or "")
    properties = _properties(event)
    info = _info(properties)
    part = _part(properties)
    session_id = session_id_of(event)
    mapped: list[dict[str, Any]] = []

    if event_type == "message.part.delta":
        # 增量文本：{sessionID, messageID, partID, field, delta}
        if str(properties.get("field") or "") == "text":
            mapped.append(
                {
                    "event": "assistant.message.text_delta",
                    "session_id": session_id,
                    "data": {
                        "opencode_message_id": str(properties.get("messageID") or "") or None,
                        "part_id": str(properties.get("partID") or ""),
                        "delta": str(properties.get("delta") or ""),
                    },
                }
            )
        return mapped

    if event_type == "message.part.updated":
        part_type = str(part.get("type") or "")
        message_id = str(part.get("messageID") or info.get("id") or "") or None
        if part_type == "reasoning":
            # 推理片段不得进入用户可见内容；Orchestrator 记录 part_id 后跳过其增量。
            mapped.append(
                {
                    "event": "assistant.message.reasoning",
                    "session_id": session_id,
                    "data": {
                        "opencode_message_id": message_id,
                        "part_id": str(part.get("id") or ""),
                    },
                }
            )
        elif part_type == "text":
            # 真实事件里 part 事件不携带角色；角色由 message.updated 单独提供，
            # 由 Orchestrator 按 opencode_message_id 归并。
            mapped.append(
                {
                    "event": "assistant.message.delta",
                    "session_id": session_id,
                    "data": {
                        "opencode_message_id": message_id,
                        "part_id": str(part.get("id") or ""),
                        "text": str(part.get("text") or ""),
                    },
                }
            )
        elif part_type == "tool":
            status = str(_state(part).get("status") or "")
            tool_name = normalize_tool_name(str(part.get("tool") or ""))
            base = {
                "opencode_message_id": message_id,
                "part_id": str(part.get("id") or ""),
                "tool": tool_name,
                "title": str(_state(part).get("title") or ""),
            }
            if status == "completed":
                mapped.append(
                    {
                        "event": "assistant.tool.completed",
                        "session_id": session_id,
                        "data": {
                            **base,
                            "input": _state(part).get("input"),
                            "output": tool_output_payload(part),
                        },
                    }
                )
            elif status in {"error", "failed"}:
                mapped.append(
                    {
                        "event": "assistant.tool.failed",
                        "session_id": session_id,
                        "data": {**base, "error": str(_state(part).get("error") or "")},
                    }
                )
            elif status in {"running", "pending"}:
                mapped.append(
                    {
                        "event": "assistant.tool.started",
                        "session_id": session_id,
                        "data": {**base, "input": _state(part).get("input")},
                    }
                )
        return mapped

    if event_type == "message.updated":
        if info:
            error = info.get("error")
            error_message = None
            if error:
                if isinstance(error, dict):
                    data = error.get("data") if isinstance(error.get("data"), dict) else {}
                    error_message = str(
                        data.get("message") or error.get("message") or error.get("name") or ""
                    )
                else:
                    error_message = str(error)
            mapped.append(
                {
                    "event": "assistant.message.info",
                    "session_id": session_id,
                    "data": {
                        "opencode_message_id": str(info.get("id") or "") or None,
                        "role": str(info.get("role") or ""),
                        "error": error_message,
                    },
                }
            )
        return mapped

    if event_type == "session.idle":
        mapped.append(
            {
                "event": "assistant.completed",
                "session_id": session_id,
                "data": {},
            }
        )
        return mapped

    if event_type == "session.error":
        error = properties.get("error")
        if isinstance(error, dict):
            data = error.get("data") if isinstance(error.get("data"), dict) else {}
            message = str(data.get("message") or error.get("message") or error.get("name") or "")
        else:
            message = str(error or "")
        mapped.append(
            {
                "event": "assistant.failed",
                "session_id": session_id,
                "data": {"error": message or "助手会话异常"},
            }
        )
        return mapped

    return mapped
