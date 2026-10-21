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
# Worker Gateway 单帧上限。超过就缩小原生请求页，不能丢弃已读到的消息。
_FRAME_BUDGET = 131072


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

    def fetch(page_limit: int) -> tuple[list[Any], str | None]:
        query: dict[str, Any] = {"limit": page_limit, "directory": str(workspace)}
        if before:
            query["before"] = before
        url = (
            f"{base}/session/{urllib.parse.quote(session_id, safe='')}/message?"
            + urllib.parse.urlencode(query)
        )
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            raw = response.read()
            cursor = response.headers.get("x-next-cursor")
        try:
            messages = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("OpenCode 返回不是合法 JSON") from exc
        if not isinstance(messages, list):
            raise ValueError("OpenCode 返回不是消息数组")
        return messages, cursor

    requested = limit
    messages, next_before = fetch(requested)
    trimmed = [_trim_message(item) for item in messages if isinstance(item, dict)]
    # 单帧放不下时缩小原生请求页并重新读取：返回的消息必须与游标范围一致，
    # 否则被裁掉的（更新）消息会在下一页被永久跳过。
    while len(json.dumps(trimmed, ensure_ascii=False).encode()) > _FRAME_BUDGET:
        if requested <= 1:
            # 单条消息本身超过单帧上限：明确报告，不静默丢消息。
            return {
                "messages": [],
                "next_before": None,
                "truncated": True,
                "too_large": "单条原生消息超过单帧预算",
            }
        requested = max(1, requested // 2)
        messages, next_before = fetch(requested)
        trimmed = [_trim_message(item) for item in messages if isinstance(item, dict)]
    return {
        "messages": trimmed,
        "next_before": next_before,
        # 页内消息全部返回，游标与内容范围一致。
        "truncated": False,
    }
