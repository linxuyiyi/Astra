"""Worker 端只读轨迹读取：分页、裁剪并限制响应大小。"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_TIMEOUT_SECONDS = 60.0
_MAX_PART_CHARS = 8000
_SAFE_SESSION = ("ses_", "session_")


def _text(value: Any, limit: int = _MAX_PART_CHARS) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n...\n" + text[-limit // 2 :]


def _trim_message(message: dict[str, Any]) -> dict[str, Any]:
    result = {"info": message.get("info") if isinstance(message.get("info"), dict) else {}}
    parts = []
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        item = {
            key: value
            for key, value in part.items()
            if key in {"id", "type", "tool", "status", "text"}
        }
        if "text" in item:
            item["text"] = _text(item["text"])
        state = part.get("state")
        if isinstance(state, dict):
            item["state"] = {
                "status": state.get("status"),
                "output": _text(state.get("output")),
                "error": _text(state.get("error")),
                "metadata": {
                    key: value
                    for key, value in (state.get("metadata") or {}).items()
                    if key in {"exit", "description", "truncated"}
                },
            }
        parts.append(item)
    result["parts"] = parts
    return result


def read_trace_from_payload(
    opencode_url: str,
    workspace_root: str,
    data: Any,
) -> dict[str, Any]:
    options = data if isinstance(data, dict) else {}
    session_id = str(options.get("session_id") or "")
    if not session_id.startswith(_SAFE_SESSION) or not all(
        char.isalnum() or char in "_-" for char in session_id
    ):
        raise ValueError("无效的 session_id")
    workspace = Path(str(options.get("workspace") or "")).resolve()
    root = Path(workspace_root).resolve()
    if not workspace.is_relative_to(root):
        raise ValueError("工作区不在 Worker 根目录内")
    try:
        limit = max(1, min(50, int(options.get("limit") or 20)))
    except (TypeError, ValueError) as exc:
        raise ValueError("无效的分页大小") from exc
    before = options.get("before")
    if before is not None and (not isinstance(before, str) or len(before) > 256):
        raise ValueError("无效的分页游标")
    base = str(opencode_url or "").rstrip("/")
    if not base.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise ValueError("OpenCode 地址必须是 loopback")
    query: dict[str, Any] = {"limit": limit, "directory": str(workspace)}
    if before:
        query["before"] = before
    url = f"{base}/session/{urllib.parse.quote(session_id, safe='')}/message?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        raw = response.read()
        next_before = response.headers.get("x-next-cursor")
    try:
        messages = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("OpenCode 返回不是合法 JSON") from exc
    if not isinstance(messages, list):
        raise ValueError("OpenCode 返回不是消息数组")
    trimmed = [_trim_message(item) for item in messages if isinstance(item, dict)]
    # 保证单帧可被 Worker Gateway 接收；优先保留最新消息。
    budget = 131072
    while trimmed and len(json.dumps(trimmed, ensure_ascii=False).encode()) > budget:
        trimmed.pop(0)
    return {"messages": trimmed, "next_before": next_before, "truncated": len(trimmed) < len(messages)}
