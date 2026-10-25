"""读取人工确认时冻结的 DSH 原生证据，沿用 MAP/REDUCE 和 candidate 审核。"""

from __future__ import annotations

import json

from app.config.settings import get_settings
from app.runtime.dsh_client import DshRuntimeClient
from app.runtime.dsh_projection import review_event

from .schemas import TraceResult


async def read_dsh_trace(binding, environment, snapshot: dict) -> TraceResult:
    from .trace_reader import TraceTooLarge, TraceUnavailable

    if (
        snapshot.get("native_session_id") != binding.native_session_id
        or snapshot.get("root_session_id") != binding.id
        or snapshot.get("task_id") != binding.task_id
        or snapshot.get("source_session_ids") != [binding.id]
        or not snapshot.get("native_trace_sha256")
    ):
        raise TraceUnavailable("DSH 复盘来源与冻结绑定不一致")
    client = DshRuntimeClient(environment.id, binding)
    settings = get_settings()
    cursor, count, size = 0, 0, 0
    events = []
    message_ids = set()
    try:
        while True:
            page = await client.read_trace_page(cursor=cursor, limit=40)
            if (
                not page.get("complete")
                or page.get("sha256") != snapshot["native_trace_sha256"]
                or page.get("event_count") != snapshot.get("native_event_count")
                or page.get("session_id") != binding.native_session_id
            ):
                raise TraceUnavailable("DSH 原生轨迹不完整或已偏离人工确认时的冻结证据")
            batch = page["events"]
            if [event.get("seq") for event in batch] != list(range(cursor, cursor + len(batch))):
                raise TraceUnavailable("DSH 分页事件重复或缺口")
            for event in batch:
                count += 1
                size += len(json.dumps(event, ensure_ascii=False).encode())
                if (
                    count > settings.experience_review_max_messages
                    or size > settings.experience_review_max_source_bytes
                ):
                    raise TraceTooLarge("DSH 原生轨迹超过复盘硬上限")
                value = review_event(event, binding.native_session_id)
                if value:
                    events.append(value)
                    if event["type"] in {"user/message", "assistant/message", "tool/result"}:
                        message_ids.add(value.message_id)
            next_cursor = page.get("next_cursor")
            if next_cursor is None:
                break
            if next_cursor != cursor + len(batch) or next_cursor <= cursor:
                raise TraceUnavailable("DSH 原生轨迹游标未推进")
            cursor = next_cursor
    except (TraceUnavailable, TraceTooLarge):
        raise
    except Exception as exc:
        raise TraceUnavailable("DSH 原生证据读取失败") from exc
    return TraceResult(
        events=events,
        sessions=[binding.native_session_id],
        message_count=len(message_ids),
        source_digest=snapshot["native_trace_sha256"],
        watermarks={binding.native_session_id: str(snapshot["native_event_count"])},
    )
